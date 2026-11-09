"""Mock data source so JS Entertainment works before you sign up for Apify.

Generates believable videos for a creator's seed accounts. A small pool of
"trending audios" is adopted by MORE accounts each scan, so the radar shows
real rising momentum and a working BUY/SATURATED traffic light out of the box.
"""

import hashlib
import random
import time

# Niche-flavored demo sounds, so a comedian never sees "gym phonk" and vice versa.
# (With real Apify data, sounds come from the niche's own accounts.) Index 0 is the
# breakout that ramps up scan-over-scan.
NICHE_SOUNDS = [
    (("comed", "funny", "sketch", "standup", "improv"),
     ["bruh sound effect", "awkward-silence sfx", "laugh-track remix", "viral voiceover", "punchline drop"]),
    (("music", "sing", "cover", "produc", "dj", "rap", "guitar", "piano"),
     ["sped-up snippet", "acoustic flip", "original audio", "slowed + reverb", "viral hook"]),
    (("gym", "fitness", "workout", "health", "run", "yoga"),
     ["gym phonk", "hype beat", "sped-up workout mix", "motivational voiceover", "phonk drift"]),
    (("food", "cook", "recipe", "baking", "chef", "eat"),
     ["ASMR cooking sounds", "sizzle sfx", "satisfying audio", "lo-fi kitchen", "viral food sound"]),
    (("fashion", "beauty", "makeup", "style", "look", "thrift"),
     ["runway beat", "old-money aesthetic", "get-ready sound", "y2k remix", "vogue sfx"]),
    (("travel", "outdoor", "vlog", "adventure", "nature"),
     ["cinematic travel score", "wanderlust audio", "aesthetic vlog music", "lo-fi travel", "airport reel sound"]),
    (("tech", "review", "gadget", "coding", "software", "dev"),
     ["clean tech beat", "unboxing ASMR", "lo-fi coding", "futuristic sfx", "review voiceover"]),
    (("game", "gaming", "gamer", "esport", "speedrun"),
     ["gaming montage beat", "clutch-moment sfx", "sped-up phonk", "rage-clip audio", "victory sound"]),
    (("art", "draw", "paint", "craft", "design", "illustr"),
     ["lo-fi art beat", "satisfying brush ASMR", "timelapse music", "aesthetic audio", "calm piano"]),
]
DEFAULT_SOUNDS = ["original sound", "sped-up edit", "slowed + reverb", "viral voiceover", "trending audio"]


def sounds_for(niche):
    n = (niche or "").lower()
    for keys, labels in NICHE_SOUNDS:
        if any(k in n for k in keys):
            return [(f"aud_{i}", l) for i, l in enumerate(labels)]
    return [(f"aud_{i}", l) for i, l in enumerate(DEFAULT_SOUNDS)]


def _seed_int(text):
    return int(hashlib.md5(text.encode()).hexdigest(), 16) % (10 ** 8)


def fetch(seeds, scan_index=0, niche=""):
    """Return videos (with current stats) for the given seeds.
    scan_index ramps the breakout audio's adoption so momentum is visible."""
    now = time.time()
    AUDIOS = sounds_for(niche)
    # the creator's own / named accounts (these also power their Charts)
    accounts = [{"platform": s["platform"], "value": s["value"]}
                for s in seeds if s["kind"] in ("account", "self")]
    # invent a population of "scene" accounts for each hashtag we're watching,
    # so audios can trend across multiple accounts
    for i, s in enumerate(seeds):
        if s["kind"] == "hashtag":
            accounts.append({"platform": s["platform"], "value": f"{s['value']}_c{i}"})
    if not accounts:
        accounts = [{"platform": "tiktok", "value": f"creator_{i}"} for i in range(6)]

    videos = []
    # breakout adoption grows each scan: scan 0 -> 1 account, scan 3 -> 4, capped
    breakout_adopters = min(1 + scan_index, max(1, len(accounts)))

    for ai, acc in enumerate(accounts):
        rng = random.Random(_seed_int(acc["value"]) + scan_index)
        baseline = rng.choice([8000, 20000, 50000, 120000])  # this account's "normal"
        n_posts = rng.randint(4, 7)
        for p in range(n_posts):
            # most posts are near baseline; some overperform
            mult = rng.choices([0.4, 0.8, 1.0, 1.3, 4.5, 9.0],
                               weights=[2, 3, 4, 3, 2, 1])[0]
            audio = AUDIOS[rng.randrange(len(AUDIOS))]
            # the first `breakout_adopters` accounts post a HIT using the breakout audio
            if ai < breakout_adopters and p == 0:
                audio = AUDIOS[0]
                mult = rng.uniform(4.0, 12.0)
            views = int(baseline * mult)
            likes = int(views * rng.uniform(0.04, 0.12))
            post_time = now - rng.uniform(2, 96) * 3600
            vid = f"{acc['value']}_{p}_{scan_index if ai < breakout_adopters and p == 0 else 'x'}"
            videos.append({
                "platform": acc.get("platform", "tiktok"),
                "video_id": vid,
                "author": acc["value"],
                "url": f"https://example.com/{acc['value']}/{p}",
                "caption": f"post {p} by {acc['value']}",
                "audio_id": audio[0],
                "audio_name": audio[1],
                "hashtags": [],
                "post_time": post_time,
                "views": views,
                "likes": likes,
                "comments": int(likes * rng.uniform(0.02, 0.08)),
                "shares": int(likes * rng.uniform(0.01, 0.05)),
            })
    return videos
