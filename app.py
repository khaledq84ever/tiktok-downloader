import atexit
import logging
import os
import re
import glob
import json
import threading
import time
import shutil
import subprocess
import uuid
import signal
import urllib.parse
from collections import defaultdict
from datetime import datetime, timezone

import requests as req_lib
from flask import Flask, request, jsonify, send_file, render_template
from flask_cors import CORS

try:
    import static_ffmpeg
    static_ffmpeg.add_paths()
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

BASE_PATH    = os.environ.get('BASE_PATH', '')
DOWNLOAD_DIR = '/tmp/tiktok_cache'
FILE_TTL     = 1800
RATE_LIMIT   = 10
CLEANUP_INTERVAL = 300
RATE_CLEANUP_INTERVAL = 120

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

jobs          = {}
jobs_lock     = threading.Lock()
_rate_store   = defaultdict(list)
_rate_lock    = threading.Lock()
_shutdown     = threading.Event()


def _job_path(job_id):
    return os.path.join(DOWNLOAD_DIR, f'job_{job_id}.json')


def _save_job(job_id, job):
    try:
        with open(_job_path(job_id), 'w') as f:
            json.dump(job, f)
    except Exception as e:
        log.warning('Failed to save job %s: %s', job_id, e)


def _load_job_from_disk(job_id):
    try:
        with open(_job_path(job_id)) as f:
            return json.load(f)
    except Exception:
        return None


def _load_all_jobs():
    for p in glob.glob(os.path.join(DOWNLOAD_DIR, 'job_*.json')):
        try:
            with open(p) as f:
                job = json.load(f)
            job_id = os.path.basename(p)[4:-5]
            if job.get('status') in ('pending', 'processing'):
                job['status'] = 'error'
                job['error'] = 'Server restarted. Please try again.'
                _save_job(job_id, job)
            if job.get('status') == 'done' and not os.path.exists(job.get('file', '')):
                os.remove(p)
                continue
            jobs[job_id] = job
        except Exception as e:
            log.warning('Failed to load job file %s: %s', p, e)


_load_all_jobs()


YT_DLP_AVAILABLE = shutil.which('yt-dlp') is not None
COOKIE_FILE = os.environ.get('TIKTOK_COOKIE_FILE', '')
PROXY_URL = os.environ.get('PROXY_URL', '')

_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': 'https://snaptik.app/',
}


def _decode_snaptik(js):
    """Decode snaptik.app obfuscated JS response"""
    idx = js.find('eval(')
    if idx < 0:
        return None
    depth = 0
    end_idx = idx
    for i, c in enumerate(js[idx:]):
        if c == '(': depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0:
                end_idx = idx + i + 1
                break
    eval_content = js[idx:end_idx]
    m = re.search(r'"([^"]+)"\s*,(\d+)\s*,"([^"]+)"\s*,(\d+)\s*,(\d+)\s*,(\d+)\s*\)\)$', eval_content)
    if not m:
        return None
    encoded, n_charset, t_offset = m.group(1), m.group(3), int(m.group(4))
    e_base, sep_idx = int(m.group(5)), int(m.group(6))
    sep_char = n_charset[sep_idx]
    result = ''
    for part in encoded.split(sep_char):
        if not part: continue
        ns = ''.join(str(n_charset.find(c)) for c in part if n_charset.find(c) >= 0)
        if ns:
            result += chr(int(ns, e_base) - t_offset)
    return urllib.parse.unquote(result)


