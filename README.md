# PDF Extractor

Finds and downloads PDFs from any page — including ones not exposed as normal links —
using the Chrome DevTools Protocol (via Playwright). Shows each result with **Download**,
**View** (default PDF app), **In browser**, and **Unlock** (for encrypted files).

> Use it on content you're allowed to access. It does not bypass logins, paywalls, DRM,
> or guess passwords; for login-gated pages use "Show browser" and sign in yourself.

## Web version (zero install for users)
See **DEPLOY.md** - FastAPI server (`server.py`) + browser UI (`web/index.html`), Docker/Render/Fly/Cloud Run configs included.
Quick try: `.\run.ps1 -Web` then open http://localhost:8000.

## Saving files (you choose where and what)
Nothing is written to a location of your choosing until you answer a save prompt. Extracted PDFs wait in a private
temporary staging folder (deleted on exit), then:

| Where | What you see |
|---|---|
| **Desktop GUI** | **Save as...** opens a dialog: folder box + **Browse...**, file-name box (blank = default), live preview of the cleaned name. Remembers your last folder. |
| **CLI** | Prompts per PDF: *Save this PDF? / Folder / File name / Open it to verify (viewer, browser, no)*. `--out DIR` sets the default folder; `--no-prompt` (`-NoPrompt`) skips prompts for scripting. |
| **Web UI** | **Save as...** dialog with **Choose folder...** (Chrome/Edge: writes straight into the folder you pick) and a file-name box. Firefox/Safari can't pick folders from a page, so the name is applied and the browser's own download setting decides the folder. |

Rules applied everywhere: default name = the real file name, else the PDF's embedded title, else `document_<id>.pdf`;
`< > : " / \ | ? *` and control characters become `_`; Windows reserved names (CON, NUL, COM1...) are prefixed; `.pdf`
is added if missing; blank = default. Folders are created if missing and tested for write access (clear message instead of
a crash). Existing files are never overwritten silently: overwrite / keep both / cancel.
After saving, **View** / **In browser** (and **Open saved file** on the web) open the file you actually saved.

## Icon
Put your icon at `assets/app.ico` (or run `python tools/make_icon.py [logo.png]`). It brands the .exe, the desktop window, and the web favicon/logo.

## Files
| File | Purpose |
|---|---|
| `pdf_extractor.py` | Engine + CLI + GUI |
| `requirements.txt` | Python dependencies |
| `run.ps1` | Mode 1: run from source (auto-creates venv) |
| `build.ps1` / `pdf_extractor.spec` | Mode 2: build `dist\PDFExtractor.exe` (uses `assets/app.ico`) |
| `server.py`, `web/index.html` | Web app (FastAPI + browser UI) |
| `requirements-web.txt`, `Dockerfile`, `render.yaml`, `fly.toml` | Web deployment |
| `assets/app.ico`, `tools/make_icon.py` | Branding |
| `DEPLOY.md` | Step-by-step web deployment |

## Mode 1 — PowerShell / Python
Requires Python 3.10+ and Microsoft Edge or Chrome (Edge ships with Windows).

```powershell
Set-ExecutionPolicy -Scope Process Bypass      # only if scripts are blocked
.\run.ps1                                      # GUI
.\run.ps1 -Url "https://example.com/page" -Deep -Open   # CLI
```
Flags: `-Headed` (show browser), `-Deep` (click download/view buttons), `-Wait 12`
(listen longer), `-Password xyz`, `-Out C:\pdfs`, `-InstallChromium` (no Edge/Chrome present).

## Mode 2 — Standalone .exe
```powershell
.\build.ps1
```
Result: `dist\PDFExtractor.exe`. Copy it anywhere; no Python needed. It uses the PC's
Edge/Chrome. (Want a fully self-contained build? Run
`python -m playwright install chromium` and set `PLAYWRIGHT_BROWSERS_PATH=0` before
building, then add Playwright's browser folder to `datas` in the spec.)

## How detection works
Network responses (CDP `Network.getResponseBody`) · blob URLs (hooked `createObjectURL`) ·
DOM/shadow-DOM/inline-script scan · download events · optional click-through.
Every candidate must contain a real `%PDF-` header, so trackers, HTML error pages and junk
redirects are discarded. Duplicates are removed by SHA-256.

## Troubleshooting
- **Nothing found:** enable Deep mode, raise `-Wait`, or show the browser and open the document yourself while it listens.
- **Needs login/captcha:** show the browser, complete it within the wait window (use `-Wait 60`).
- **Encrypted PDF:** you're prompted; skipping leaves it flagged "password required".
- **Antivirus flags the exe:** common with unsigned PyInstaller builds; keep `upx=False` and sign the exe if distributing.
