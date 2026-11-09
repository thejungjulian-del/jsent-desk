"""Instagram via the PUBLIC web-profile endpoint — NO login, NO browser, NO cookie.

This is the cheapest in-house alternative to the Apify IG actor. It GETs Instagram's
public web-profile API for a username and reads that account's most recent posts straight
out of the JSON, then maps each one to JS Entertainment's standard post dict via the same parser the
logged-in session scraper uses.

    GET https://i.instagram.com/api/v1/users/web_profile_info/?username=<handle>
        header  x-ig-app-id: <public web app id>
    -> data.user.edge_owner_to_timeline_media.edges[].node  (~12 recent posts)

Account-based only: the public endpoint is per-username, so there is NO hashtag search and
NO For-You discovery here. Intake = the list of niche IG accounts you give the scanner
(the creator's own accounts + any niche accounts seeded/discovered).

⚠️  Best run from a RESIDENTIAL IP (your Mac). Instagram rate-limits public requests hard
    from datacenter IPs and under volume — we sleep between accounts and cap per-account.
    Reads public data only; no auth, nothing posted.

CLI:
    python sources/ig_public.py test <handle> [<handle> ...]
Enable it for scans:
    IG_PUBLIC=1 in .env       # radar then uses this for Instagram instead of Apify
"""

import json
import os
import random
import time
import urllib.error
import urllib.request

try:                                            # reuse the session scraper's node mapper
    from sources.ig_session import _parse_node
except ImportError:                             # when run as `python sources/ig_public.py`
    from ig_session import _parse_node

# Instagram's public web app id (the one the website itself sends). Override via env if it
# ever changes. This header is what makes the endpoint return JSON instead of a login wall.
IG_APP_ID = os.environ.get("IG_APP_ID", "936619743392459")

PER_ACCOUNT = int(os.environ.get("IG_PUBLIC_PER_ACCOUNT", "12"))    # posts to keep per account
# Randomized gap between accounts (research: jitter matters more than magnitude; a fixed
# delay still reads as a bot). 3-8s over ~15-30 accounts = a ~1-4 min human-paced scan.
SLEEP_MIN = float(os.environ.get("IG_PUBLIC_SLEEP_MIN", "3.0"))
SLEEP_MAX = float(os.environ.get("IG_PUBLIC_SLEEP_MAX", "8.0"))
TIMEOUT = int(os.environ.get("IG_PUBLIC_TIMEOUT", "20"))

# try the app-API host first, then the www host — whichever answers.
_HOSTS = [h for h in os.environ.get("IG_PUBLIC_HOSTS", "i.instagram.com,www.instagram.com").split(",") if h.strip()]
_PATH = "/api/v1/users/web_profile_info/?username={}"

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Don't follow IG's redirects — a 302 to /accounts/login/ is a backoff/challenge
    signal, not a real resource move (and following it loops). Surface it as an HTTPError."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

# Optional residential proxy (so this can run from a datacenter/server IP that Instagram
# would otherwise 401). Format: SCRAPER_PROXY=http://user:pass@host:port . Empty = direct.
_PROXY = os.environ.get("SCRAPER_PROXY", "").strip()
_handlers = [_NoRedirect()]
if _PROXY:
    _handlers.append(urllib.request.ProxyHandler({"http": _PROXY, "https": _PROXY}))
_OPENER = urllib.request.build_opener(*_handlers)

# Logged-in cookies — a logged-in session gets far higher rate limits than the anonymous
# endpoint (which only serves ~1 fetch per window). A POOL of burner accounts is supported:
# set IG_COOKIE plus IG_COOKIE_1, IG_COOKIE_2, … (each = a sessionid or full cookie string).
# The pool rotates and FAILS OVER — when one account hits a rate-limit, the scan continues
# on the next one — multiplying total headroom and adding resilience.
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
SESSION_COOLDOWN = int(os.environ.get("IG_SESSION_COOLDOWN", "3600"))   # secs to rest a rate-limited account


