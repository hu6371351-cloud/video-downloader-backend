"""
Universal Video Downloader — Flask Backend (Streaming Architecture)
=====================================================================

Designed to run safely on Render's free tier (512 MB RAM, 0.1 CPU).

CRITICAL MEMORY DESIGN
-----------------------
This backend NEVER downloads, saves, or buffers a full video file to disk
or RAM. Every download request is streamed chunk-by-chunk directly from
the source (or through ffmpeg, chunk-by-chunk) straight into the HTTP
response. Memory usage stays flat (a few MB) regardless of video length.

Two delivery paths, chosen automatically per request:

  1. DIRECT PASSTHROUGH (low CPU, zero processing)
     Used when the requested quality already exists as a single combined
     video+audio file at the source (common for 360p and below). We open
     a streaming GET request to that URL with requests(stream=True) and
     forward 1 MB chunks directly to the client as they arrive.

  2. FFMPEG REMUX PIPE (used only when video+audio are separate streams)
     Modern YouTube rarely offers combined files above 360p — video and
     audio come as two separate streams. In that case we run ffmpeg as a
     subprocess with BOTH source URLs as inputs and 'pipe:1' (stdout) as
     the output. ffmpeg reads, remuxes, and writes progressively; we read
     ffmpeg's stdout in small chunks and forward them immediately. No
     temp file is ever created. To keep this fast on 0.1 CPU, we request
     H.264 video + AAC audio specifically so ffmpeg can COPY the streams
     (just repackaging) instead of re-encoding them, which would be far
     too slow on a free-tier CPU.

Install dependencies:
    pip install -r requirements.txt

Run locally:
    python app.py
    -> listens on http://localhost:5000
"""

import subprocess
from urllib.parse import quote

import requests
import yt_dlp
from flask import Flask, Response, jsonify, request, stream_with_context
from flask_cors import CORS

app = Flask(__name__)
CORS(app)  # Allows a frontend hosted on a different domain (e.g. a Render Static Site) to call this API

CHUNK_SIZE = 1024 * 1024  # 1 MB, per the streaming spec

# Quality tiers offered to the user. yt-dlp automatically falls back to the
# next best available resolution if the exact one requested isn't offered.
VIDEO_QUALITY_TIERS = [
    {"label": "1080p (FHD)", "height": 1080},
    {"label": "720p (HD)", "height": 720},
    {"label": "480p (SD)", "height": 480},
    {"label": "360p", "height": 360},
]


def _url_encode(raw_url):
    return quote(raw_url, safe='')


def _build_headers_string(http_headers):
    """Convert yt-dlp's http_headers dict into ffmpeg's -headers CRLF string format."""
    if not http_headers:
        return ""
    lines = [f"{key}: {value}" for key, value in http_headers.items()]
    return "\r\n".join(lines) + "\r\n"


# ---------------------------------------------------------------------------
# /api/info — look up a video and return curated quality options
# ---------------------------------------------------------------------------
@app.route('/api/info', methods=['GET', 'POST'])
def get_video_info():
    video_url = request.args.get('url')
    if not video_url and request.is_json:
        video_url = (request.json or {}).get('url')
    if not video_url:
        video_url = request.form.get('url')

    if not video_url:
        return jsonify({"error": "Missing 'url' parameter"}), 400

    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'noplaylist': True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
    except Exception as exc:
        return jsonify({"error": f"Failed to process video: {str(exc)}"}), 500

    title = info.get('title', 'Untitled Video')
    thumbnail = info.get('thumbnail', '')
    duration_seconds = info.get('duration')

    available_heights = set()
    for fmt in info.get('formats', []):
        has_video = fmt.get('vcodec') not in (None, 'none')
        height = fmt.get('height')
        if has_video and height:
            available_heights.add(height)

    max_available = max(available_heights) if available_heights else 0

    links = []
    for tier in VIDEO_QUALITY_TIERS:
        if max_available and tier["height"] > max_available:
            continue
        links.append({
            "quality": tier["label"],
            "format": "MP4",
            "isAudio": False,
            "downloadUrl": f"/api/download?url={_url_encode(video_url)}&height={tier['height']}&type=video"
        })

    if not any(not link["isAudio"] for link in links) and available_heights:
        links.insert(0, {
            "quality": f"{max_available}p (Best Available)",
            "format": "MP4",
            "isAudio": False,
            "downloadUrl": f"/api/download?url={_url_encode(video_url)}&height={max_available}&type=video"
        })

    links.append({
        "quality": "MP3 Audio",
        "format": "MP3",
        "isAudio": True,
        "downloadUrl": f"/api/download?url={_url_encode(video_url)}&type=audio&audioFormat=mp3"
    })
    links.append({
        "quality": "M4A Audio (Original)",
        "format": "M4A",
        "isAudio": True,
        "downloadUrl": f"/api/download?url={_url_encode(video_url)}&type=audio&audioFormat=m4a"
    })

    return jsonify({
        "title": title,
        "thumbnail": thumbnail,
        "duration": duration_seconds,
        "links": links
    })


