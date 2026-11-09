"""YouTube long-form videos source — the 'what to make a video about' lane.

Real data via the free YouTube Data API v3 (set YOUTUBE_API_KEY); demo data
otherwise. The trend unit here is a TOPIC (the niche keyword a video ranks for),
not an audio — so an overperforming topic means "make a video about this".
"""

import datetime
import hashlib
import os
import random
import time

import requests

API = "https://www.googleapis.com/youtube/v3"


def enabled():
    return bool(os.environ.get("YOUTUBE_API_KEY", "").strip())


def fetch(seeds, scan_index=0):
    topics = [s["value"] for s in seeds] or []
    if not topics:
        return []
    if enabled():
        try:
            vids = _live(topics)
            if vids:
                return vids
        except Exception:
            pass
    return _demo(topics, scan_index)


def _iso(s):
    try:
        return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _channel_subs(channel_ids, key):
    """Batch-fetch subscriber counts for channels (up to 50 ids per call, 1 quota unit each).
    Returns {channel_id: subscriber_count}. Channels that hide their sub count -> 0."""
    subs = {}
    ids = [c for c in dict.fromkeys(channel_ids) if c]     # de-dupe, preserve order
    for i in range(0, len(ids), 50):
        batch = ids[i:i + 50]
        r = requests.get(f"{API}/channels", params={
            "key": key, "id": ",".join(batch), "part": "statistics",
        }, timeout=60)
        r.raise_for_status()
        for c in r.json().get("items", []):
            st = c.get("statistics", {})
            hidden = st.get("hiddenSubscriberCount")
            subs[c["id"]] = 0 if hidden else int(st.get("subscriberCount", 0) or 0)
    return subs


def channel_uploads(handles, per_channel=None):
    """Watch-accounts fetch: for each YouTube channel handle, pull its recent uploads with view
    counts + thumbnails. Returns {handle: [video dicts]}. The radar then flags videos beating
    that channel's OWN median (the IG overperformance model). Empty dict if no key.

    Quota: ~2 calls/channel (channels.list + playlistItems, 1 unit each) + 1 batched videos.list."""
    if not enabled():
        return {}
    key = os.environ["YOUTUBE_API_KEY"].strip()
    per = per_channel or int(os.environ.get("YT_PER_CHANNEL", "15"))
    out = {}
    for handle in handles:
        h = (handle or "").strip().lstrip("@")
        if not h:
            continue
        try:
            r = requests.get(f"{API}/channels", params={
                "key": key, "forHandle": "@" + h, "part": "contentDetails,statistics,snippet",
            }, timeout=60)
            r.raise_for_status()
            items = r.json().get("items", [])
            if not items:                                   # legacy /user/<name> channels
                r = requests.get(f"{API}/channels", params={
                    "key": key, "forUsername": h, "part": "contentDetails,statistics,snippet",
                }, timeout=60)
                r.raise_for_status()
                items = r.json().get("items", [])
            if not items:
                print(f"[youtube] channel not found: @{h}")
                continue
            ch = items[0]
            title = ch["snippet"]["title"]
            subs = 0 if ch["statistics"].get("hiddenSubscriberCount") else int(ch["statistics"].get("subscriberCount", 0) or 0)
            uploads = ch["contentDetails"]["relatedPlaylists"]["uploads"]
            pi = requests.get(f"{API}/playlistItems", params={
                "key": key, "playlistId": uploads, "part": "contentDetails", "maxResults": per,
            }, timeout=60)
            pi.raise_for_status()
            vids = [i["contentDetails"]["videoId"] for i in pi.json().get("items", [])
                    if i.get("contentDetails", {}).get("videoId")]
            if not vids:
                continue
            vr = requests.get(f"{API}/videos", params={
                "key": key, "id": ",".join(vids[:50]), "part": "statistics,snippet",
            }, timeout=60)
            vr.raise_for_status()
            lst = []
            for v in vr.json().get("items", []):
                st, sn = v.get("statistics", {}), v.get("snippet", {})
                th = sn.get("thumbnails", {})
                cover = (th.get("medium") or th.get("high") or th.get("default") or {}).get("url", "")
                lst.append({
                    "platform": "youtube", "video_id": v["id"], "author": title,
                    "channel_id": sn.get("channelId"), "handle": h,
                    "url": f"https://youtube.com/watch?v={v['id']}", "caption": sn.get("title"),
                    "cover": cover, "subs": subs, "post_time": _iso(sn.get("publishedAt")),
                    "views": int(st.get("viewCount", 0) or 0), "likes": int(st.get("likeCount", 0) or 0),
                    "comments": int(st.get("commentCount", 0) or 0), "shares": 0,
                })
            if lst:
                out[h] = lst
        except Exception as e:
            print(f"[youtube] channel_uploads failed for @{h}: {type(e).__name__}: {str(e)[:120]}")
    return out


