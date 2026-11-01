#!/usr/bin/env python3
"""
PDF Extractor
=============
Finds and downloads PDFs from any web page, including ones that never appear as
a plain <a href="...pdf"> link.

How it finds PDFs (all channels run at the same time)
-----------------------------------------------------
1. NETWORK (Chrome DevTools Protocol): every response is inspected through
   Network.responseReceived / Network.loadingFinished, and the body is pulled
   with Network.getResponseBody. Catches XHR/fetch streams, redirects, iframes
   and PDFs served with a wrong Content-Type.
2. BLOBS: an init script wraps URL.createObjectURL so blob: PDFs generated in
   JavaScript are captured the moment they are created.
3. DOM: every frame (including open shadow roots) is scanned for href/src/data-*
   attributes, inline scripts and viewer wrappers like viewer.html?file=...
   Candidates are fetched with the browser's cookies and verified by magic bytes.
4. DOWNLOAD EVENTS: forced "Content-Disposition: attachment" downloads.
5. DEEP MODE (optional): clicks "Download / View / PDF" style buttons.

Every candidate is accepted ONLY if it really contains the "%PDF-" header, so
tracking pixels, HTML error pages and junk redirects are dropped automatically.

Encrypted PDFs are detected with pypdf. You are asked for the password (GUI
dialog or CLI prompt); if you skip it, the file is kept and flagged as locked.
This tool never tries to crack or guess passwords.

Usage
-----
    python pdf_extractor.py                       # GUI
    python pdf_extractor.py URL [--headed] [--deep] [--out DEFAULT_FOLDER] [--no-prompt] [--open]

Saving
------
Extracted PDFs are first held in a private temporary 'staging' folder. NOTHING is written to a
location of your choosing until you answer the save prompt (folder + file name). Names are sanitised,
folders are validated/created, and existing files are never overwritten without asking.
"""
from __future__ import annotations

import argparse
import atexit
import base64
import getpass
import hashlib
import json
import os
import queue
import re
import shutil
import sys
import tempfile
import threading
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from playwright.sync_api import Error as PWError
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright
from pypdf import PdfReader, PdfWriter

APP_NAME = "PDF Extractor"
PDF_MAGIC = b"%PDF-"
MIN_PDF_BYTES = 1024  # anything smaller is almost certainly a stub / error page
DEFAULT_OUT = Path.home() / "Downloads" / "PDF_Extractor"


def resource_path(rel: str) -> Path:
    """Locate bundled files both from source and inside a PyInstaller exe."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base / rel

# ----------------------------------------------------------------- junk filters
JUNK_HOSTS = (
    "google-analytics.com", "googletagmanager.com", "doubleclick.net", "facebook.net",
    "facebook.com/tr", "hotjar.com", "scorecardresearch.com", "segment.io", "mixpanel.com",
    "clarity.ms", "amazon-adsystem.com", "criteo.", "taboola.", "outbrain.", "newrelic.com",
    "sentry.io", "fullstory.com", "adservice.google",
)
JUNK_MIME_PREFIXES = ("image/", "font/", "audio/", "video/", "text/css", "text/javascript",
                      "application/javascript", "application/font", "application/json")
ASSET_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".css", ".js", ".woff",
              ".woff2", ".ttf", ".mp4", ".mp3", ".webm", ".map")

PDF_URL_RE = re.compile(r"""(?:https?:)?//[^\s"'<>\\)]+?\.pdf(?:\?[^\s"'<>\\)]*)?|"""
                        r"""[A-Za-z0-9_\-./%]+?\.pdf(?:\?[^\s"'<>\\)]*)?""", re.I)
HINT_RE = re.compile(r"(download|attachment|getfile|get_file|file_?id|doc_?id|viewer|document|"
                     r"/pdf|pdf/|export|render|fulltext|full-text)", re.I)

# ------------------------------------------------------- in-page JavaScript
# Wraps URL.createObjectURL so blob: PDFs created by page scripts are captured.
BLOB_HOOK_JS = r"""
(() => {
  if (window.__pdfx_hooked) return; window.__pdfx_hooked = true;
  const orig = URL.createObjectURL;
  URL.createObjectURL = function (obj) {
    try {
      if (obj instanceof Blob && !/^(image|video|audio|font)\//.test(obj.type) && obj.size > 1024) {
        const r = new FileReader();
        r.onload = () => {
          const s = String(r.result); const i = s.indexOf(',');
          if (window.__pdfx_blob) window.__pdfx_blob(s.slice(i + 1), obj.type, location.href);
        };
        r.readAsDataURL(obj);
      }
    } catch (e) {}
    return orig.apply(this, arguments);
  };
})();
"""

# Collects every URL-looking value in a frame, including open shadow roots.
SCAN_JS = r"""
() => {
  const out = new Set();
  const add = v => { if (typeof v === 'string' && v) out.add(v); };
  const walk = root => {
    root.querySelectorAll('*').forEach(el => {
      for (const a of (el.getAttributeNames ? el.getAttributeNames() : [])) {
        if (['href','src','data','content','action','value'].includes(a) || a.startsWith('data-'))
          add(el.getAttribute(a));
      }
      if (el.shadowRoot) walk(el.shadowRoot);
    });
  };
  walk(document);
  document.querySelectorAll('script:not([src])').forEach(s => add((s.textContent || '').slice(0, 400000)));
  return Array.from(out);
}
"""