# ---------------------------------------------------------------------------
# /api/download — stream the actual file, never touching disk or full RAM
# ---------------------------------------------------------------------------
@app.route('/api/download', methods=['GET'])
def download_video():
    video_url = request.args.get('url')
    download_type = request.args.get('type', 'video')
    height = request.args.get('height', type=int) or 720
    audio_format = request.args.get('audioFormat', 'mp3')

    if not video_url:
        return jsonify({"error": "Missing 'url' parameter"}), 400

    if download_type == 'audio':
        return _handle_audio_download(video_url, audio_format)
    return _handle_video_download(video_url, height)


def _resolve_formats(video_url, format_selector):
    """Ask yt-dlp which format(s) match the selector, without downloading anything.
    Returns a list of format dicts, each containing a direct 'url' and 'http_headers'."""
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'noplaylist': True,
        'format': format_selector,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(video_url, download=False)

    if info.get('requested_formats'):
        return info['requested_formats']
    return [info]


def _handle_video_download(video_url, height):
    # Prefer H.264 video + AAC audio so ffmpeg can COPY (remux) instead of
    # re-encoding — critical for staying usable on a 0.1 CPU free instance.
    # Try, in order: (1) an already-combined MP4 at this height — the common
    # case for TikTok, Instagram, and low-res YouTube; (2) an already-combined
    # file in any format; (3) a video+audio merge preferring H.264/AAC so
    # ffmpeg can copy instead of re-encode — the common case for YouTube at
    # 480p+; (4) a merge with whatever codecs are available; (5) an absolute
    # last-resort fallback so we almost always find something playable.
    format_selector = (
        f'best[height<={height}][ext=mp4]/'
        f'best[height<={height}]/'
        f'bestvideo[height<={height}][vcodec^=avc1]+bestaudio[acodec^=mp4a]/'
        f'bestvideo[height<={height}]+bestaudio/'
        f'best'
    )

    try:
        formats = _resolve_formats(video_url, format_selector)
    except Exception as exc:
        return jsonify({"error": f"Could not resolve video formats: {str(exc)}"}), 500

    if len(formats) == 1:
        # A single already-combined (or video-only) file — stream it directly,
        # no ffmpeg needed at all.
        fmt = formats[0]
        return _stream_passthrough(fmt['url'], fmt.get('http_headers', {}), 'video.mp4', 'video/mp4')

    # Two separate streams (video-only + audio-only) — remux via ffmpeg pipe
    video_fmt, audio_fmt = formats[0], formats[1]
    if video_fmt.get('vcodec') in (None, 'none'):
        video_fmt, audio_fmt = audio_fmt, video_fmt  # ensure correct order

    return _stream_ffmpeg_merge(video_fmt, audio_fmt)


