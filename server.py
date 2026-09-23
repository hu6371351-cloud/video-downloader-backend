"""
Self-hosted YouTube (and 1000+ other sites) video download API,
powered by yt-dlp + ffmpeg. Completely free, runs on your own machine.

Install dependencies first:
    pip install yt-dlp flask flask-cors

Run with:
    python server.py

Then it listens on http://localhost:5000

HOW THIS WORKS
--------------
Modern YouTube (and several other platforms) rarely offer a single file
that already contains both video and audio above 360p. Instead, video and
audio are split into separate streams. To give you clean options like
"1080p MP4" that actually work, this server:

  1. /api/info     -> Looks up the video and returns a short, curated list
                       of quality tiers (1080p, 720p, 480p, 360p, Audio MP3,
                       Audio M4A) as links pointing back to THIS server.

  2. /api/download -> When you click one of those links, this endpoint uses
                       yt-dlp to actually download the requested quality,
                       merges video+audio with ffmpeg if needed, and streams
                       the finished file back to your browser as a normal
                       download. The temporary file is deleted afterward.
"""

import os
import glob
import shutil
import tempfile
import uuid
from urllib.parse import quote

from flask import Flask, request, jsonify, send_file, after_this_request
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)
CORS(app)

# Quality tiers offered to the user. yt-dlp's format selector automatically
# falls back to the next best available resolution if the exact one requested
# isn't offered by the source video.
VIDEO_QUALITY_TIERS = [
    {"label": "1080p (FHD)", "height": 1080},
    {"label": "720p (HD)", "height": 720},
    {"label": "480p (SD)", "height": 480},
    {"label": "360p", "height": 360},
]


def _url_encode(raw_url):
    return quote(raw_url, safe='')


@app.route('/api/info', methods=['GET', 'POST'])
def get_video_info():
    """
    Accepts a video URL via ?url=... (GET) or JSON/form body { "url": "..." } (POST).
    Returns title, thumbnail, duration, and a short curated list of quality
    tiers. Each entry links to /api/download on THIS server rather than a
    raw CDN URL, since the actual file may need to be merged first.
    """
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

    # Work out which heights are actually available for this video, so we
    # don't offer a "1080p" tier for a video that only exists in 480p, etc.
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
            # Skip tiers higher than what the source actually offers
            continue
        links.append({
            "quality": tier["label"],
            "format": "MP4",
            "isAudio": False,
            "downloadUrl": f"/api/download?url={_url_encode(video_url)}&height={tier['height']}&type=video"
        })

    # If nothing matched (very low quality source), always offer at least
    # the best available video as a fallback
    if not any(not link["isAudio"] for link in links) and available_heights:
        links.insert(0, {
            "quality": f"{max_available}p (Best Available)",
            "format": "MP4",
            "isAudio": False,
            "downloadUrl": f"/api/download?url={_url_encode(video_url)}&height={max_available}&type=video"
        })

    # Audio-only options
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

    response_payload = {
        "title": title,
        "thumbnail": thumbnail,
        "duration": duration_seconds,
        "links": links
    }

    return jsonify(response_payload)


@app.route('/api/download', methods=['GET'])
def download_video():
    """
    Actually downloads and (if needed) merges the requested quality using
    yt-dlp + ffmpeg, then streams the finished file back as an attachment.
    The temporary working folder is deleted once the response has been sent.
    """
    video_url = request.args.get('url')
    download_type = request.args.get('type', 'video')
    height = request.args.get('height', type=int)
    audio_format = request.args.get('audioFormat', 'mp3')

    if not video_url:
        return jsonify({"error": "Missing 'url' parameter"}), 400

    temp_dir = tempfile.mkdtemp(prefix="ytdl_")
    unique_name = str(uuid.uuid4())
    output_template = os.path.join(temp_dir, f"{unique_name}.%(ext)s")

    if download_type == "audio":
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'noplaylist': True,
            'format': 'bestaudio/best',
            'outtmpl': output_template,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': audio_format,
                'preferredquality': '192',
            }],
        }
    else:
        height = height or 720
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'noplaylist': True,
            'format': f'bestvideo[height<={height}]+bestaudio/best[height<={height}]',
            'merge_output_format': 'mp4',
            'outtmpl': output_template,
        }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([video_url])
    except Exception as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        return jsonify({"error": f"Failed to download video: {str(exc)}"}), 500

    # Find whatever file yt-dlp actually produced (extension may vary)
    produced_files = glob.glob(os.path.join(temp_dir, f"{unique_name}.*"))
    if not produced_files:
        shutil.rmtree(temp_dir, ignore_errors=True)
        return jsonify({"error": "Download completed but no output file was found."}), 500

    final_path = produced_files[0]
    final_filename = os.path.basename(final_path)

    @after_this_request
    def cleanup(response):
        shutil.rmtree(temp_dir, ignore_errors=True)
        return response

    return send_file(final_path, as_attachment=True, download_name=final_filename)


@app.route('/', methods=['GET'])
def health_check():
    return jsonify({"status": "ok", "message": "yt-dlp backend is running"})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