# ============================================================ data structures
@dataclass
class PdfResult:
    name: str
    path: Path
    url: str
    source: str
    size: int
    pages: Optional[int] = None
    encrypted: bool = False
    locked: bool = False           # still needs a password
    status: str = "ok"
    unlocked_path: Optional[Path] = None
    suggested_name: str = ""          # default file name offered in the save prompt
    generic_name: bool = False        # True when the name was invented (no real filename was found)
    saved_path: Optional[Path] = None  # where the USER chose to save it (None until then)

    @property
    def best_path(self) -> Path:
        """File the user should be handed: the unlocked copy if we made one."""
        return self.unlocked_path or self.path

    def refresh_suggestion(self) -> None:
        """After unlocking, the document title may become readable - use it for a generic name."""
        if self.generic_name and not self.locked:
            title = read_title(self.best_path)
            if title:
                self.suggested_name, self.generic_name = sanitize_filename(title), False


# ============================================================ PDF inspection
def inspect_pdf(path: Path, password: Optional[str] = None) -> dict:
    """Return {'encrypted', 'locked', 'pages', 'error'} without ever raising."""
    info = {"encrypted": False, "locked": False, "pages": None, "error": None}
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            info["encrypted"] = True
            # Empty user password is common ("owner-restricted" PDFs open fine).
            if int(reader.decrypt(password or "")) == 0:
                info["locked"] = True
                return info
        info["pages"] = len(reader.pages)
    except Exception as exc:  # corrupt files, missing AES backend, etc.
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info


def unlock_pdf(path: Path, password: str) -> Optional[Path]:
    """Write '<name>.unlocked.pdf' using a password the user supplied."""
    try:
        reader = PdfReader(str(path))
        if int(reader.decrypt(password)) == 0:
            return None
        writer = PdfWriter(clone_from=reader)
        out = path.with_name(path.stem + ".unlocked.pdf")
        with open(out, "wb") as fh:
            writer.write(fh)
        return out
    except Exception:
        return None


def open_pdf(path: Path) -> None:
    """Open in the system's default PDF viewer."""
    if sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        webbrowser.open(Path(path).resolve().as_uri())


def open_in_browser(path: Path) -> None:
    webbrowser.open(Path(path).resolve().as_uri())


# ============================================================ candidate parsing
def extract_candidates(raw_values: list[str], base: str) -> tuple[list[str], list[str]]:
    """Split raw page strings into (explicit .pdf URLs, weaker 'hint' URLs)."""
    explicit: list[str] = []
    hints: list[str] = []
    for raw in raw_values:
        v = raw.strip()
        if not v:
            continue
        if v.lower().startswith("data:application/pdf"):
            explicit.append(v)
            continue
        if len(v) > 1500:  # inline script / JSON blob: regex it
            for m in PDF_URL_RE.finditer(v.replace("\\/", "/")):
                explicit.append(urljoin(base, m.group(0)))
            continue
        low = v.lower()
        if low.startswith(("javascript:", "mailto:", "tel:", "#", "blob:")):
            continue
        absolute = urljoin(base, v)
        # Viewer wrappers: viewer.html?file=https://host/doc.pdf
        for vals in parse_qs(urlparse(absolute).query).values():
            for q in vals:
                if ".pdf" in q.lower():
                    explicit.append(urljoin(base, unquote(q)))
        path = urlparse(absolute).path.lower()
        if ".pdf" in low:
            explicit.append(absolute)
        elif HINT_RE.search(low) and not path.endswith(ASSET_EXTS) and absolute.startswith("http"):
            hints.append(absolute)
    return list(dict.fromkeys(explicit)), list(dict.fromkeys(hints))


# ============================================================ saving helpers (shared by CLI + GUI)
class SaveError(Exception):
    """A problem the user can fix (bad folder, no permission, name clash...). Message is user-friendly."""


ILLEGAL_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                    *(f"LPT{i}" for i in range(1, 10))}
MAX_STEM_LEN = 150
PREFS_FILE = Path.home() / ".pdf_extractor.json"


def sanitize_filename(raw: Optional[str], default: str = "document.pdf") -> str:
    """Make any user-typed text a safe PDF file name on Windows, macOS and Linux.

    * illegal characters  < > : " / \\ | ? *  and control characters become "_"
    * leading/trailing spaces and dots are removed (Windows silently drops them)
    * reserved Windows device names (CON, NUL, COM1...) get a "_" prefix
    * length is capped, ".pdf" is added when missing, blank input falls back to `default`
    NOTE: server.py relies on this function, and web/index.html mirrors it for live previews.
    """
    name = ILLEGAL_NAME_CHARS.sub("_", raw or "")
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        return sanitize_filename(default, "document.pdf") if default != "document.pdf" else "document.pdf"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    stem = name[:-4].rstrip(" .")
    if stem.split(".")[0].upper() in WINDOWS_RESERVED:
        stem = "_" + stem
    stem = stem[:MAX_STEM_LEN].rstrip(" .") or "document"
    return stem + ".pdf"


safe_filename = sanitize_filename   # backwards-compatible alias


def read_title(path: Path) -> Optional[str]:
    """The PDF's embedded document title, if it has a sensible one."""
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted and int(reader.decrypt("")) == 0:
            return None
        title = ((reader.metadata.title if reader.metadata else "") or "").strip()
    except Exception:
        return None
    return title if 3 <= len(title) <= 120 and re.search(r"[A-Za-z0-9]", title) else None


def load_last_dir() -> Path:
    """Last folder the user saved to (remembered between runs), else ~/Downloads/PDF_Extractor."""
    try:
        d = Path(json.loads(PREFS_FILE.read_text("utf-8")).get("last_dir", ""))
        if str(d) and d.is_dir():
            return d
    except Exception:
        pass
    return DEFAULT_OUT