def _handle_audio_download(video_url, audio_format):
    format_selector = 'bestaudio[acodec^=mp4a]/bestaudio/best'

    try:
        formats = _resolve_formats(video_url, format_selector)
    except Exception as exc:
        return jsonify({"error": f"Could not resolve audio format: {str(exc)}"}), 500

    fmt = formats[0]
    source_ext = fmt.get('ext', 'm4a')

    if audio_format == 'mp3' and source_ext != 'mp3':
        return _stream_ffmpeg_audio_convert(fmt, target_format='mp3')

    # Original format requested, or source is already the target format —
    # stream it directly with no conversion needed.
    mimetype = 'audio/mp4' if source_ext in ('m4a', 'mp4a') else f'audio/{source_ext}'
    filename = f'audio.{source_ext}'
    return _stream_passthrough(fmt['url'], fmt.get('http_headers', {}), filename, mimetype)


def _stream_passthrough(source_url, http_headers, filename, mimetype):
    """Path 1: open a streaming GET to the source and forward chunks directly.
    Nothing is ever fully loaded into memory — only one CHUNK_SIZE buffer at a time."""

    def generate():
        with requests.get(source_url, headers=http_headers, stream=True, timeout=30) as upstream:
            upstream.raise_for_status()
            for chunk in upstream.iter_content(chunk_size=CHUNK_SIZE):
                if chunk:
                    yield chunk

    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return Response(stream_with_context(generate()), mimetype=mimetype, headers=headers)


def _stream_ffmpeg_merge(video_fmt, audio_fmt):
    """Path 2: run ffmpeg with both source URLs as inputs, streaming its
    stdout straight to the client. ffmpeg does the reading from the network
    itself — we only ever hold one small chunk of its output at a time."""

    video_headers = _build_headers_string(video_fmt.get('http_headers', {}))
    audio_headers = _build_headers_string(audio_fmt.get('http_headers', {}))

    cmd = ['ffmpeg', '-loglevel', 'error']
    if video_headers:
        cmd += ['-headers', video_headers]
    cmd += ['-i', video_fmt['url']]
    if audio_headers:
        cmd += ['-headers', audio_headers]
    cmd += ['-i', audio_fmt['url']]
    cmd += [
        '-map', '0:v:0', '-map', '1:a:0',
        '-c:v', 'copy', '-c:a', 'aac', '-b:a', '160k',
        '-movflags', 'frag_keyframe+empty_moov+faststart',
        '-f', 'mp4', 'pipe:1'
    ]

    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def generate():
        try:
            while True:
                chunk = process.stdout.read(CHUNK_SIZE)
                if not chunk:
                    break
                yield chunk
        finally:
            process.stdout.close()
            process.wait()

    headers = {"Content-Disposition": 'attachment; filename="video.mp4"'}
    return Response(stream_with_context(generate()), mimetype='video/mp4', headers=headers)


def _stream_ffmpeg_audio_convert(audio_fmt, target_format='mp3'):
    """Same streaming-pipe approach as above, but for audio-only conversion
    (e.g. source is Opus/WebM, target is MP3)."""

    audio_headers = _build_headers_string(audio_fmt.get('http_headers', {}))

    cmd = ['ffmpeg', '-loglevel', 'error']
    if audio_headers:
        cmd += ['-headers', audio_headers]
    cmd += ['-i', audio_fmt['url']]
    cmd += ['-vn', '-acodec', 'libmp3lame', '-b:a', '192k', '-f', 'mp3', 'pipe:1']

    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def generate():
        try:
            while True:
                chunk = process.stdout.read(CHUNK_SIZE)
                if not chunk:
                    break
                yield chunk
        finally:
            process.stdout.close()
            process.wait()

    headers = {"Content-Disposition": f'attachment; filename="audio.{target_format}"'}
    return Response(stream_with_context(generate()), mimetype=f'audio/{target_format}', headers=headers)


@app.route('/', methods=['GET'])
def health_check():
    return jsonify({"status": "ok", "message": "Streaming video backend is running"})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
