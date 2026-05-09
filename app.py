from flask import Flask, request, jsonify, send_file, render_template
from flask_cors import CORS
import os, uuid, re, glob, json, threading, time, shutil, subprocess
import requests as req_lib
from collections import defaultdict

try:
    import static_ffmpeg
    static_ffmpeg.add_paths()
except Exception:
    pass

app = Flask(__name__)
CORS(app)

DOWNLOAD_DIR = '/tmp/tiktok_cache'
FILE_TTL     = 1800
RATE_LIMIT   = 10

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

jobs          = {}
jobs_lock     = threading.Lock()
_rate_store   = defaultdict(list)
_rate_lock    = threading.Lock()


def _job_path(job_id):
    return os.path.join(DOWNLOAD_DIR, f'job_{job_id}.json')

def _save_job(job_id, job):
    try:
        with open(_job_path(job_id), 'w') as f:
            json.dump(job, f)
    except Exception:
        pass

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
                job['error']  = 'Server restarted. Please try again.'
                _save_job(job_id, job)
            if job.get('status') == 'done' and not os.path.exists(job.get('file', '')):
                os.remove(p)
                continue
            jobs[job_id] = job
        except Exception:
            pass

_load_all_jobs()


# ── tikwm API ────────────────────────────────────────────────────────────────

TIKWM = 'https://www.tikwm.com/api/'
_HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

def tikwm_info(url):
    try:
        r = req_lib.get(TIKWM, params={'url': url, 'hd': 1},
                        headers=_HEADERS, timeout=15)
        data = r.json()
        if data.get('code') == 0:
            return data.get('data'), None
        return None, data.get('msg', 'Video not found or unavailable.')
    except Exception as e:
        return None, f'Could not reach download service. Please try again.'


def download_stream(video_url, output_path, job_id):
    r = req_lib.get(video_url, stream=True, timeout=120, headers={
        **_HEADERS, 'Referer': 'https://www.tiktok.com/'
    })
    r.raise_for_status()
    total = int(r.headers.get('content-length', 0))
    done  = 0
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


# ── Helpers ───────────────────────────────────────────────────────────────────

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

def make_filename(title, ext='mp4'):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', title or 'tiktok').strip()
    name = re.sub(r'\s+', ' ', name)
    return (name[:80] or 'tiktok') + '.' + ext

def _set_job(job_id, updates):
    with jobs_lock:
        jobs[job_id].update(updates)
        _save_job(job_id, jobs[job_id])

def schedule_cleanup(job_id, path):
    def _cleanup():
        time.sleep(FILE_TTL)
        try:
            if os.path.isfile(path):  os.remove(path)
            elif os.path.isdir(path): shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass
        with jobs_lock:
            jobs.pop(job_id, None)
    threading.Thread(target=_cleanup, daemon=True).start()

def is_valid_url(url):
    return bool(re.search(r'(?:https?://)?(?:www\.|vm\.|vt\.|m\.)?tiktok\.com', url, re.I))

def normalize_url(url):
    url = url.strip()
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


# ── Worker ────────────────────────────────────────────────────────────────────

def do_download(job_id, url, title, fmt, quality):
    _set_job(job_id, {'status': 'processing', 'progress': 5})
    try:
        data, err = tikwm_info(url)
        if err or not data:
            _set_job(job_id, {'status': 'error', 'error': err or 'Could not fetch video.'})
            return

        # Pick the right source URL
        if fmt == 'mp3':
            src_url = data.get('music') or data.get('play')
        elif quality == 'sd':
            src_url = data.get('play') or data.get('hdplay')
        else:
            src_url = data.get('hdplay') or data.get('play')

        if not src_url:
            _set_job(job_id, {'status': 'error', 'error': 'No download URL found.'})
            return

        file_id  = str(uuid.uuid4())
        tmp_ext  = 'mp3' if fmt == 'mp3' and src_url.endswith('.mp3') else 'mp4'
        tmp_path = os.path.join(DOWNLOAD_DIR, f'{file_id}.{tmp_ext}')

        download_stream(src_url, tmp_path, job_id)
        _set_job(job_id, {'progress': 92})

        # If MP3 requested but we got an MP4, extract audio with ffmpeg
        if fmt == 'mp3' and tmp_ext == 'mp4':
            mp3_path = os.path.join(DOWNLOAD_DIR, f'{file_id}_audio.mp3')
            ffmpeg   = _find_ffmpeg()
            if ffmpeg:
                subprocess.run([ffmpeg, '-i', tmp_path, '-q:a', '0',
                                '-map', 'a', mp3_path, '-y'],
                               capture_output=True, timeout=120)
                os.remove(tmp_path)
                tmp_path = mp3_path
            else:
                tmp_path = tmp_path  # serve mp4 as fallback

        out_ext  = 'mp3' if fmt == 'mp3' else 'mp4'
        filename = make_filename(title or data.get('title', 'tiktok'), out_ext)
        _set_job(job_id, {'status': 'done', 'file': tmp_path,
                           'filename': filename, 'progress': 100})
        schedule_cleanup(job_id, tmp_path)

    except Exception as e:
        _set_job(job_id, {'status': 'error', 'error': 'Download failed. Please try again.'})


# ── Security headers ──────────────────────────────────────────────────────────

@app.after_request
def add_security_headers(resp):
    resp.headers['X-Frame-Options']        = 'SAMEORIGIN'
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['Referrer-Policy']        = 'strict-origin-when-cross-origin'
    return resp


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/manifest.json')
def manifest():
    return jsonify({
        "name": "TikTok Downloader",
        "short_name": "TikSave",
        "description": "Download TikTok videos without watermark",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#010101",
        "theme_color": "#fe2c55",
        "icons": []
    })

@app.route('/robots.txt')
def robots():
    return 'User-agent: *\nAllow: /\n', 200, {'Content-Type': 'text/plain'}

@app.route('/info', methods=['POST'])
def get_info():
    if not _check_rate(_client_ip()):
        return jsonify({'error': 'Too many requests. Please wait a moment.'}), 429
    data = request.get_json() or {}
    url  = normalize_url(data.get('url', '').strip())
    if not url or not is_valid_url(url):
        return jsonify({'error': 'Invalid TikTok URL — please check the link.'}), 400

    info, err = tikwm_info(url)
    if err or not info:
        return jsonify({'error': err or 'Could not fetch video info.'}), 400

    duration = info.get('duration', 0) or 0
    m, s     = divmod(int(duration), 60)
    return jsonify({
        'title':        info.get('title', '') or 'TikTok Video',
        'thumbnail':    info.get('cover', '') or info.get('origin_cover', ''),
        'duration':     f'{m}:{s:02d}' if duration else '—',
        'duration_sec': int(duration),
        'uploader':     info.get('author', {}).get('nickname', '') if isinstance(info.get('author'), dict) else '',
        'url':          url,
    })

@app.route('/start', methods=['POST'])
def start_convert():
    if not _check_rate(_client_ip()):
        return jsonify({'error': 'Too many requests. Please wait a moment.'}), 429
    data    = request.get_json() or {}
    url     = normalize_url(data.get('url', '').strip())
    title   = data.get('title', '').strip()
    fmt     = data.get('format', 'mp4')
    quality = data.get('quality', 'hd')
    if fmt not in ('mp4', 'mp3'):
        fmt = 'mp4'
    if quality not in ('hd', 'sd'):
        quality = 'hd'
    if not is_valid_url(url):
        return jsonify({'error': 'Invalid TikTok URL'}), 400

    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {'status': 'pending', 'file': None, 'filename': None,
                         'error': None, 'progress': 0}

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


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