def remember_dir(folder: Path) -> None:
    try:
        PREFS_FILE.write_text(json.dumps({"last_dir": str(folder)}), "utf-8")
    except OSError:
        pass  # remembering the folder is a convenience, never an error


def resolve_dest_dir(raw: Optional[str], default: Path) -> Path:
    """Validate a user-typed folder: expands ~ and %VARS%, creates it if missing, proves it is writable.

    Raises SaveError with a plain-English message instead of crashing.
    """
    text = (raw or "").strip().strip("\"'")
    shown = text or str(default)
    try:
        folder = Path(os.path.expandvars(os.path.expanduser(text))) if text else Path(default)
        if not folder.is_absolute():
            folder = Path.cwd() / folder
        folder = folder.resolve()
        if folder.exists() and not folder.is_dir():
            raise SaveError(f"'{folder}' is a file, not a folder. Pick a folder instead.")
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=folder, prefix=".pdfx_write_test_"):   # prove we can write
            pass
    except SaveError:
        raise
    except PermissionError:
        raise SaveError(f"No permission to write to '{shown}'. Choose a different folder.") from None
    except (OSError, ValueError) as exc:
        reason = getattr(exc, "strerror", None) or str(exc)
        raise SaveError(f"Can't use the folder '{shown}': {reason}") from None
    return folder


def unique_path(target: Path) -> Path:
    """'report.pdf' -> 'report (1).pdf' -> 'report (2).pdf' ..."""
    n = 1
    while True:
        candidate = target.with_name(f"{target.stem} ({n}){target.suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def save_pdf(src: Path, folder: Path, filename: str,
             on_conflict: Optional[Callable[[Path], str]] = None) -> Path:
    """Copy `src` into `folder` as `filename` (already validated + sanitised by the caller).

    If the target exists, `on_conflict(target)` must return "overwrite", "rename" or "cancel"
    (default when no callback: "rename" - we never silently overwrite).
    The copy goes to a .part file first so an interrupted save never leaves a truncated PDF.
    """
    target = folder / filename
    if target.exists():
        action = on_conflict(target) if on_conflict else "rename"
        if action == "cancel":
            raise SaveError("Cancelled - nothing was saved.")
        if action == "rename":
            target = unique_path(target)
    part = target.with_name(target.name + ".part")
    try:
        shutil.copyfile(src, part)
        os.replace(part, target)
    except PermissionError:
        raise SaveError(f"No permission to write '{target.name}' in '{folder}'.") from None
    except OSError as exc:
        raise SaveError(f"Could not save the file: {getattr(exc, 'strerror', None) or exc}") from None
    finally:
        part.unlink(missing_ok=True)
    return target


