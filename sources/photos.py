"""Instagram photo / carousel posts source.

Photos have no view counts, so the 'reach' metric is engagement (likes). We store
that in the `views` field so the overperformance math is unchanged, and tag each
post with its look (a niche keyword) so the radar clusters them as PHOTO trends.

Real data via the Apify Instagram scraper (set APIFY_TOKEN); demo data otherwise.
"""

import hashlib
import os
import random
import time

import requests

from . import apify  # reuse token + actor config


def fetch(seeds, scan_index=0):
    tags = [s["value"] for s in seeds] or []
    if not tags:
        return []
    if apify.enabled():
        try:
            vids = _live(tags)
            if vids:
                return vids
        except Exception:
            pass
    return _demo(tags, scan_index)


def _live(tags):
    actor = os.environ.get("APIFY_IG_ACTOR", "apidojo/instagram-scraper")
    per = int(os.environ.get("APIFY_PER_SEED", "30"))
    urls = [f"https://www.instagram.com/explore/tags/{t.lstrip('#').replace(' ', '')}/" for t in tags]
    items = apify._run(actor, {"startUrls": urls, "maxItems": per * len(urls)})
    out = []
    for it in items:
        if it.get("isVideo"):                       # photos / carousels only (skip Reels)
            continue
        likes = apify._num(it.get("likeCount"))
        owner = it.get("owner") or {}
        src = apify._ig_source(it.get("inputSource"))
        out.append({
            "platform": "instagram_photo",
            "video_id": it.get("id") or it.get("code") or it.get("url"),
            "author": owner.get("username"),
            "url": it.get("url"),
            "cover": apify._pick_url(it.get("displayUrl"), it.get("thumbnailUrl"),
                                     it.get("thumbnail"), it.get("images")),
            "caption": it.get("caption"), "audio_id": None, "audio_name": None,
            "hashtags": apify._tags_from_caption(it.get("caption")),
            "topic": src, "media_type": "post", "source": src,
            "post_time": apify._ts(it.get("createdAt"), it.get("timestamp"),
                                   it.get("takenAt"), it.get("taken_at_timestamp")),
            "views": likes, "likes": likes,
            "comments": apify._num(it.get("commentCount")), "shares": 0,
        })
    return out


def _demo(tags, scan_index):
    now = time.time()
    out = []
    n = len(tags) or 1
    hot = scan_index % n
    for ti, tag in enumerate(tags):
        is_hot = (ti == hot)
        for ch in range(3):
            rng = random.Random(int(hashlib.md5(f"{tag}{ch}{scan_index}p".encode()).hexdigest(), 16) % (10 ** 8))
            baseline = rng.choice([3000, 15000, 60000])   # this account's normal likes
            for p in range(rng.randint(4, 6)):
                if is_hot and p == 0:
                    mult, vid = rng.uniform(4.0, 10.0), f"ph_{tag}_{ch}_{p}_{scan_index}"
                else:
                    mult = rng.choices([0.4, 0.8, 1.0, 1.3], weights=[2, 3, 4, 3])[0]
                    vid = f"ph_{tag}_{ch}_{p}_x"
                likes = int(baseline * mult)
                out.append({
                    "platform": "instagram_photo", "video_id": vid, "author": f"{tag}_ig{ch}",
                    "url": "https://instagram.com/", "caption": f"{tag} look",
                    "audio_id": None, "audio_name": None, "hashtags": [], "topic": tag,
                    "post_time": now - rng.uniform(6, 300) * 3600,
                    "views": likes, "likes": likes, "comments": int(likes * 0.03), "shares": 0,
                })
    return out
