# Work Notes — TikTok Downloader backend

## 2026-06-03 — Backend was DOWN; redeployed

**Symptom:** TikGet extension failed; backend root returned Railway's
`404 "Application not found"` (no app running — the deployment was gone, same
pattern as the 2026-05-31 mass restore).

**Action:** `railway up` from this folder to redeploy the existing code.
**No code changes** — the extension and backend code were fine; the service just
wasn't deployed.

**Verified:** `/health` → `status: ok` (v2.0, snaptik_api + yt-dlp present);
full E2E on a public video → **2.95 MB MP4**.
**Shipped:** Railway `ravishing-acceptance` SUCCESS · GitHub `master` (no new commit).