# ============================================================ the engine
class Extractor:
    def __init__(
        self,
        out_dir: Optional[Path] = None,       # None = private temp staging folder (recommended)
        headless: bool = True,
        deep: bool = False,
        wait: int = 6,
        password: Optional[str] = None,
        log: Callable[[str], None] = print,
        on_result: Optional[Callable[[PdfResult], None]] = None,
        password_provider: Optional[Callable[[str], Optional[str]]] = None,
        url_guard: Optional[Callable[[str], bool]] = None,   # return False to block a URL (SSRF protection)
        max_bytes: int = 200 * 1024 * 1024,                   # ignore PDFs bigger than this
        max_seconds: Optional[int] = None,                    # hard time budget for one run
    ):
        self.out_dir: Optional[Path] = Path(out_dir) if out_dir else None
        self._own_dir = out_dir is None
        self.headless, self.deep, self.wait = headless, deep, wait
        self.password = password
        self.log = log
        self.on_result = on_result
        self.password_provider = password_provider
        self.url_guard, self.max_bytes, self.max_seconds = url_guard, max_bytes, max_seconds
        self._deadline: Optional[float] = None
        self.results: list[PdfResult] = []
        self._hashes: set[str] = set()
        self._tried: set[str] = set()
        self._meta: dict = {}
        self._pending: list = []     # CDP responses ready for getResponseBody
        self._blobs: list = []       # (base64, mime, page_url)
        self._downloads: list = []   # Playwright Download objects
        self._attached: set[int] = set()
        self._page = None
        self._ctx = None

    # ------------------------------------------------------------- public API
    def run(self, url: str) -> list[PdfResult]:
        if not re.match(r"^[a-z]+://", url, re.I):
            url = "https://" + url
        if self.out_dir is None:      # staging area: NOT where the user's files end up
            self.out_dir = Path(tempfile.mkdtemp(prefix="pdfx_"))
        self.out_dir.mkdir(parents=True, exist_ok=True)
        if self.max_seconds:
            self._deadline = time.monotonic() + self.max_seconds
        with sync_playwright() as pw:
            browser = self._launch(pw)
            try:
                ctx = self._ctx = browser.new_context(accept_downloads=True, ignore_https_errors=True)
                ctx.expose_function("__pdfx_blob", self._on_blob)
                ctx.add_init_script(BLOB_HOOK_JS)
                if self.url_guard:  # vets every browser request, including redirects and sub-resources
                    ctx.route("**/*", lambda route: route.continue_()
                              if self.url_guard(route.request.url) else route.abort())
                ctx.on("page", self._attach)
                page = self._page = ctx.new_page()
                self._attach(page)

                self.log(f"Opening {url}")
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                except PWTimeout:
                    self.log("Page load timed out - continuing with what loaded.")
                except PWError as exc:
                    if "Download is starting" not in str(exc):  # direct PDF links download instead
                        self.log(f"Navigation warning: {str(exc).splitlines()[0]}")

                self._pump(2)
                self._scroll(page)               # trigger lazy-loaded content
                self._scan_dom(ctx)
                if self.deep:
                    self._click_candidates(ctx, page)
                self.log(f"Listening for late network activity ({self.wait}s)...")
                self._pump(self.wait)
                self._scan_dom(ctx)              # second pass after JS settled
                self._pump(1)
            finally:
                browser.close()
        self.log(f"Done - {len(self.results)} PDF(s) extracted.")
        return self.results

    def cleanup(self) -> None:
        """Delete the staging folder (only if this Extractor created it)."""
        if self._own_dir and self.out_dir:
            shutil.rmtree(self.out_dir, ignore_errors=True)

    # ------------------------------------------------------------- browser setup
    def _launch(self, pw):
        # Prefer the Edge/Chrome already on the machine => exe needs no browser download.
        # PDFX_CHANNEL=bundled forces Playwright's own Chromium (used in the Docker image).
        forced = os.environ.get("PDFX_CHANNEL")
        channels = [None] if forced == "bundled" else ([forced] if forced else ["msedge", "chrome", None])
        args = ["--no-sandbox", "--disable-dev-shm-usage"] if os.environ.get("PDFX_NO_SANDBOX") else []
        for channel in channels:
            try:
                b = pw.chromium.launch(channel=channel, headless=self.headless, args=args)
                self.log(f"Browser: {channel or 'bundled Chromium'}")
                return b
            except PWError:
                continue
        raise RuntimeError("No Chromium browser found. Install Edge/Chrome, or run "
                           "'python -m playwright install chromium'.")

    def _attach(self, page) -> None:
        """Hook one page: CDP Network domain + download events."""
        if id(page) in self._attached:
            return
        self._attached.add(id(page))
        page.on("download", lambda d: self._downloads.append(d))
        try:
            s = self._ctx.new_cdp_session(page)
            s.send("Network.enable")
            s.on("Network.responseReceived", lambda e, s=s: self._on_response(s, e))
            s.on("Network.loadingFinished", lambda e, s=s: self._on_finished(s, e))
        except PWError:
            pass

    # ------------------------------------------------------------- CDP handlers
    def _on_response(self, s, e) -> None:
        r = e.get("response", {})
        url, mime = r.get("url", ""), (r.get("mimeType") or "").lower()
        if r.get("status") != 200 or url.startswith(("data:", "blob:")) or self._is_junk(url, mime):
            return
        hdrs = {k.lower(): v for k, v in (r.get("headers") or {}).items()}
        cd = hdrs.get("content-disposition", "")
        if ("pdf" in mime or ".pdf" in urlparse(url).path.lower() or ".pdf" in cd.lower()
                or mime in ("application/octet-stream", "binary/octet-stream", "")):
            self._meta[(id(s), e["requestId"])] = {"url": url, "mime": mime, "cd": cd}

    def _on_finished(self, s, e) -> None:
        meta = self._meta.pop((id(s), e["requestId"]), None)
        if meta:
            self._pending.append((s, e["requestId"], meta))

    def _on_blob(self, b64: str, mime: str, page_url: str) -> None:
        self._blobs.append((b64, mime, page_url))

    # ------------------------------------------------------------- processing loop
    def _pump(self, seconds: float) -> None:
        """Let the browser run (required for events in the sync API) and drain queues."""
        page, elapsed = self._page, 0.0
        while elapsed < seconds:
            if self._deadline and time.monotonic() > self._deadline:
                break
            try:
                page.wait_for_timeout(250)
            except PWError:
                break
            elapsed += 0.25
            self._drain()
        self._drain()

    def _drain(self) -> None:
        while self._pending:
            s, rid, meta = self._pending.pop(0)
            try:
                body = s.send("Network.getResponseBody", {"requestId": rid})
                data = (base64.b64decode(body["body"]) if body.get("base64Encoded")
                        else body["body"].encode("latin-1", "ignore"))
            except PWError:
                continue
            self._consider(data, meta["url"], f"network ({meta['mime'] or 'unknown'})", meta["cd"])
        while self._blobs:
            b64, mime, page_url = self._blobs.pop(0)
            try:
                self._consider(base64.b64decode(b64), page_url, f"blob ({mime or 'untyped'})")
            except ValueError:
                pass
        while self._downloads:
            dl = self._downloads.pop(0)
            try:
                tmp = Path(tempfile.mkdtemp()) / (dl.suggested_filename or "download.pdf")
                dl.save_as(str(tmp))
                self._consider(tmp.read_bytes(), dl.url, "download event", f'filename="{dl.suggested_filename}"')
                shutil.rmtree(tmp.parent, ignore_errors=True)
            except PWError as exc:
                self.log(f"Download failed: {str(exc).splitlines()[0]}")

    # ------------------------------------------------------------- DOM discovery
    def _scroll(self, page) -> None:
        for _ in range(8):
            try:
                page.evaluate("window.scrollBy(0, Math.max(600, document.documentElement.scrollHeight / 4))")
            except PWError:
                break
            self._pump(0.6)

    def _scan_dom(self, ctx) -> None:
        explicit: list[str] = []
        hints: list[str] = []
        referer = self._page.url
        for pg in ctx.pages:
            for fr in pg.frames:
                try:
                    raw = fr.evaluate(SCAN_JS)
                except PWError:
                    continue
                e, h = extract_candidates(raw, fr.url or pg.url)
                explicit += e
                hints += h
        explicit = [u for u in dict.fromkeys(explicit) if u not in self._tried]
        hints = [u for u in dict.fromkeys(hints) if u not in self._tried][:25]  # cap speculative fetches
        self.log(f"DOM scan: {len(explicit)} explicit and {len(hints)} possible PDF link(s).")
        for u in explicit:
            self._fetch(ctx, u, referer, "dom link")
        for u in hints:
            self._fetch(ctx, u, referer, "dom hint")

    def _fetch(self, ctx, url: str, referer: str, source: str) -> None:
        if url in self._tried:
            return
        self._tried.add(url)
        if url.lower().startswith("data:application/pdf"):
            try:
                self._consider(base64.b64decode(url.split(",", 1)[1]), "data-uri", "inline data URI")
            except ValueError:
                pass
            return
        if self._is_junk(url, ""):
            return
        try:  # uses the browser context's cookies, so logged-in sessions work
            resp = None
            for _ in range(6):  # follow redirects by hand so each hop can be vetted
                if self.url_guard and not self.url_guard(url):
                    self.log(f"Blocked non-public address: {url[:90]}")
                    return
                resp = ctx.request.get(url, headers={"Referer": referer}, timeout=30_000, max_redirects=0)
                loc = resp.headers.get("location")
                if resp.status in (301, 302, 303, 307, 308) and loc:
                    url = urljoin(url, loc)
                    continue
                break
            else:
                return
            if resp.ok and not self._is_junk(url, resp.headers.get("content-type", "")):
                self._consider(resp.body(), url, source, resp.headers.get("content-disposition", ""))
        except PWError:
            pass

    def _click_candidates(self, ctx, page) -> None:
        """Deep mode: click buttons/links that look like they open a document."""
        self.log("Deep mode: clicking download/view style controls...")
        loc = page.locator("a, button, [role=button]").filter(
            has_text=re.compile(r"pdf|download|view|open|read|full.?text|document", re.I))
        try:
            n = min(loc.count(), 8)
        except PWError:
            return
        start = page.url
        for i in range(n):
            try:
                loc.nth(i).click(timeout=2000, no_wait_after=True)
            except PWError:
                continue
            self._pump(1.5)
            if page.url != start:
                try:
                    page.go_back(timeout=10_000)
                except PWError:
                    pass
        self._scan_dom(ctx)

    # ------------------------------------------------------------- validation + saving
    @staticmethod
    def _is_junk(url: str, mime: str) -> bool:
        u = url.lower()
        return (any(h in u for h in JUNK_HOSTS)
                or urlparse(u).path.endswith(ASSET_EXTS)
                or mime.lower().startswith(JUNK_MIME_PREFIXES))

    def _consider(self, data: bytes, url: str, source: str, content_disp: str = "") -> None:
        """Accept `data` only if it is a real PDF; save, dedupe, check encryption."""
        if not data or PDF_MAGIC not in data[:1024]:
            return                                   # HTML error page, tracker, redirect, etc.
        if len(data) < MIN_PDF_BYTES or len(data) > self.max_bytes:
            return
        digest = hashlib.sha256(data).hexdigest()
        if digest in self._hashes:
            return
        self._hashes.add(digest)

        m = re.search(r"""filename\*?=(?:UTF-8'')?"?([^";]+)""", content_disp, re.I)
        raw_name = unquote(m.group(1) if m else Path(urlparse(url).path).name)
        real_name = raw_name.lower().endswith(".pdf")          # did the site give us a genuine file name?
        staged = sanitize_filename(raw_name if real_name else f"document_{digest[:8]}.pdf")
        path = self.out_dir / staged
        if path.exists():
            path = unique_path(path)
        path.write_bytes(data)                                  # staging only - the user picks the real destination

        info = inspect_pdf(path, self.password)
        res = PdfResult(path.name, path, url, source, len(data), info["pages"],
                        info["encrypted"], info["locked"])
        res.suggested_name, res.generic_name = staged, not real_name
        if not real_name:                                       # no real filename: prefer the document's own title
            res.refresh_suggestion()
        if info["error"]:
            res.status = f"extracted, but could not be parsed ({info['error']})"
        if res.locked:
            self._handle_password(res)
        elif res.encrypted:
            res.status = "encrypted (opens without password)"
        self.log(f"PDF found [{source}] {res.name} - {len(data) / 1024:.0f} KB - {res.status}")
        self.results.append(res)
        if self.on_result:
            self.on_result(res)

    def _handle_password(self, res: PdfResult) -> None:
        """Try the supplied password, then ask the user (max 3 tries). Never brute-forces."""
        res.status = "password required"
        for attempt in range(3):
            pw = self.password if attempt == 0 and self.password else (
                self.password_provider(res.name) if self.password_provider else None)
            if not pw:
                self.log(f"{res.name}: password-protected - flagged, left locked.")
                return
            out = unlock_pdf(res.path, pw)
            if out:
                res.unlocked_path, res.locked = out, False
                res.pages = inspect_pdf(out)["pages"]
                res.refresh_suggestion()
                res.status = "unlocked"
                return
            self.log(f"{res.name}: wrong password.")