def tikwm_info(url):
    """Get TikTok info via tikwm.com API — returns IP-independent direct URLs
    that can be fetched from the end user's browser. Returns all variants:
    HD MP4 (no-WM), MP4 (no-WM), MP4 (with-WM), MP3.
    Matches the pattern from the YouTube downloader's /tiktok/resolve.
    """
    try:
        r = req_lib.post(
            'https://www.tikwm.com/api/',
            data={'url': url, 'hd': 1},
            headers={**_HEADERS, 'Referer': 'https://www.tikwm.com/'},
            timeout=20,
        )
        if r.status_code != 200:
            return None, f'tikwm HTTP {r.status_code}'
        body = r.json()
        if body.get('code') != 0 or not body.get('data'):
            return None, body.get('msg') or 'tikwm returned no data.'
        d = body['data']

        def _abs(u):
            if not u: return ''
            if u.startswith('http'): return u
            return 'https://www.tikwm.com' + u

        downloads = []
        if d.get('hdplay'):
            downloads.append({'label': 'HD MP4 (no watermark)', 'url': _abs(d['hdplay']), 'kind': 'video', 'ext': 'mp4'})
        if d.get('play'):
            downloads.append({'label': 'MP4 (no watermark)',    'url': _abs(d['play']),   'kind': 'video', 'ext': 'mp4'})
        if d.get('wmplay'):
            downloads.append({'label': 'MP4 (with watermark)',  'url': _abs(d['wmplay']), 'kind': 'video', 'ext': 'mp4'})
        if d.get('music'):
            downloads.append({'label': 'MP3 (audio only)',      'url': _abs(d['music']),  'kind': 'audio', 'ext': 'mp3'})
        if not downloads:
            return None, 'tikwm returned no playable URL.'

        author = d.get('author') or {}
        if isinstance(author, dict):
            author = author.get('nickname') or author.get('unique_id') or ''

        return {
            'download_url': downloads[0]['url'],
            'downloads':    downloads,
            'title':        d.get('title') or '',
            'thumbnail':    _abs(d.get('cover') or d.get('origin_cover') or ''),
            'author':       author or '',
            'duration':     int(d.get('duration') or 0),
        }, None
    except Exception as e:
        return None, str(e)


def snaptik_info(url):
    """Get TikTok info via snaptik.app API"""
    try:
        r = req_lib.get('https://snaptik.app/en2', headers=_HEADERS, timeout=15)
        token_m = re.search(r'name="token"\s*value="([^"]+)"', r.text)
        token = token_m.group(1) if token_m else ''
        r = req_lib.post('https://snaptik.app/abc2.php', data={
            'url': url, 'lang': 'en2', 'token': token,
        }, headers=_HEADERS, timeout=20)
        decoded = _decode_snaptik(r.text)
        if not decoded:
            return None, 'Could not decode snaptik response.'
        if 'showAlert' in decoded:
            err_m = re.search(r'showAlert\("([^"]+)"', decoded)
            return None, err_m.group(1) if err_m else 'TikTok server unavailable.'
        urls = re.findall(r'https?://[^\s"\'<>,;)\]]+\.(?:mp4|jpg|png)', decoded)
        if urls:
            return {'download_url': urls[0], 'source': 'snaptik'}, None
        return None, 'No download URL found.'
    except Exception as e:
        return None, str(e)


def ytdlp_info(url):
    if not YT_DLP_AVAILABLE:
        return None, 'yt-dlp not available.'
    cmd = ['yt-dlp', '--dump-json', '--no-warnings', url]
    if COOKIE_FILE and os.path.exists(COOKIE_FILE):
        cmd += ['--cookies', COOKIE_FILE]
    if PROXY_URL:
        cmd += ['--proxy', PROXY_URL]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout), None
        return None, result.stderr.strip() or 'yt-dlp failed.'
    except subprocess.TimeoutExpired:
        return None, 'yt-dlp timed out.'
    except Exception as e:
        return None, str(e)


def fetch_video_info(url):
    # tikwm first — it's TikTok-specific and returns explicit HD/SD/MP3 URLs that
    # are always direct video (or audio) files. Avoids the "audio-only file" trap
    # yt-dlp can fall into with TikTok's mixed format list.
    data0, err0 = tikwm_info(url)
    if data0:
        return data0, 'tikwm', None

    data, err = ytdlp_info(url)
    if data:
        return data, 'ytdlp', None

    data2, err2 = snaptik_info(url)
    if data2:
        return data2, 'snaptik', None

    return None, None, err0 or err or err2 or 'All download sources failed.'


