"""Global 'Popular sounds' chart — TikTok's trending audio, refreshed once a day and
cached for ALL creators (it's global, not per-creator, so it's cheap).

Source: automation-lab/tiktok-trends-scraper (TikTok Creative Center trending songs;
pay-per-result, no monthly rental; returns name/author/soundId(string)/linkedVideoUrl/
rank/publishedVideoCount). Override the actor with APIFY_TRENDING_ACTOR.
"""

import json
import os
import threading
import time

import db
from sources import apify

TTL = int(os.environ.get("POPSOUNDS_TTL", "86400"))     # refresh at most once a day
_LOCK = threading.Lock()


def enabled():
    return bool(os.environ.get("APIFY_TOKEN", "").strip()) and \
        os.environ.get("POPSOUNDS", "1").strip().lower() not in ("0", "false", "no")


def get():
    """The cached trending-sounds list; kicks off a daily background refresh if stale."""
    raw = db.get_setting("popular_sounds")
    data = json.loads(raw) if raw else {}
    if time.time() - float(data.get("ts", 0)) > TTL:
        maybe_refresh()
    return data.get("sounds", [])


def maybe_refresh():
    """Throttled, non-blocking daily refresh (single-flight)."""
    if not enabled():
        return
    try:
        last = float(db.get_setting("popular_sounds_last") or 0)
    except (TypeError, ValueError):
        last = 0
    if time.time() - last < TTL:
        return
    db.set_setting("popular_sounds_last", str(time.time()))   # claim the slot up front
    threading.Thread(target=_refresh, daemon=True).start()


def _refresh():
    if not _LOCK.acquire(blocking=False):
        return
    try:
        sounds = fetch_trending()
        if sounds:
            db.set_setting("popular_sounds", json.dumps({"ts": time.time(), "sounds": sounds}))
            print(f"[popsounds] refreshed {len(sounds)} trending sounds")
    except Exception as e:
        print(f"[popsounds] refresh failed: {type(e).__name__}: {str(e)[:160]}")
    finally:
        _LOCK.release()


def fetch_trending():
    actor = os.environ.get("APIFY_TRENDING_ACTOR", "automation-lab/tiktok-trends-scraper")
    country = os.environ.get("POPSOUNDS_COUNTRY", "US")
    period = int(os.environ.get("POPSOUNDS_PERIOD", "7"))
    limit = int(os.environ.get("POPSOUNDS_MAX", "30"))
    items = apify._run(actor, {"trendType": "sound", "countryCode": country,
                               "period": period, "maxResults": limit})
    out = []
    for it in items:
        title = it.get("name") or it.get("title")
        if not title:
            continue
        out.append({
            "title": title,
            "author": it.get("soundAuthor") or it.get("author") or "",
            "url": it.get("linkedVideoUrl") or it.get("link") or "",
            "rank": it.get("rank"),
            "count": apify._num(it.get("publishedVideoCount") or it.get("postCount") or 0),
        })
    return out[:limit]