def _live(topics):
    key = os.environ["YOUTUBE_API_KEY"].strip()
    per = int(os.environ.get("YT_PER_TOPIC", "10"))
    after = (datetime.datetime.utcnow() - datetime.timedelta(days=21)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = []
    for topic in topics:
        r = requests.get(f"{API}/search", params={
            "key": key, "q": topic, "part": "snippet", "type": "video",
            "order": "viewCount", "publishedAfter": after, "maxResults": per,
        }, timeout=60)
        r.raise_for_status()
        ids = [i["id"]["videoId"] for i in r.json().get("items", []) if i.get("id", {}).get("videoId")]
        if not ids:
            continue
        s = requests.get(f"{API}/videos", params={
            "key": key, "id": ",".join(ids), "part": "statistics,snippet",
        }, timeout=60)
        s.raise_for_status()
        for v in s.json().get("items", []):
            st, sn = v.get("statistics", {}), v.get("snippet", {})
            out.append({
                "platform": "youtube", "video_id": v["id"], "author": sn.get("channelTitle"),
                "channel_id": sn.get("channelId"),
                "url": f"https://youtube.com/watch?v={v['id']}", "caption": sn.get("title"),
                "audio_id": None, "audio_name": None, "hashtags": [], "topic": topic,
                "post_time": _iso(sn.get("publishedAt")),
                "views": int(st.get("viewCount", 0)), "likes": int(st.get("likeCount", 0)),
                "comments": int(st.get("commentCount", 0)), "shares": 0,
            })
    # attach each channel's subscriber count → lets the radar judge views-vs-audience overperformance
    try:
        subs = _channel_subs([v.get("channel_id") for v in out], key)
        for v in out:
            v["subs"] = subs.get(v.get("channel_id"), 0)
    except Exception:
        pass          # subs are an enhancement; radar falls back to per-channel view-median
    return out


def _demo(topics, scan_index):
    now = time.time()
    out = []
    n = len(topics) or 1
    hot = scan_index % n   # one topic is the breakout this scan, across several channels
    for ti, topic in enumerate(topics):
        tag = topic.replace(" ", "")
        is_hot = (ti == hot)
        for ch in range(3):
            rng = random.Random(int(hashlib.md5(f"{topic}{ch}{scan_index}".encode()).hexdigest(), 16) % (10 ** 8))
            baseline = rng.choice([8000, 30000, 90000])   # this channel's normal views
            for p in range(rng.randint(4, 6)):
                if is_hot and p == 0:                     # the breakout video
                    mult, vid = rng.uniform(4.0, 10.0), f"yt_{tag}_{ch}_{p}_{scan_index}"
                else:                                     # normal posts hold the baseline
                    mult = rng.choices([0.4, 0.8, 1.0, 1.3], weights=[2, 3, 4, 3])[0]
                    vid = f"yt_{tag}_{ch}_{p}_x"
                views = int(baseline * mult)
                likes = int(views * rng.uniform(0.02, 0.06))
                out.append({
                    "platform": "youtube", "video_id": vid, "author": f"{tag}_ch{ch}",
                    "url": "https://youtube.com/", "caption": f"{topic} — explained",
                    "audio_id": None, "audio_name": None, "hashtags": [], "topic": topic,
                    "post_time": now - rng.uniform(6, 300) * 3600,
                    "views": views, "likes": likes, "comments": int(likes * 0.05), "shares": 0,
                })
    return out
