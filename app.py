from flask import Flask, request, jsonify, send_file, render_template
from flask_cors import CORS
import subprocess, os, uuid, json, re, glob, threading, time, shutil
import urllib.parse
from collections import defaultdict

try:
    import static_ffmpeg
    static_ffmpeg.add_paths()
except Exception:
    pass

app = Flask(__name__)
CORS(app)

DOWNLOAD_DIR = '/tmp/tiktok_cache'
YTDLP        = os.environ.get('YTDLP_PATH', 'yt-dlp')
FILE_TTL     = 1800
JOB_TIMEOUT  = 300
RATE_LIMIT   = 10

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

jobs          = {}
jobs_lock     = threading.Lock()
url_jobs      = {}
url_jobs_lock = threading.Lock()
_rate_store   = defaultdict(list)
_rate_lock    = threading.Lock()


def _update_ytdlp():
    try:
        subprocess.run([YTDLP, '--update-to', 'stable'],
                       capture_output=True, timeout=90)
    except Exception:
        pass

threading.Thread(target=_update_ytdlp, daemon=True).start()


def _job_path(job_id):
    return os.path.join(DOWNLOAD_DIR, f'job_{job_id}.json')

def _save_job(job_id, job):
    try:
        with open(_job_path(job_id), 'w') as f:
            json.dump(job, f)
    except Exception:
        pass

def _load_jobs():
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

_load_jobs()


_TIKTOK_RE = re.compile(
    r'(?:https?://)?(?:www\.|vm\.|vt\.|m\.)?tiktok\.com',
    re.IGNORECASE)

def is_valid_url(url):
    return bool(_TIKTOK_RE.search(url))

def normalize_url(url):
    url = url.strip()
    if not url.startswith('http'):
        url = 'https://' + url
    return url

def parse_ytdlp_error(stderr):
    err = (stderr or '').lower()
    if 'private' in err:
        return 'This video is private or no longer available.'
    if 'removed' in err or 'no longer available' in err:
        return 'This video has been removed.'
    if 'country' in err or 'region' in err:
        return "This video is not available in the server's region."
    if 'copyright' in err:
        return 'This video is unavailable due to copyright restrictions.'
    return 'Could not download this video. Please try another.'

def _find_ffmpeg_dir():
    p = shutil.which('ffmpeg')
    if p:
        return os.path.dirname(p)
    for d in ['/nix/var/nix/profiles/default/bin', '/run/current-system/sw/bin',
              '/usr/bin', '/usr/local/bin']:
        if os.path.isfile(os.path.join(d, 'ffmpeg')):
            return d
    nix_matches = glob.glob('/nix/store/*/bin/ffmpeg')
    if nix_matches:
        return os.path.dirname(nix_matches[0])
    return None

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
        try:
            os.remove(_job_path(job_id))
        except Exception:
            pass
        with jobs_lock:
            jobs.pop(job_id, None)
    threading.Thread(target=_cleanup, daemon=True).start()

def make_filename(title, ext='mp4'):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', title).strip()
    name = re.sub(r'\s+', ' ', name)
    return (name[:80] or 'tiktok') + '.' + ext

_IMPERSONATE = ['--impersonate', 'chrome-110']
_TIKTOK_HEADERS = [
    '--add-header', 'Referer:https://www.tiktok.com/',
    '--add-header', 'Accept-Language:en-US,en;q=0.9',
]

def build_cmd(url, output_template, quality='hd', fmt='mp4'):
    if fmt == 'mp3':
        cmd = [YTDLP, '-x', '--audio-format', 'mp3', '--audio-quality', '320K',
               '--no-playlist', '--newline'] + _IMPERSONATE + _TIKTOK_HEADERS
    else:
        if quality == 'sd':
            fmt_str = 'bestvideo[height<=480]+bestaudio/best[height<=480]/best'
        else:
            fmt_str = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'
        cmd = [YTDLP, '-f', fmt_str, '--merge-output-format', 'mp4',
               '--no-playlist', '--newline'] + _IMPERSONATE + _TIKTOK_HEADERS
    ffmpeg_dir = _find_ffmpeg_dir()
    if ffmpeg_dir:
        cmd += ['--ffmpeg-location', ffmpeg_dir]
    cmd += ['-o', output_template, url]
    return cmd


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


_PROGRESS_RE = re.compile(r'\[download\]\s+([\d.]+)%')

