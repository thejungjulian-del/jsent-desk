"""Free-platform trend radar — YouTube watch-accounts overperformance (no Apify).

Same detection philosophy as the Instagram scanner, applied to YouTube via the free Data API:
WATCH specific channels (the creator's YouTube watch-list), pull each channel's recent uploads,
and surface the videos beating that channel's OWN recent-upload median views — relative
overperformance, not raw size (neutralizes channel size the way IG neutralizes follower count).

Each overperforming video IS a trend, stored like an IG post (unit_type='yt_video',
channel='youtube', medium='video', example = the video with its thumbnail) so the creator's
YouTube tab renders a grid of "what's overperforming" exactly like the Instagram tab.
"""

import json
import os
import statistics
import time

import db
from sources import youtube

UNIT_TYPE = "yt_video"
OVERPERF = float(os.environ.get("YT_OVERPERF", "1.5"))       # views >= this * channel median = a hit
RECENCY_DAYS = int(os.environ.get("YT_RECENCY_DAYS", "45"))  # only surface recent overperformers
TOP_N = int(os.environ.get("YT_TOP_N", "18"))                # cap stored videos per lane
PER_CHANNEL = int(os.environ.get("YT_PER_CHANNEL", "15"))    # recent uploads to sample per channel


def enabled():
    """Live YouTube data available? (needs YOUTUBE_API_KEY)."""
    return youtube.enabled()


def _watched_channels(creator_id):
    """The YouTube channels this creator/hub watches (seeds: platform=youtube, kind=account)."""
    out = []
    for s in db.list_seeds(creator_id):
        if s.get("kind") == "account" and s.get("platform") == "youtube":
            h = (s.get("value") or "").strip().lstrip("@")
            if h:
                out.append(h)
    return list(dict.fromkeys(out))


def wants(niche, gender=None):
    """Does any creator in this niche+gender make YouTube content? (Gate the quota spend.)"""
    try:
        for m in db.creators_in_niche(niche, gender):
            fmts = m.get("formats") or []
            if isinstance(fmts, str):
                try:
                    fmts = json.loads(fmts)
                except Exception:
                    fmts = []
            if any(f in ("yt_long", "yt_short") for f in fmts):
                return True
    except Exception:
        pass
    return False


def scan(creator_id, niche, gender=None, scan_time=None):
    """Scan the watched YouTube channels for one lane, store the overperforming videos as
    trend rows under `creator_id` (the hub). Returns the stored rows."""
    scan_time = scan_time or time.time()
    if not enabled():
        return []
    handles = _watched_channels(creator_id)
    if not handles:
        print(f"[free_radar] {niche}: no YouTube channels watched — skipping")
        return []
    try:
        by_channel = youtube.channel_uploads(handles, per_channel=PER_CHANNEL)
    except Exception as e:
        print(f"[free_radar] youtube fetch failed: {type(e).__name__}: {str(e)[:120]}")
        return []

    cutoff = scan_time - RECENCY_DAYS * 86400
    hits = []
    for h, vids in by_channel.items():
        views = [v.get("views", 0) or 0 for v in vids]
        if len(views) < 3:                          # need enough history for a real baseline
            continue
        med = statistics.median(views) or 0
        if med <= 0:
            continue
        for v in vids:
            pt = v.get("post_time")
            if pt and pt < cutoff:                   # only recent overperformers count as "now"
                continue
            ratio = (v.get("views", 0) or 0) / med
            if ratio >= OVERPERF:
                hits.append((v, ratio))

    hits.sort(key=lambda x: x[1], reverse=True)
    hits = hits[:TOP_N]
    rows = []
    for v, ratio in hits:
        title = (v.get("caption") or "video").strip()
        rows.append({
            "creator_id": creator_id, "scan_time": scan_time,
            "unit_type": UNIT_TYPE, "unit_key": f"yt:{v.get('video_id')}", "unit_label": title[:90],
            "platform": "youtube", "hit_count": 1, "account_count": 1,
            "overperf_sum": ratio, "score": ratio,
            "momentum": "new", "status": "buy", "niche": niche, "medium": "video",
            "description": f"{ratio:.1f}× {v.get('author')}'s usual views",
            "examples": [{"url": v.get("url"), "cover": v.get("cover") or "",
                          "author": v.get("author") or "", "views": int(v.get("views", 0) or 0),
                          "likes": int(v.get("likes", 0) or 0)}],
            "sound": "", "your_take": "", "channel": "youtube", "platforms": ["youtube"],
        })
    for r in rows:
        db.insert_trend_snapshot(r)
    print(f"[free_radar] {niche}: {len(handles)} channels -> {len(rows)} overperforming videos")
    return rows


def latest(creator_id):
    """Most recent YouTube overperforming videos for this hub (for the creator feed's YouTube tab)."""
    return db.latest_trends_of_type(creator_id, UNIT_TYPE)