# ============================================================ CLI
def cli_save_flow(console, results: list[PdfResult], default_dir: Path, interactive: bool = True) -> list[Path]:
    """Ask, for every extracted PDF, where to save it and what to call it - then save and offer to open it."""
    from rich.prompt import Confirm, Prompt

    def on_conflict(target: Path) -> str:
        return Prompt.ask(f"'{target.name}' already exists there", choices=["overwrite", "rename", "cancel"],
                          default="rename")

    saved: list[Path] = []
    last = default_dir
    for i, r in enumerate(results, 1):
        console.rule(f"PDF {i} of {len(results)}")
        console.print(f"[bold]{r.suggested_name}[/bold]  ({r.size / 1024:.0f} KB, {r.pages or '?'} pages, {r.status})")
        if r.locked:
            console.print("[yellow]Still password-protected - the encrypted original will be saved.[/yellow]")
        if interactive and not Confirm.ask("Save this PDF?", default=True):
            continue
        while True:
            try:
                raw_dir = Prompt.ask("Folder to save in", default=str(last)) if interactive else str(last)
                folder = resolve_dest_dir(raw_dir, last)            # validates / creates / tests write access
                raw_name = Prompt.ask("File name (Enter keeps the default)", default=r.suggested_name) \
                    if interactive else r.suggested_name
                name = sanitize_filename(raw_name, r.suggested_name)
                if name != raw_name:
                    console.print(f"[dim]File name adjusted to:[/dim] {name}")
                out = save_pdf(r.best_path, folder, name, on_conflict if interactive else None)
            except SaveError as exc:
                console.print(f"[red]{exc}[/red]")
                if interactive and Confirm.ask("Try again?", default=True):
                    continue
                break
            r.saved_path, last = out, folder
            remember_dir(folder)
            saved.append(out)
            console.print(f"[green]Saved:[/green] {out}")
            if interactive:                                          # post-save verification
                how = Prompt.ask("Open it to verify?", choices=["viewer", "browser", "no"], default="no")
                if how == "viewer":
                    open_pdf(out)
                elif how == "browser":
                    open_in_browser(out)
            break
    return saved