def do_convert(job_id, url, title=None, fmt='mp4', quality='hd'):
    _set_job(job_id, {'status': 'processing', 'progress': 0})
    file_id = str(uuid.uuid4())
    output_template = os.path.join(DOWNLOAD_DIR, f'{file_id}.%(ext)s')
    cmd = build_cmd(url, output_template, quality, fmt)
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        stderr_lines = []

        def _read_stderr():
            for line in proc.stderr:
                stderr_lines.append(line)
                m = _PROGRESS_RE.search(line)
                if m:
                    pct = min(int(float(m.group(1))), 90)
                    with jobs_lock:
                        if jobs.get(job_id, {}).get('status') == 'processing':
                            jobs[job_id]['progress'] = pct

        t = threading.Thread(target=_read_stderr, daemon=True)
        t.start()
        try:
            proc.wait(timeout=JOB_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            _set_job(job_id, {'status': 'error', 'error': 'Download timed out. Please try again.'})
            return
        t.join(timeout=5)

        if proc.returncode != 0:
            _set_job(job_id, {'status': 'error',
                               'error': parse_ytdlp_error(''.join(stderr_lines))})
            return

        files = glob.glob(os.path.join(DOWNLOAD_DIR, f'{file_id}.*'))
        if not files:
            _set_job(job_id, {'status': 'error', 'error': 'Output file not found. Please try again.'})
            return

        ext      = 'mp3' if fmt == 'mp3' else 'mp4'
        filename = make_filename(title or 'tiktok', ext)
        _set_job(job_id, {'status': 'done', 'file': files[0],
                           'filename': filename, 'progress': 100})
        schedule_cleanup(job_id, files[0])

    except Exception:
        _set_job(job_id, {'status': 'error', 'error': 'Download failed. Please try again.'})
    finally:
        with url_jobs_lock:
            url_jobs.pop(url, None)


@app.after_request
def add_security_headers(resp):
    resp.headers['X-Frame-Options']        = 'SAMEORIGIN'
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['Referrer-Policy']        = 'strict-origin-when-cross-origin'
    return resp


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
    try:
        result = subprocess.run(
            [YTDLP, '--dump-json', '--no-playlist'] + _IMPERSONATE + _TIKTOK_HEADERS + [url],
            capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return jsonify({'error': parse_ytdlp_error(result.stderr)}), 400
        info     = json.loads(result.stdout)
        duration = info.get('duration', 0) or 0
        m, s     = divmod(int(duration), 60)
        return jsonify({
            'title':        info.get('title', '') or info.get('description', 'TikTok Video'),
            'thumbnail':    info.get('thumbnail', ''),
            'duration':     f'{m}:{s:02d}' if duration else '—',
            'duration_sec': int(duration),
            'uploader':     info.get('uploader', '') or info.get('creator', ''),
            'url':          url,
        })
    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Request timed out. Please try again.'}), 504
    except Exception:
        return jsonify({'error': 'Failed to fetch video info. Please try again.'}), 500

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

    with url_jobs_lock:
        existing = url_jobs.get(url)
        if existing:
            with jobs_lock:
                st = jobs.get(existing, {}).get('status')
            if st in ('pending', 'processing'):
                return jsonify({'job_id': existing})

    job_id = str(uuid.uuid4())
    job    = {'status': 'pending', 'file': None, 'filename': None,
               'error': None, 'progress': 0}
    with jobs_lock:
        jobs[job_id] = job
        _save_job(job_id, job)
    with url_jobs_lock:
        url_jobs[url] = job_id

    threading.Thread(
        target=do_convert,
        args=(job_id, url, title or None, fmt, quality),
        daemon=True
    ).start()
    return jsonify({'job_id': job_id})

@app.route('/status/<job_id>')
def get_status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify({k: job.get(k) for k in ('status', 'error', 'filename', 'progress')})

@app.route('/download/<job_id>')
def download_file(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job or job['status'] != 'done':
        return jsonify({'error': 'File not ready — please try again.'}), 404
    path, filename = job['file'], job['filename']
    if not os.path.exists(path):
        return jsonify({'error': 'File expired. Please download again.'}), 410
    safe = re.sub(r'[^\w\s\-\.\(\)]', '', filename).strip() or 'tiktok.mp4'
    mime = 'audio/mpeg' if safe.endswith('.mp3') else 'video/mp4'
    return send_file(path, as_attachment=True, download_name=safe, mimetype=mime)


@app.route('/debug-info', methods=['POST'])
def debug_info():
    data = request.get_json() or {}
    url  = normalize_url(data.get('url', '').strip())
    result = subprocess.run(
        [YTDLP, '--dump-json', '--no-playlist'] + _IMPERSONATE + _TIKTOK_HEADERS + [url],
        capture_output=True, text=True, timeout=30)
    return jsonify({'returncode': result.returncode,
                    'stdout': result.stdout[:500],
                    'stderr': result.stderr[-1000:]})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
