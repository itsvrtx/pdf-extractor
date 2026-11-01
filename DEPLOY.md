# Deploying PDF Extractor as a web app

Users open a URL; **your server** runs Chromium + Playwright. Nothing to install on their side.

```
Browser ──HTTPS──> FastAPI (server.py) ──> Playwright/Chromium ──> target page
   ^  live log (SSE), Download / View / Unlock            └─ PDFs saved to /data/jobs/<id>, auto-deleted
```

## 0. Add your icon
Copy your file to **`assets/app.ico`** (or `python tools/make_icon.py logo.png` to convert a PNG;
`python tools/make_icon.py` makes a placeholder). That one file feeds the web favicon, the web
header logo, the desktop `.exe` icon and the desktop window icon.

## 1. Try it locally
```powershell
.\run.ps1 -Web            # http://localhost:8000  (uses your Edge/Chrome)
```
or, with Docker (same image you'll deploy):
```bash
docker build -t pdf-extractor .
docker run --rm -p 8000:8000 -e ACCESS_TOKEN=change-me pdf-extractor
```

## 2. Deploy (pick one)
Chromium needs **~1-2 GB RAM per instance**. Keep it at **one process** (jobs are held in memory).

### A. Render (easiest)
1. Push this folder to a GitHub repo.
2. Render > **New > Blueprint** > select the repo (uses `render.yaml`).
3. After the first deploy, copy the generated `ACCESS_TOKEN` from *Environment* and share it with your users.

### B. Fly.io
```bash
fly launch --no-deploy --copy-config        # edit app name in fly.toml first
fly secrets set ACCESS_TOKEN="a-long-random-string"
fly deploy
```

### C. Google Cloud Run
```bash
gcloud run deploy pdf-extractor --source . --region us-central1 \
  --memory 2Gi --cpu 2 --timeout 300 --concurrency 4 --max-instances 1 \
  --set-env-vars TRUST_PROXY=1,ACCESS_TOKEN=a-long-random-string --allow-unauthenticated
```
(`--max-instances 1` keeps job state and files on the instance that created them.)

### D. Any VPS (Docker)
```bash
docker run -d --restart unless-stopped -p 8000:8000 -e ACCESS_TOKEN=... -e TRUST_PROXY=1 pdf-extractor
```
Put Caddy/nginx in front for HTTPS (`reverse_proxy localhost:8000`). For nginx add
`proxy_buffering off;` so the live log (SSE) streams.

### How saving works on the hosted version
The server can't (and shouldn't) write to a visitor's disk. PDFs stay in a temporary per-job folder on the server; the
**Save as...** dialog then writes them to the visitor's chosen folder through the browser (File System Access API on
Chrome/Edge) or via a normal download with the chosen file name (other browsers). The server re-sanitises every requested
file name, so a tampered request can't inject paths or header characters. Needs HTTPS (or localhost) for the folder picker.

## 3. Settings (environment variables)
| Variable | Default | Purpose |
|---|---|---|
| `ACCESS_TOKEN` | *(none)* | Gate for all API calls. **Set this on any public host.** |
| `TRUST_PROXY` | off | `1` behind a proxy so rate limits see the real client IP |
| `MAX_CONCURRENT` / `MAX_QUEUE` | 2 / 8 | Parallel browsers / waiting jobs. Raise only with more RAM |
| `RATE_PER_MIN` | 6 | New jobs per IP per minute |
| `JOB_TTL_MIN` | 30 | Minutes before files are deleted |
| `MAX_WAIT` / `MAX_JOB_SECONDS` / `MAX_PDF_MB` | 20 / 120 / 100 | Safety limits |
| `APP_TITLE` | PDF Extractor | Branding text |

## 4. Security notes (read before going public)
- A "fetch any URL" service is an SSRF target. The server blocks localhost, private/link-local ranges
  (including cloud metadata `169.254.169.254`) for the page, every redirect and every sub-request.
  Residual risk: DNS rebinding. For strict setups also deny egress to private ranges at the network/firewall level.
- Always set `ACCESS_TOKEN` (or put the app behind your SSO/VPN). Without it, anyone can use your server
  to browse the web and download files.
- Files are stored only temporarily and are not shared between users, but anyone holding a job link + token can fetch them.
- It cannot get past logins/paywalls: pages needing a login return nothing. The desktop build's
  "Show browser" mode is the way to handle those.
- Use only on content you're allowed to download.

## 5. Why not a browser extension?
A Manifest V3 extension can see the current tab's traffic, but can't run the Playwright automation,
Deep-mode clicking or server-side fetching, and it needs store review/installation, which conflicts with
"zero install". The web dashboard keeps one codebase and every feature. If you later want a one-click
shortcut, a tiny bookmarklet that opens `https://your-host/?url=<current page>` is a cheap add-on.

## Troubleshooting
- **Instance killed / "Target closed":** out of memory; use a 2 GB plan.
- **Live log stalls behind a proxy:** disable response buffering for `/api/jobs/*/events`.
- **401 in the UI:** enter the access token in the field that appears at the top.
- **Favicon missing:** `assets/app.ico` wasn't present when the image was built; add it and rebuild.