def _cookie_val(cookie, name):
    for part in (cookie or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip() == name:
                return v.strip()
    return ""


def _load_sessions():
    """Build the burner-account pool from IG_COOKIE + IG_COOKIE_1..N. Each entry keeps its
    OWN fingerprint state (csrf token + the x-ig-www-claim anti-replay token), so the
    accounts never cross-contaminate. Empty list = anonymous mode."""
    raws, i = [], 1
    if os.environ.get("IG_COOKIE", "").strip():
        raws.append(os.environ["IG_COOKIE"].strip())
    while os.environ.get(f"IG_COOKIE_{i}", "").strip():
        raws.append(os.environ[f"IG_COOKIE_{i}"].strip())
        i += 1
    out = []
    for raw in raws:
        cookie = raw if "=" in raw else f"sessionid={raw}"
        out.append({"cookie": cookie, "csrf": _cookie_val(cookie, "csrftoken"),
                    "www_claim": "0", "blocked_until": 0.0})
    return out


_SESSIONS = _load_sessions()
_CUR = [0]                                      # index of the session to try first


class RateLimited(Exception):
    """Instagram asked us to back off (401 'please wait' / 429). Signals the scan to STOP
    rather than hammer every remaining account and burn the session."""


def enabled():
    """On when IG_PUBLIC is truthy. Needs nothing else — no session file, no creds."""
    return os.environ.get("IG_PUBLIC", "").strip().lower() not in ("", "0", "false", "no")


def _api_get_with(session, path, referer):
    """One GET using a specific session (or anonymous if session is None). Raises
    RateLimited on a backoff signal (401/429/30x); raises the last transient error else."""
    last = None
    for host in _HOSTS:
        url = "https://" + host.strip() + path
        headers = {
            "User-Agent": _UA,
            "x-ig-app-id": IG_APP_ID,
            "x-ig-www-claim": (session or {}).get("www_claim", "0"),
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": referer,
        }
        if session:
            headers["Cookie"] = session["cookie"]
            if session.get("csrf"):
                headers["x-csrftoken"] = session["csrf"]
        req = urllib.request.Request(url, headers=headers)
        try:
            with _OPENER.open(req, timeout=TIMEOUT) as r:
                claim = r.headers.get("x-ig-set-www-claim")
                if claim and session:                  # server issued a fresh claim → store it
                    session["www_claim"] = claim
                return json.load(r)
        except urllib.error.HTTPError as e:
            # 401/429 = explicit rate-limit; 30x = redirect to login (challenge/backoff)
            if e.code in (401, 429) or 300 <= e.code < 400:
                raise RateLimited(f"HTTP {e.code}") from e
            last = e
        except Exception as e:                         # transient/network → try the next host
            last = e
    raise last


def _api_get(path, referer):
    """GET one IG endpoint, rotating across the burner-account pool. Sticks with the current
    account while it works; on a rate-limit/challenge it rests that account (SESSION_COOLDOWN)
    and FAILS OVER to the next available one. Raises RateLimited only when ALL accounts are
    resting. Anonymous (no cookies) = a single direct attempt."""
    if not _SESSIONS:
        return _api_get_with(None, path, referer)
    now = time.time()
    n = len(_SESSIONS)
    last = None
    for off in range(n):
        idx = (_CUR[0] + off) % n
        sess = _SESSIONS[idx]
        if sess["blocked_until"] > now:                # this account is resting
            continue
        try:
            r = _api_get_with(sess, path, referer)
            _CUR[0] = idx                              # stick with the account that works
            return r
        except RateLimited as e:
            sess["blocked_until"] = now + SESSION_COOLDOWN
            print(f"  [ig_public] account #{idx + 1} rate-limited → resting, failing over")
            last = e
            continue
        except Exception as e:
            last = e
            continue
    raise (last if isinstance(last, RateLimited) else RateLimited("all accounts resting"))
    raise last


def _get(username):
    """The public web-profile JSON for one username (metadata + user id; the embedded post
    edges are only populated for ANONYMOUS requests)."""
    return _api_get(_PATH.format(username), f"https://www.instagram.com/{username}/")


def _fetch_account(username, want=None):
    """Returns the account's recent posts. Propagates RateLimited so the scan can stop early.

    Two paths: ANONYMOUS reads the posts embedded in the profile's `edge_owner_to_timeline_
    media`; LOGGED-IN (cookie set) must instead hit the authed user-feed endpoint, because
    the profile call returns ZERO post edges when authenticated."""
    want = want or PER_ACCOUNT
    try:
        data = _get(username)
    except RateLimited:
        raise                                   # let fetch() stop the whole run
    except Exception as e:
        print(f"  [ig_public] @{username}: {type(e).__name__}: {str(e)[:100]}")
        return []

    user = (data.get("data") or {}).get("user") or {}
    if not user:
        print(f"  [ig_public] @{username}: no user payload (private / not found / blocked)")
        return []

    # carry the profile-level follower/post counts onto each post (feed items don't repeat them)
    followers = (user.get("edge_followed_by") or {}).get("count") or user.get("follower_count") or 0
    media_count = (user.get("edge_owner_to_timeline_media") or {}).get("count") or user.get("media_count") or 0

    if _SESSIONS:                               # authed → posts come from the user feed
        uid = user.get("id") or user.get("pk")
        if not uid:
            return []
        try:
            feed = _api_get(f"/api/v1/feed/user/{uid}/?count={want}",
                            f"https://www.instagram.com/{username}/")
        except RateLimited:
            raise
        except Exception as e:
            print(f"  [ig_public] @{username} (feed): {type(e).__name__}: {str(e)[:100]}")
            return []
        nodes = feed.get("items") or []
    else:                                       # anonymous → posts embedded in the profile
        nodes = [e.get("node") or {} for e in
                 ((user.get("edge_owner_to_timeline_media") or {}).get("edges") or [])]

    out = []
    for node in nodes[:want]:
        # nodes carry a sparse owner; graft username + the profile's follower/post counts so
        # the parser attributes the post and records account stats.
        owner = node.get("owner") or node.get("user")
        if not isinstance(owner, dict):
            owner = {}
        owner.setdefault("username", user.get("username") or username)
        owner.setdefault("follower_count", followers)
        owner.setdefault("media_count", media_count)
        node["owner"] = owner
        p = _parse_node(node, username)
        if p:
            out.append(p)
    return out


def validate(handle):
    """Lightweight check used by creator auto-discovery: is this a REAL, public, active
    account? Returns {username, followers, posts, is_private} or None (not found / error).
    Raises RateLimited so a discovery sweep can stop on a backoff signal."""
    h = (handle or "").strip().strip("/").lstrip("@").lower()
    if not h:
        return None
    data = _get(h)                              # raises RateLimited on 401/429
    user = (data.get("data") or {}).get("user") or {}
    if not user:
        return None
    return {
        "username": user.get("username") or h,
        "followers": (user.get("edge_followed_by") or {}).get("count") or user.get("follower_count") or 0,
        "posts": (user.get("edge_owner_to_timeline_media") or {}).get("count") or user.get("media_count") or 0,
        "is_private": bool(user.get("is_private")),
        # for niche-fit judging (free — comes with the profile fetch)
        "full_name": user.get("full_name") or "",
        "bio": (user.get("biography") or "")[:300],
        "category": user.get("category_name") or "",
        "is_verified": bool(user.get("is_verified")),
    }


def fetch(seeds, scan_index=0):
    """Public-endpoint Instagram fetch. Reads recent posts from each IG account seed and
    returns them in the same shape as sources.apify.fetch. Hashtag seeds are ignored (the
    public endpoint is per-username); [] when there are no account seeds."""
    accts, seen = [], set()
    for s in seeds:
        if s.get("platform") != "instagram" or s.get("kind") not in ("account", "self"):
            continue
        h = (s.get("value") or "").strip().strip("/").lstrip("@").lower()
        if h and h not in seen:
            seen.add(h)
            accts.append(h)
    if not accts:
        return []

    results = []
    for i, h in enumerate(accts):
        try:
            got = _fetch_account(h)
        except RateLimited as e:
            # Instagram told us to back off. STOP now — hammering the rest only deepens the
            # throttle and risks the session. Keep whatever we already pulled this run.
            print(f"  [ig_public] @{h}: {e} → backing off, stopping scan "
                  f"({len(results)} posts from {i} accounts so far)")
            break
        print(f"  [ig_public] @{h}: {len(got)} posts")
        results.extend(got)
        if i < len(accts) - 1:
            time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))

    out, seen = [], set()
    for v in results:
        if not v.get("author") or v["video_id"] in seen:
            continue
        seen.add(v["video_id"])
        out.append(v)
    return out


if __name__ == "__main__":
    import sys
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
        IG_APP_ID = os.environ.get("IG_APP_ID", IG_APP_ID)
    except Exception:
        pass

    args = sys.argv[2:] if (len(sys.argv) > 1 and sys.argv[1] == "test") else sys.argv[1:]
    seeds = [{"platform": "instagram", "kind": "account", "value": a} for a in (args or ["instagram"])]
    posts = fetch(seeds)
    print(f"\n{len(posts)} posts:")
    for p in sorted(posts, key=lambda x: x.get("likes") or 0, reverse=True)[:15]:
        snd = f"  🎵 {p['audio_name']}" if (p["media_type"] == "reel" and p.get("audio_name")) else ""
        print(f"  [{p['media_type']:4}] @{(p['author'] or '?'):20} {(p.get('likes') or 0):>9,} likes  {p['url']}{snd}")
