# TikGet — TikTok Video Downloader

Flask + yt-dlp backend and web app for downloading TikTok videos **without watermark**.

**Live:** https://ravishing-acceptance-production-f209.up.railway.app

Also powers the [TikGet browser extension](https://github.com/khaledq84ever/tiktok-extension) — get it from [GetPack](https://getpack-production.up.railway.app).

## API
- `POST /info` `{url}` → title, thumbnail, duration
- `POST /start` `{url, format}` → `{job_id}`
- `GET /status/<job_id>` → progress / done / error
- `GET /download/<job_id>/<filename>` → file

Deploy: `railway up --ci` from this folder.