def get_download_urls(data, source, fmt, quality):
    if source == 'tikwm':
        # tikwm gives us labelled streams. Pick by user's choice; fall back through
        # the list so we always return SOMETHING playable.
        downloads = data.get('downloads', []) or []
        by_label = {d['label']: d['url'] for d in downloads if d.get('url')}
        if fmt == 'mp3':
            return by_label.get('MP3 (audio only)') or data.get('download_url'), 'direct_audio'
        # Video paths: prefer H.264 over HEVC ALWAYS, regardless of HD/SD pick.
        # tikwm's "HD MP4" is HEVC (~30% browser support); plain "MP4" is H.264 720p
        # (plays everywhere). HEVC files were silently downloading as "audio only" in
        # the user's player because the video stream couldn't be decoded.
        order = ['MP4 (no watermark)', 'MP4 (with watermark)', 'HD MP4 (no watermark)']
        for label in order:
            if by_label.get(label):
                return by_label[label], None
        return data.get('download_url'), None

    if source == 'snaptik':
        url = data.get('download_url', '')
        return url or None, None

    elif source == 'ytdlp':
        if fmt == 'mp3':
            return None, 'audio'
        url = data.get('url', '')
        if url:
            return url, None
        formats = data.get('formats', [])
        best = None
        for f in formats:
            if f.get('vcodec') != 'none' and f.get('acodec') != 'none':
                if quality == 'hd' or not best:
                    best = f
                elif f.get('height', 0) < best.get('height', 0):
                    best = f
        return best.get('url') if best else data.get('url'), None

    return None, None


def _extract_title(data, source):
    if source == 'ytdlp':
        return data.get('title', '') or 'TikTok Video'
    return data.get('title', '') or 'TikTok Video'


def _extract_thumbnail(data, source):
    if source == 'ytdlp':
        return data.get('thumbnail', '')
    return data.get('thumbnail', '') or ''


def _extract_uploader(data, source):
    if source == 'ytdlp':
        return data.get('uploader', '') or data.get('channel', '')
    return data.get('author', '') or ''


def _extract_duration(data, source):
    duration = data.get('duration', 0) or 0
    m, s = divmod(int(duration), 60)
    return f'{m}:{s:02d}' if duration else '—', int(duration)


def download_stream(video_url, output_path, job_id):
    r = req_lib.get(video_url, stream=True, timeout=120, headers={
        **_HEADERS, 'Referer': 'https://www.tiktok.com/'
    })
    r.raise_for_status()
    total = int(r.headers.get('content-length', 0))
    done = 0
    with open(output_path, 'wb') as f:
        for chunk in r.iter_content(chunk_size=65536):
            if chunk:
                f.write(chunk)
                done += len(chunk)
                if total:
                    pct = min(int(done / total * 90), 90)
                    with jobs_lock:
                        if jobs.get(job_id, {}).get('status') == 'processing':
                            jobs[job_id]['progress'] = pct


def _find_ffmpeg():
    p = shutil.which('ffmpeg')
    if p:
        return p
    for d in ['/nix/var/nix/profiles/default/bin', '/usr/bin', '/usr/local/bin']:
        fp = os.path.join(d, 'ffmpeg')
        if os.path.isfile(fp):
            return fp
    nix = glob.glob('/nix/store/*/bin/ffmpeg')
    return nix[0] if nix else None


