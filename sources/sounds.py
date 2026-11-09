"""Sound-based discovery.

Once a sound is hot in a creator's pull, fetch MORE posts that use that exact sound —
this turns a sound only 1-2 popular creators happened to be caught using into a trend
with many riders. The key to it working is a CORRECT music id: the main TikTok scrape
must use clockworks (exact string musicMeta.musicId), not apidojo (rounds the id).

Uses clockworks/tiktok-sound-scraper (input `musics`). We build a music URL with a
placeholder slug + the exact id (`tiktok.com/music/x-<id>`); TikTok resolves by the
trailing id (slug is cosmetic). ⚠️ that slug behaviour is undocumented — watch the
[sounds] log to confirm it pulls posts; if it returns 0, the URL shape needs adjusting.
ON by default now that ids are correct; set APIFY_SOUND_DISCOVERY=0 to disable.
"""

import os

from . import apify


def enabled():
    return bool(os.environ.get("APIFY_TOKEN", "").strip()) and \
        os.environ.get("APIFY_SOUND_DISCOVERY", "1").strip().lower() not in ("0", "false", "no")


def _candidate_sounds(pool, max_sounds):
    """TikTok audio_ids used by >=2 distinct authors in the pull, ranked by total likes."""
    by_id = {}
    for v in pool:
        if v.get("platform") != "tiktok" or not v.get("audio_id"):
            continue
        d = by_id.setdefault(v["audio_id"], {"authors": set(), "likes": 0, "source": v.get("source")})
        d["authors"].add(v.get("author"))
        d["likes"] += int(v.get("likes", 0) or 0)
    hot = [(aid, d) for aid, d in by_id.items() if len(d["authors"]) >= 2]
    hot.sort(key=lambda x: x[1]["likes"], reverse=True)
    return hot[:max_sounds]


def expand(pool):
    """Return EXTRA posts pulled by hot-sound, tagged so niche attribution carries over."""
    if not enabled():
        return []
    actor = os.environ.get("APIFY_TIKTOK_SOUND_ACTOR", "clockworks/tiktok-sound-scraper")
    max_sounds = int(os.environ.get("SOUND_DISCOVERY_MAX", "5"))
    per = int(os.environ.get("SOUND_DISCOVERY_PER", "30"))
    cands = _candidate_sounds(pool, max_sounds)
    print(f"[sounds] {len(cands)} hot sound(s) to expand")
    extra = []
    for aid, d in cands:
        url = f"https://www.tiktok.com/music/x-{aid}"
        try:
            items = apify._run(actor, {"musics": [url], "resultsPerPage": per})
        except Exception as e:
            print(f"[sounds] {aid} failed: {type(e).__name__}: {str(e)[:160]}")
            continue
        got = 0
        for it in items:
            v = apify._map_tiktok_cw(it)
            if not (v.get("video_id") and v.get("author")):
                continue
            v["source"] = d.get("source")        # inherit the niche lane of the original posts
            extra.append(v)
            got += 1
        print(f"[sounds] sound {aid}: pulled {got} posts")
    return extra