def run_cli(args) -> int:
    from rich.console import Console
    from rich.table import Table

    console = Console()
    ex = Extractor(                       # no out_dir => private staging folder; nothing lands in the user's folders yet
        headless=not args.headed, deep=args.deep, wait=args.wait,
        password=args.password, log=lambda m: console.log(m),
        password_provider=lambda name: getpass.getpass(f"Password for {name} (blank = skip): ") or None,
    )
    try:
        try:
            results = ex.run(args.url)
        except RuntimeError as exc:
            console.print(f"[red]{exc}[/red]")
            return 1
        if not results:
            console.print("[yellow]No PDFs found. Try --deep, --headed, or a larger --wait.[/yellow]")
            return 2
        table = Table(title="Extracted PDFs (not saved yet)")
        for col in ("#", "Suggested name", "Pages", "Size", "Status"):
            table.add_column(col)
        for i, r in enumerate(results, 1):
            table.add_row(str(i), r.suggested_name, str(r.pages or "?"), f"{r.size / 1024:.0f} KB", r.status)
        console.print(table)
        saved = cli_save_flow(console, results, Path(args.out) if args.out else load_last_dir(),
                              interactive=not args.no_prompt)
        if args.open and saved:
            open_pdf(saved[0])
        if not saved:
            console.print("[yellow]Nothing was saved.[/yellow]")
        return 0
    finally:
        ex.cleanup()


