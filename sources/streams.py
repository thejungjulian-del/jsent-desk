"""Stream categories source — 'what to go live with', scoped to the creator's NICHE.

A comedian should see Just Chatting / Podcasts / IRL — not Music. So we map the
niche to the categories creators in that lane actually stream, always including
the universal go-live formats (Just Chatting, IRL).

Real data via Twitch Helix (set TWITCH_CLIENT_ID + TWITCH_CLIENT_SECRET); demo
data otherwise.
"""

import hashlib
import os
import random

import requests

# niche keyword -> the categories creators in that lane go live with
NICHE_MAP = [
    (("comed", "funny", "sketch", "standup", "stand-up", "improv"),
     ["Just Chatting", "Podcasts & Talk Shows", "IRL"]),
    (("music", "sing", "cover", "produc", "dj", "rap", "guitar", "piano"),
     ["Music", "DJs", "Just Chatting"]),
    (("game", "gaming", "gamer", "esport", "speedrun"),
     ["Valorant", "Minecraft", "Grand Theft Auto V", "League of Legends", "Just Chatting"]),
    (("fitness", "gym", "workout", "health", "yoga", "run"),
     ["Fitness & Health", "Just Chatting", "IRL"]),
    (("food", "cook", "recipe", "baking", "chef", "eat"),
     ["Food & Drink", "ASMR", "Just Chatting"]),
    (("tech", "review", "gadget", "coding", "software", "dev"),
     ["Science & Technology", "Software and Game Development", "Just Chatting"]),
    (("fashion", "beauty", "makeup", "style", "look", "thrift"),
     ["Beauty & Body Art", "Just Chatting", "IRL"]),
    (("travel", "outdoor", "vlog", "adventure", "nature"),
     ["Travel & Outdoors", "IRL", "Just Chatting"]),
    (("art", "draw", "paint", "craft", "design", "illustr"),
     ["Art", "Makers & Crafting", "Just Chatting"]),
]
_DEFAULT = ["Just Chatting", "IRL", "Podcasts & Talk Shows"]
_YT_LIVE = {"Music", "IRL", "Travel & Outdoors", "Food & Drink"}   # shown as YouTube Live


def categories_for(niche):
    n = (niche or "").lower()
    cats = None
    for keys, c in NICHE_MAP:
        if any(k in n for k in keys):
            cats = list(c)
            break
    cats = cats or list(_DEFAULT)
    if "Just Chatting" not in cats:
        cats.append("Just Chatting")
    return cats


def twitch_enabled():
    return bool(os.environ.get("TWITCH_CLIENT_ID", "").strip()
                and os.environ.get("TWITCH_CLIENT_SECRET", "").strip())


def fetch(niche, scan_index=0):
    cats = categories_for(niche)
    if twitch_enabled():
        try:
            out = _twitch_live(cats)
            if out:
                return out
        except Exception:
            pass
    return _demo(cats, scan_index)


def _twitch_token():
    r = requests.post("https://id.twitch.tv/oauth2/token", params={
        "client_id": os.environ["TWITCH_CLIENT_ID"].strip(),
        "client_secret": os.environ["TWITCH_CLIENT_SECRET"].strip(),
        "grant_type": "client_credentials",
    }, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def _twitch_live(cats):
    token = _twitch_token()
    h = {"Client-Id": os.environ["TWITCH_CLIENT_ID"].strip(), "Authorization": f"Bearer {token}"}
    out = []
    for cat in cats:
        g = requests.get("https://api.twitch.tv/helix/games", headers=h,
                         params={"name": cat}, timeout=30).json().get("data", [])
        if not g:
            continue
        streams = requests.get("https://api.twitch.tv/helix/streams", headers=h,
                               params={"game_id": g[0]["id"], "first": 100}, timeout=30).json().get("data", [])
        out.append({"source": "twitch", "category": cat,
                    "viewers": sum(s.get("viewer_count", 0) for s in streams),
                    "streams_count": max(len(streams), 1)})
    return out


def _demo(cats, scan_index):
    surge = scan_index % len(cats) if cats else 0
    out = []
    for i, cat in enumerate(cats):
        rng = random.Random(int(hashlib.md5(f"{cat}{scan_index}".encode()).hexdigest(), 16) % (10 ** 8))
        base = rng.choice([2000, 15000, 60000, 150000])
        boost = rng.uniform(1.8, 3.0) if i == surge else rng.uniform(0.8, 1.2)
        viewers = int(base * boost)
        out.append({"source": "youtube_live" if cat in _YT_LIVE else "twitch",
                    "category": cat, "viewers": viewers,
                    "streams_count": max(1, int(viewers / rng.uniform(60, 600)))})
    return out