def make_filename(title, ext='mp4', url=''):
    """Build a filesystem-safe filename. ALWAYS appends a short id derived from
    the source URL so two videos with no caption don't both become 'tiktok.mp4'
    and overwrite each other in the user's Downloads folder."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', title or '').strip()
    name = re.sub(r'\s+', ' ', name)[:60]
    # Extract a stable short id from the TikTok URL (the numeric video id, or
    # vm.tiktok.com short code). Falls back to a random 6-char tag.
    vid = ''
    if url:
        m = re.search(r'/video/(\d+)', url) or re.search(r'/(?:v|t)/(\w+)', url) \
            or re.search(r'tiktok\.com/(\w{8,})', url)
        if m:
            vid = m.group(1)[-8:]
    if not vid:
        vid = uuid.uuid4().hex[:6]
    base = (name + ' ' if name else '') + vid
    return base[:80] + '.' + ext


def _set_job(job_id, updates):
    with jobs_lock:
        jobs[job_id].update(updates)
        _save_job(job_id, jobs[job_id])


def schedule_cleanup(job_id, path):
    def _cleanup():
        if _shutdown.wait(FILE_TTL):
            return
        try:
            if os.path.isfile(path):
                os.remove(path)
            elif os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass
        with jobs_lock:
            jobs.pop(job_id, None)
    threading.Thread(target=_cleanup, daemon=True).start()


def is_valid_url(url):
    return bool(re.search(
        r'(?:https?://)?(?:www\.|vm\.|vt\.|m\.)?tiktok\.com',
        url, re.I
    ))


def normalize_url(url):
    url = url.strip().split('?')[0]
    if not url.startswith('http'):
        url = 'https://' + url
    return url


def _check_rate(ip):
    now = time.time()
    with _rate_lock:
        _rate_store[ip] = [t for t in _rate_store[ip] if now - t < 60]
        if len(_rate_store[ip]) >= RATE_LIMIT:
            return False
        _rate_store[ip].append(now)
        return True


def _client_ip():
    return (request.headers.get('X-Forwarded-For', '')
            .split(',')[0].strip() or request.remote_addr or 'unknown')


def do_download(job_id, url, title, fmt, quality):
    _set_job(job_id, {'status': 'processing', 'progress': 5})
    try:
        data, source, err = fetch_video_info(url)
        if err or not data:
            _set_job(job_id, {'status': 'error', 'error': err or 'Could not fetch video.'})
            return

        _set_job(job_id, {'progress': 15})

        src_url, audio_only = get_download_urls(data, source, fmt, quality)

        if source == 'ytdlp' and audio_only:
            file_id = str(uuid.uuid4())
            mp3_path = os.path.join(DOWNLOAD_DIR, f'{file_id}.mp3')
            try:
                subprocess.run(
                    ['yt-dlp', '-x', '--audio-format', 'mp3',
                     '-o', mp3_path, url, '--no-warnings'],
                    capture_output=True, timeout=180
                )
                actual = mp3_path
                if not os.path.exists(actual):
                    actual = mp3_path + '.mp3'
                if not os.path.exists(actual):
                    _set_job(job_id, {'status': 'error', 'error': 'Audio extraction failed.'})
                    return
                filename = make_filename(title or _extract_title(data, source) or 'tiktok', 'mp3', url)
                _set_job(job_id, {'status': 'done', 'file': actual,
                                   'filename': filename, 'progress': 100})
                schedule_cleanup(job_id, actual)
                return
            except Exception as e:
                _set_job(job_id, {'status': 'error', 'error': 'Audio download failed.'})
                return

        if not src_url:
            _set_job(job_id, {'status': 'error', 'error': 'No download URL found.'})
            return

        file_id = str(uuid.uuid4())
        out_ext = 'mp3' if fmt == 'mp3' else 'mp4'
        tmp_path = os.path.join(DOWNLOAD_DIR, f'{file_id}.{out_ext}')

        if source == 'ytdlp':
            yt_path = os.path.join(DOWNLOAD_DIR, f'{file_id}_yt.%(ext)s')
            try:
                # Force a stream that ACTUALLY contains video. Plain `-f best` on TikTok
                # can match an audio-only entry, which is why downloads were ending up
                # as audio-only files. `--merge-output-format mp4` guarantees the merged
                # extension is .mp4 when bv*+ba is selected.
                if quality == 'sd':
                    fmt_sel = 'bv*[height<=480]+ba/b[height<=480][vcodec!=none]/bv*+ba/b[vcodec!=none]'
                else:
                    fmt_sel = 'bv*+ba/b[vcodec!=none]/best'
                subprocess.run(
                    ['yt-dlp', '-f', fmt_sel, '--merge-output-format', 'mp4',
                     '-o', yt_path, url, '--no-warnings'],
                    capture_output=True, timeout=180
                )
                # Prefer the .mp4 if present; otherwise pick a file that actually has video.
                expected = os.path.join(DOWNLOAD_DIR, f'{file_id}_yt.mp4')
                if not os.path.exists(expected):
                    candidates = sorted(
                        glob.glob(os.path.join(DOWNLOAD_DIR, f'{file_id}_yt.*')),
                        # Prefer mp4 / mkv / webm over audio-only m4a / mp3 / opus / aac.
                        key=lambda p: (0 if p.endswith(('.mp4', '.mkv', '.webm', '.mov')) else 1, p),
                    )
                    expected = candidates[0] if candidates else expected
                if not os.path.exists(expected):
                    _set_job(job_id, {'status': 'error', 'error': 'Download via yt-dlp failed.'})
                    return
                # Sanity-check: if the file landed with an audio-only extension, treat as failure
                # so the snaptik fallback can take over.
                if expected.lower().endswith(('.m4a', '.mp3', '.opus', '.aac', '.ogg', '.wav')):
                    try: os.remove(expected)
                    except OSError: pass
                    raise RuntimeError('yt-dlp returned audio-only file')
                _set_job(job_id, {'progress': 90})
                if fmt == 'mp3' and out_ext == 'mp4':
                    mp3_path = os.path.join(DOWNLOAD_DIR, f'{file_id}_audio.mp3')
                    ffmpeg = _find_ffmpeg()
                    if ffmpeg:
                        subprocess.run(
                            [ffmpeg, '-i', expected, '-q:a', '0',
                             '-map', 'a', mp3_path, '-y'],
                            capture_output=True, timeout=120
                        )
                        os.remove(expected)
                        out_path = mp3_path
                    else:
                        out_path = expected
                else:
                    out_path = expected
                filename = make_filename(title or _extract_title(data, source) or 'tiktok', out_ext, url)
                _set_job(job_id, {'status': 'done', 'file': out_path,
                                   'filename': filename, 'progress': 100})
                schedule_cleanup(job_id, out_path)
                return
            except Exception as e:
                # yt-dlp couldn't produce a video file (e.g., it returned audio only,
                # or download itself failed). Fall back to snaptik so the user still
                # gets the video they asked for.
                logging.warning(f'yt-dlp video path failed ({e}); trying snaptik fallback')
                snap_data, snap_err = snaptik_info(url)
                if not snap_data or not snap_data.get('download_url'):
                    _set_job(job_id, {'status': 'error',
                                       'error': 'Video download failed (yt-dlp returned audio only, snaptik fallback also failed).'})
                    return
                source = 'snaptik'
                data = snap_data
                src_url = snap_data['download_url']
                # Fall through to the snaptik download path below.

        download_stream(src_url, tmp_path, job_id)
        _set_job(job_id, {'progress': 92})

        if fmt == 'mp3' and out_ext == 'mp4':
            mp3_path = os.path.join(DOWNLOAD_DIR, f'{file_id}_audio.mp3')
            ffmpeg = _find_ffmpeg()
            if ffmpeg:
                subprocess.run(
                    [ffmpeg, '-i', tmp_path, '-q:a', '0',
                     '-map', 'a', mp3_path, '-y'],
                    capture_output=True, timeout=120
                )
                os.remove(tmp_path)
                tmp_path = mp3_path
            else:
                log.warning('ffmpeg not found, serving mp4 as fallback for mp3 request')

        filename = make_filename(title or _extract_title(data, source) or 'tiktok', out_ext, url)
        _set_job(job_id, {'status': 'done', 'file': tmp_path,
                           'filename': filename, 'progress': 100})
        schedule_cleanup(job_id, tmp_path)

    except Exception as e:
        log.error('Download failed for job %s: %s', job_id, e)
        _set_job(job_id, {'status': 'error', 'error': 'Download failed. Please try again.'})


def _cleanup_loop():
    while not _shutdown.is_set():
        _shutdown.wait(CLEANUP_INTERVAL)
        if _shutdown.is_set():
            break
        now = time.time()
        removed = 0
        with jobs_lock:
            stale = []
            for job_id, job in list(jobs.items()):
                if job.get('status') == 'done' and job.get('file'):
                    try:
                        mtime = os.path.getmtime(job['file'])
                        if now - mtime > FILE_TTL:
                            try:
                                if os.path.isfile(job['file']):
                                    os.remove(job['file'])
                            except Exception:
                                pass
                            stale.append(job_id)
                    except OSError:
                        stale.append(job_id)
                elif job.get('status') == 'error':
                    age = job.get('_created', now)
                    if now - age > 3600:
                        stale.append(job_id)
            for jid in stale:
                jobs.pop(jid, None)
                jp = _job_path(jid)
                try:
                    if os.path.exists(jp):
                        os.remove(jp)
                except Exception:
                    pass
                removed += 1
        if removed:
            log.info('Cleanup: removed %d stale jobs', removed)


def _rate_cleanup_loop():
    while not _shutdown.is_set():
        _shutdown.wait(RATE_CLEANUP_INTERVAL)
        if _shutdown.is_set():
            break
        now = time.time()
        with _rate_lock:
            for ip in list(_rate_store.keys()):
                _rate_store[ip] = [t for t in _rate_store[ip] if now - t < 60]
                if not _rate_store[ip]:
                    del _rate_store[ip]


def _signal_handler(signum, frame):
    log.info('Received signal %d, shutting down...', signum)
    _shutdown.set()


signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)

atexit.register(lambda: _shutdown.set())

cleanup_thread = threading.Thread(target=_cleanup_loop, daemon=True, name='job-cleanup')
cleanup_thread.start()
rate_cleanup_thread = threading.Thread(target=_rate_cleanup_loop, daemon=True, name='rate-cleanup')
rate_cleanup_thread.start()


@app.after_request
def add_security_headers(resp):
    resp.headers['X-Frame-Options'] = 'SAMEORIGIN'
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    resp.headers['X-XSS-Protection'] = '0'
    if resp.content_type and 'text/html' in resp.content_type:
        resp.headers['Content-Security-Policy'] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: https:; "
            "connect-src 'self'; "
        )
    return resp


@app.route('/health')
def health():
    deps = {
        'ffmpeg': _find_ffmpeg() is not None,
        'yt-dlp': YT_DLP_AVAILABLE,
        'snaptik_api': True,
        'download_dir': os.path.isdir(DOWNLOAD_DIR),
    }
    all_ok = all(deps.values())
    status_code = 200 if all_ok else 503
    return jsonify({
        'status': 'ok' if all_ok else 'degraded',
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'uptime': time.time() - _start_time,
        'active_jobs': len(jobs),
        'dependencies': deps,
        'version': '2.0',
    }), status_code


@app.route('/')
def index():
    resp = app.make_response(render_template('index.html', base_path=BASE_PATH))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma']        = 'no-cache'
    resp.headers['Expires']       = '0'
    return resp


@app.route('/manifest.json')
def manifest():
    return jsonify({
        "name": "TikTok Downloader",
        "short_name": "TikSave",
        "description": "Download TikTok videos without watermark",
        "start_url": BASE_PATH + "/",
        "display": "standalone",
        "background_color": "#010101",
        "theme_color": "#fe2c55",
        "icons": []
    })


@app.route('/robots.txt')
def robots():
    return 'User-agent: *\nAllow: /\n', 200, {'Content-Type': 'text/plain'}


# Self-unregistering service worker — defends against stale SW from old deploys
# that can intercept /download and break the blob-fetch on mobile.
@app.route('/sw.js')
def sw_js():
    return ("self.addEventListener('install',e=>self.skipWaiting());"
            "self.addEventListener('activate',e=>e.waitUntil("
            "self.registration.unregister().then(()=>self.clients.matchAll())"
            ".then(c=>c.forEach(x=>x.navigate(x.url)))));",
            200, {'Content-Type': 'application/javascript',
                  'Cache-Control': 'no-store'})


@app.route('/info', methods=['POST'])
def get_info():
    if not _check_rate(_client_ip()):
        return jsonify({'error': 'Too many requests. Please wait a moment.'}), 429
    data = request.get_json() or {}
    url = normalize_url(data.get('url', '').strip())
    if not url or not is_valid_url(url):
        return jsonify({'error': 'Invalid TikTok URL — please check the link.'}), 400

    info, source, err = fetch_video_info(url)
    if err or not info:
        return jsonify({'error': err or 'Could not fetch video info.'}), 400

    duration_str, duration_sec = _extract_duration(info, source)
    return jsonify({
        'title': _extract_title(info, source),
        'thumbnail': _extract_thumbnail(info, source),
        'duration': duration_str,
        'duration_sec': duration_sec,
        'uploader': _extract_uploader(info, source),
        'url': url,
    })


@app.route('/direct', methods=['POST'])
def get_direct():
    """Fast path: paste TikTok URL → external service → return a direct download
    URL the user's browser can fetch. Tries tikwm (IP-independent), snaptik, then
    yt-dlp; the yt-dlp URL is signed and IP-bound, so we wrap it in a /stream
    proxy URL so the user's browser can still download it through us.
    """
    if not _check_rate(_client_ip()):
        return jsonify({'error': 'Too many requests. Please wait a moment.'}), 429
    data = request.get_json() or {}
    url = normalize_url(data.get('url', '').strip())
    if not url or not is_valid_url(url):
        return jsonify({'error': 'Invalid TikTok URL — please check the link.'}), 400

    direct_url = None
    downloads = []
    title = ''
    thumbnail = ''
    uploader = ''
    duration_sec = 0
    source = None
    last_err = None

    info, err = tikwm_info(url)
    if info:
        direct_url = info['download_url']
        downloads  = info.get('downloads') or []
        title      = info.get('title') or ''
        thumbnail  = info.get('thumbnail') or ''
        uploader   = info.get('author') or ''
        duration_sec = int(info.get('duration') or 0)
        source = 'tikwm'
    else:
        last_err = err

    if not direct_url:
        info, err = snaptik_info(url)
        if info:
            direct_url = info['download_url']
            source = 'snaptik'
        else:
            last_err = err or last_err

    if not direct_url:
        info, err = ytdlp_info(url)
        if info:
            inner = info.get('url') or ''
            for f in info.get('formats', []) or []:
                if f.get('vcodec') != 'none' and f.get('acodec') != 'none' and f.get('url'):
                    inner = f['url']; break
            if inner:
                direct_url = (BASE_PATH + '/stream?u=' +
                              urllib.parse.quote(inner, safe='') +
                              '&n=' + urllib.parse.quote(make_filename(info.get('title') or 'tiktok', 'mp4', url), safe=''))
                title     = info.get('title') or ''
                thumbnail = info.get('thumbnail') or ''
                uploader  = info.get('uploader') or info.get('channel') or ''
                duration_sec = int(info.get('duration') or 0)
                source = 'ytdlp+proxy'
        else:
            last_err = err or last_err

    if not direct_url:
        return jsonify({'error': last_err or 'Could not extract video.'}), 502

    m, s = divmod(duration_sec, 60)
    duration_str = f'{m}:{s:02d}' if duration_sec else '—'
    title = title or 'TikTok Video'
    if not downloads:
        downloads = [{
            'label': 'Download MP4',
            'url':   direct_url,
            'kind':  'video',
            'ext':   'mp4',
        }]
    return jsonify({
        'download_url': direct_url,
        'downloads':    downloads,
        'title':        title,
        'thumbnail':    thumbnail,
        'uploader':     uploader,
        'duration':     duration_str,
        'duration_sec': duration_sec,
        'filename':     make_filename(title, 'mp4', url),
        'source':       source,
    })


@app.route('/stream')
def stream_proxy():
    """Stream a (typically IP-bound) source URL through this server with the
    right TikTok referer + Content-Disposition: attachment, so the user's
    browser actually downloads it.
    """
    src = request.args.get('u', '')
    name = request.args.get('n', 'tiktok.mp4')
    if not src.startswith(('http://', 'https://')):
        return jsonify({'error': 'Invalid source URL'}), 400
    host = urllib.parse.urlparse(src).hostname or ''
    if not any(host.endswith(h) for h in (
        'tiktokcdn.com', 'tiktokcdn-us.com', 'tiktokcdn-eu.com',
        'tiktokv.com', 'tiktok.com', 'byteoversea.com', 'muscdn.com',
        # tikwm sometimes returns relative URLs that get prefixed with tikwm.com,
        # so allow it through. The handler still sets Content-Disposition: attachment.
        'tikwm.com',
    )):
        return jsonify({'error': 'Source host not allowed'}), 400

    safe_name = re.sub(r'[^\w\s\-\.\(\)]', '', name).strip() or 'tiktok.mp4'

    def _gen():
        with req_lib.get(src, stream=True, timeout=120, headers={
            **_HEADERS, 'Referer': 'https://www.tiktok.com/'
        }) as r:
            r.raise_for_status()
            for chunk in r.iter_content(chunk_size=65536):
                if chunk:
                    yield chunk

    from flask import Response
    return Response(_gen(), mimetype='video/mp4', headers={
        'Content-Disposition': f'attachment; filename="{safe_name}"',
        'Cache-Control': 'no-store',
    })


@app.route('/start', methods=['POST'])
def start_convert():
    if not _check_rate(_client_ip()):
        return jsonify({'error': 'Too many requests. Please wait a moment.'}), 429
    data = request.get_json() or {}
    url = normalize_url(data.get('url', '').strip())
    title = data.get('title', '').strip()
    fmt = data.get('format', 'mp4')
    quality = data.get('quality', 'hd')
    if fmt not in ('mp4', 'mp3'):
        fmt = 'mp4'
    if quality not in ('hd', 'sd'):
        quality = 'hd'
    if not is_valid_url(url):
        return jsonify({'error': 'Invalid TikTok URL'}), 400

    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {
            'status': 'pending', 'file': None, 'filename': None,
            'error': None, 'progress': 0, '_created': time.time(),
        }

    threading.Thread(
        target=do_download,
        args=(job_id, url, title or None, fmt, quality),
        daemon=True
    ).start()
    return jsonify({'job_id': job_id})


@app.route('/status/<job_id>')
def get_status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        job = _load_job_from_disk(job_id)
        if job:
            with jobs_lock:
                jobs[job_id] = job
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify({k: job.get(k) for k in ('status', 'error', 'filename', 'progress')})


@app.route('/download/<job_id>')
def download_file(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        job = _load_job_from_disk(job_id)
        if job:
            with jobs_lock:
                jobs[job_id] = job
    if not job or job['status'] != 'done':
        return jsonify({'error': 'File not ready — please try again.'}), 404
    path, filename = job['file'], job['filename']
    if not os.path.exists(path):
        return jsonify({'error': 'File expired. Please download again.'}), 410
    safe = re.sub(r'[^\w\s\-\.\(\)]', '', filename).strip() or 'tiktok.mp4'
    mime = 'audio/mpeg' if safe.endswith('.mp3') else 'video/mp4'
    return send_file(path, as_attachment=True, download_name=safe, mimetype=mime)


_start_time = time.time()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', '').lower() in ('1', 'true')
    log.info('Starting TikSave v2.0 on port %d', port)
    app.run(host='0.0.0.0', port=port, debug=debug)