# ============================================================ GUI
def run_gui() -> None:
    import customtkinter as ctk
    from tkinter import filedialog, messagebox, simpledialog

    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")

    class SaveDialog(ctk.CTkToplevel):
        """Modal 'where + what name?' prompt shown before any PDF is saved."""

        def __init__(self, master, r: PdfResult, default_dir: Path, on_saved: Callable[[Path], None]):
            super().__init__(master)
            self.r, self.default_dir, self.on_saved = r, Path(default_dir), on_saved
            self.title("Save PDF")
            self.geometry("620x340")
            self.resizable(False, False)
            self.transient(master)
            ico = resource_path("assets/app.ico")
            if ico.exists() and sys.platform.startswith("win"):
                self.after(250, lambda: self.iconbitmap(str(ico)))
            self.after(150, self.grab_set)                      # modal, but only once the window is mapped

            ctk.CTkLabel(self, text="Save PDF", font=ctk.CTkFont(size=20, weight="bold")
                         ).pack(anchor="w", padx=22, pady=(18, 0))
            ctk.CTkLabel(self, text="Choose where it goes and what it's called.", text_color="gray70"
                         ).pack(anchor="w", padx=22)

            ctk.CTkLabel(self, text="Folder").pack(anchor="w", padx=22, pady=(14, 2))
            row = ctk.CTkFrame(self, fg_color="transparent")
            row.pack(fill="x", padx=22)
            self.folder = ctk.CTkEntry(row, height=36)
            self.folder.insert(0, str(self.default_dir))
            self.folder.pack(side="left", fill="x", expand=True)
            ctk.CTkButton(row, text="Browse...", width=96, height=36, fg_color="gray25", hover_color="gray35",
                          command=self.browse).pack(side="left", padx=(8, 0))

            ctk.CTkLabel(self, text="File name (leave blank to use the default)").pack(anchor="w", padx=22, pady=(12, 2))
            self.name = ctk.CTkEntry(self, height=36, placeholder_text=r.suggested_name)
            self.name.insert(0, r.suggested_name)
            self.name.pack(fill="x", padx=22)
            self.name.bind("<KeyRelease>", lambda _e: self.preview())

            self.msg = ctk.CTkLabel(self, text="", anchor="w", justify="left", wraplength=570, text_color="gray70")
            self.msg.pack(fill="x", padx=22, pady=10)

            btns = ctk.CTkFrame(self, fg_color="transparent")
            btns.pack(fill="x", padx=22, pady=(0, 16), side="bottom")
            ctk.CTkButton(btns, text="Save", width=110, command=self.save).pack(side="right")
            ctk.CTkButton(btns, text="Cancel", width=90, fg_color="gray25", hover_color="gray35",
                          command=self.destroy).pack(side="right", padx=8)
            self.bind("<Return>", lambda _e: self.save())
            self.bind("<Escape>", lambda _e: self.destroy())
            self.after(200, lambda: (self.name.focus_set(), self.name.select_range(0, max(0, len(self.name.get()) - 4))))
            self.preview()

        def browse(self):
            d = filedialog.askdirectory(initialdir=self.folder.get() or str(self.default_dir), parent=self)
            if d:
                self.folder.delete(0, "end")
                self.folder.insert(0, d)

        def show(self, text: str, error: bool = False):
            self.msg.configure(text=text, text_color="#f87171" if error else "gray70")

        def preview(self):
            raw = self.name.get()
            final = sanitize_filename(raw, self.r.suggested_name)
            if not raw.strip():
                self.show(f"Blank - will be saved as: {final}")
            elif final != raw:
                self.show(f"Will be saved as: {final}   (illegal characters replaced / .pdf added)")
            else:
                self.show("")

        def save(self):
            try:
                folder = resolve_dest_dir(self.folder.get(), self.default_dir)
            except SaveError as exc:
                return self.show(str(exc), error=True)
            name = sanitize_filename(self.name.get(), self.r.suggested_name)

            def conflict(target: Path) -> str:
                ans = messagebox.askyesnocancel(
                    "File already exists",
                    f"'{target.name}' already exists in this folder.\n\nYes = overwrite it\n"
                    f"No = keep both (save with a new name)\nCancel = go back", parent=self)
                return "overwrite" if ans is True else "rename" if ans is False else "cancel"

            try:
                out = save_pdf(self.r.best_path, folder, name, conflict)
            except SaveError as exc:
                return self.show(str(exc), error=True)
            remember_dir(folder)
            self.on_saved(out)
            self.destroy()

    class App(ctk.CTk):
        def __init__(self):
            super().__init__()
            self.title(APP_NAME)
            self.geometry("940x720")
            self.minsize(780, 560)
            self.q: queue.Queue = queue.Queue()
            self.default_dir = load_last_dir()          # pre-fills the save prompt
            self._stages: list[Path] = []               # private temp folders holding unsaved results
            atexit.register(lambda: [shutil.rmtree(d, ignore_errors=True) for d in self._stages])
            ico = resource_path("assets/app.ico")
            if ico.exists() and sys.platform.startswith("win"):
                self.after(250, lambda: self.iconbitmap(str(ico)))

            ctk.CTkLabel(self, text="PDF Extractor", font=ctk.CTkFont(size=26, weight="bold")
                         ).pack(anchor="w", padx=24, pady=(20, 0))
            ctk.CTkLabel(self, text="Finds PDFs hidden in the DOM, blobs and network traffic.",
                         text_color="gray70").pack(anchor="w", padx=24)

            bar = ctk.CTkFrame(self, fg_color="transparent")
            bar.pack(fill="x", padx=24, pady=14)
            self.url = ctk.CTkEntry(bar, placeholder_text="https://example.com/page-with-a-hidden-pdf", height=40)
            self.url.pack(side="left", fill="x", expand=True)
            self.url.bind("<Return>", lambda _e: self.start())
            self.go = ctk.CTkButton(bar, text="Extract", width=110, height=40, command=self.start)
            self.go.pack(side="left", padx=(10, 0))

            opts = ctk.CTkFrame(self, fg_color="transparent")
            opts.pack(fill="x", padx=24)
            self.headed = ctk.CTkCheckBox(opts, text="Show browser (logins / captchas)")
            self.headed.pack(side="left")
            self.deep = ctk.CTkCheckBox(opts, text="Deep mode (click buttons)")
            self.deep.pack(side="left", padx=18)
            self.out_btn = ctk.CTkButton(opts, text=f"Default folder: {self.default_dir.name}", width=220,
                                         fg_color="gray25", hover_color="gray35", command=self.pick_dir)
            self.out_btn.pack(side="right")

            self.progress = ctk.CTkProgressBar(self, mode="indeterminate")
            self.progress.pack(fill="x", padx=24, pady=(14, 6))
            self.progress.set(0)

            self.log_box = ctk.CTkTextbox(self, height=150, font=ctk.CTkFont(family="Consolas", size=12))
            self.log_box.pack(fill="x", padx=24)
            self.log_box.configure(state="disabled")

            ctk.CTkLabel(self, text="Results", font=ctk.CTkFont(size=16, weight="bold")
                         ).pack(anchor="w", padx=24, pady=(14, 4))
            self.rows = ctk.CTkScrollableFrame(self)
            self.rows.pack(fill="both", expand=True, padx=24, pady=(0, 20))
            self.after(100, self.poll)

        # --- helpers
        def pick_dir(self):
            d = filedialog.askdirectory(initialdir=self.default_dir)
            if d:
                self.default_dir = Path(d)
                self.out_btn.configure(text=f"Default folder: {self.default_dir.name}")

        def write_log(self, msg: str):
            self.log_box.configure(state="normal")
            self.log_box.insert("end", msg + "\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")

        # --- run the engine on a worker thread; UI is only touched via the queue
        def start(self):
            url = self.url.get().strip()
            if not url:
                return
            for w in self.rows.winfo_children():
                w.destroy()
            self.go.configure(state="disabled", text="Working...")
            self.progress.start()
            stage = Path(tempfile.mkdtemp(prefix="pdfx_"))   # extraction lands here, NOT in the user's folders
            self._stages.append(stage)
            args = dict(out_dir=stage, headless=not self.headed.get(), deep=bool(self.deep.get()))
            threading.Thread(target=self.worker, args=(url, args), daemon=True).start()

        def worker(self, url: str, args: dict):
            ex = Extractor(**args, log=lambda m: self.q.put(("log", m)),
                           on_result=lambda r: self.q.put(("result", r)),
                           password_provider=self.ask_password_threadsafe)
            try:
                ex.run(url)
            except Exception as exc:
                self.q.put(("log", f"ERROR: {exc}"))
            self.q.put(("done", len(ex.results)))

        def ask_password_threadsafe(self, name: str) -> Optional[str]:
            box: dict = {}
            evt = threading.Event()
            self.q.put(("pw", name, box, evt))
            evt.wait()
            return box.get("pw")

        def poll(self):
            try:
                while True:
                    item = self.q.get_nowait()
                    kind = item[0]
                    if kind == "log":
                        self.write_log(item[1])
                    elif kind == "result":
                        self.add_row(item[1])
                    elif kind == "pw":
                        _, name, box, evt = item
                        box["pw"] = simpledialog.askstring(
                            "Password required", f"'{name}' is encrypted.\nEnter its password (Cancel to skip):",
                            show="*", parent=self)
                        evt.set()
                    elif kind == "done":
                        self.progress.stop()
                        self.progress.set(0)
                        self.go.configure(state="normal", text="Extract")
                        if item[1] == 0:
                            self.write_log("No PDFs found. Try Deep mode, or show the browser and log in first.")
            except queue.Empty:
                pass
            self.after(100, self.poll)

        # --- result rows: Save as... / View / In browser / Unlock
        def add_row(self, r: PdfResult):
            row = ctk.CTkFrame(self.rows)
            row.pack(fill="x", pady=5)
            info = ctk.CTkFrame(row, fg_color="transparent")
            info.pack(side="left", fill="x", expand=True, padx=12, pady=8)
            name_label = ctk.CTkLabel(info, text=r.suggested_name, anchor="w", font=ctk.CTkFont(size=14, weight="bold"))
            name_label.pack(fill="x")
            meta = f"{r.size / 1024:.0f} KB  |  {r.pages or '?'} pages  |  {r.source}  |  {r.status}"
            status_label = ctk.CTkLabel(info, text=meta, anchor="w", text_color="gray70")
            status_label.pack(fill="x")
            saved_label = ctk.CTkLabel(info, text="", anchor="w", text_color="#34d399")
            saved_label.pack(fill="x")

            def current() -> Path:
                """View/verify the file the user actually saved, once there is one."""
                return r.saved_path or r.best_path

            def on_saved(out: Path):
                r.saved_path = out
                saved_label.configure(text=f"Saved to: {out}")
                save_btn.configure(text="Save another copy...")
                self.default_dir = out.parent
                self.out_btn.configure(text=f"Default folder: {out.parent.name or out.parent}")
                self.write_log(f"Saved {out}")

            def save():
                SaveDialog(self, r, self.default_dir, on_saved)

            def unlock():
                pw = simpledialog.askstring("Password required", f"Password for '{r.suggested_name}':", show="*", parent=self)
                out = unlock_pdf(r.path, pw) if pw else None
                if out:
                    r.unlocked_path, r.locked, r.status = out, False, "unlocked"
                    r.refresh_suggestion()
                    name_label.configure(text=r.suggested_name)
                    status_label.configure(text=meta.rsplit("|", 1)[0] + "|  unlocked")
                    unlock_btn.destroy()
                elif pw:
                    self.write_log("Wrong password.")

            unlock_btn = ctk.CTkButton(row, text="Unlock", width=80, fg_color="#b45309", hover_color="#92400e",
                                       command=unlock)
            if r.locked:
                unlock_btn.pack(side="right", padx=(0, 12))
            save_btn = ctk.CTkButton(row, text="Save as...", width=110, command=save)
            save_btn.pack(side="right", padx=4)
            ctk.CTkButton(row, text="In browser", width=96, fg_color="gray25", hover_color="gray35",
                          command=lambda: open_in_browser(current())).pack(side="right", padx=4)
            ctk.CTkButton(row, text="View", width=70, fg_color="gray25", hover_color="gray35",
                          command=lambda: open_pdf(current())).pack(side="right", padx=4)

    App().mainloop()


# ============================================================ entry point
def main() -> int:
    ap = argparse.ArgumentParser(description="Find PDFs hidden on any web page, then save them where you choose.")
    ap.add_argument("url", nargs="?", help="Page URL. Omit to launch the GUI.")
    ap.add_argument("--out", default=None, help="Default folder offered in the save prompt")
    ap.add_argument("--no-prompt", action="store_true",
                    help="Skip the save prompts: save every PDF into --out using its suggested name (for scripts)")
    ap.add_argument("--headed", action="store_true", help="Show the browser window")
    ap.add_argument("--deep", action="store_true", help="Click download/view style buttons")
    ap.add_argument("--wait", type=int, default=6, help="Seconds to keep listening to network traffic")
    ap.add_argument("--password", help="Password to try on encrypted PDFs")
    ap.add_argument("--open", action="store_true", help="Open the first result when finished")
    args = ap.parse_args()
    if args.url:
        return run_cli(args)
    run_gui()
    return 0


if __name__ == "__main__":
    sys.exit(main())
