"""Pull real frames out of a video for visual AI analysis.

The cover thumbnail is often a posed splash frame (or text), so instead we download
the actual video and sample a few frames across it. Needs ffmpeg/ffprobe on the box;
degrades to [] gracefully if they're missing or a download fails — the scan never
breaks (ai_trends falls back to text).
"""

import base64
import os
import shutil
import subprocess
import tempfile

import requests

_MAX_BYTES = 25 * 1024 * 1024         # don't pull more than 25MB of any one video
_UA = {"User-Agent": "Mozilla/5.0"}


def _fractions(n):
    """n sample points spread evenly across the clip (excluding the exact start/end,
    which are often black/title frames). n=3 -> .25/.5/.75, n=8 -> ~.11..~.89."""
    if n <= 1:
        return (0.5,)
    return tuple((i + 1) / (n + 1) for i in range(n))


def available():
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _duration(path):
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path], timeout=10)
        return float(out.strip())
    except Exception:
        return 0.0


def extract(video_url, n=3, timeout=25):
    """Return up to n frames from the video as base64 JPEG strings ([] on any failure)."""
    if not video_url or not available():
        return []
    tmpd = tempfile.mkdtemp(prefix="trf_")
    mp4 = os.path.join(tmpd, "v.mp4")
    frames = []
    try:
        r = requests.get(video_url, timeout=timeout, stream=True, headers=_UA)
        r.raise_for_status()
        size = 0
        with open(mp4, "wb") as f:
            for chunk in r.iter_content(65536):
                if not chunk:
                    continue
                f.write(chunk)
                size += len(chunk)
                if size > _MAX_BYTES:
                    break
        if size == 0:
            return []
        dur = _duration(mp4)
        secs = [dur * fr for fr in _fractions(n)] if dur > 0 else [0.0]
        for k, s in enumerate(secs):
            out = os.path.join(tmpd, f"f{k}.jpg")
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-ss", f"{s:.2f}", "-i", mp4,
                     "-frames:v", "1", "-vf", "scale=512:-1", out],
                    timeout=20, capture_output=True)
                if os.path.exists(out) and os.path.getsize(out) > 0:
                    with open(out, "rb") as fh:
                        frames.append(base64.standard_b64encode(fh.read()).decode())
            except Exception:
                continue
    except Exception:
        return []
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)
    return frames
