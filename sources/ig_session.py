"""Instagram via our OWN logged-in session — a free alternative to the Apify IG actor.

The idea ("always-online account"): log a THROWAWAY account in ONCE, save the session
cookies to ig_session.json, then reuse them on every scan. We never log in again (which
is what trips Instagram's challenges) — we just replay the warm session.

How a scan works:
  1. Open a hashtag page (explore/tags/<tag>/) or a profile (instagram.com/<handle>/).
  2. Listen to the GraphQL/api-v1 responses the page makes as it scrolls and read the
     posts straight out of them — likes, comments, caption, the Reel's SOUND, etc.
  3. Map each post to the same dict shape the radar already expects from Apify, so this
     is a drop-in source (Reels AND photos, with audio for Reels).

⚠️  Use a THROWAWAY account; this is against IG's ToS and the account can get blocked.
    Run from a residential IP (your Mac) — a datacenter IP (the server) is flagged faster.

Setup (once, on the Mac):
    IG_USERNAME / IG_PASSWORD in .env   (or just be ready to type them in the browser)
    python sources/ig_session.py login            # opens a browser; finish login by hand
Enable it for scans:
    IG_SESSION=1 in .env                           # radar then uses this instead of Apify IG
Test it:
    python sources/ig_session.py test ootd menswear
"""

import os
import re
import time

_HERE = os.path.dirname(__file__)
SESSION_FILE = os.path.join(_HERE, "..", "ig_session.json")

IG_USERNAME = os.environ.get("IG_USERNAME", "")
IG_PASSWORD = os.environ.get("IG_PASSWORD", "")

# IG endpoints that carry post data (intercepted from network responses).
_DATA_MARKERS = ("/graphql/query", "/api/v1/tags/", "/api/v1/feed/", "/api/v1/discover/")

PER_SEED = int(os.environ.get("IG_PER_SEED", "40"))     # posts to try to gather per hashtag/account
SCROLLS = int(os.environ.get("IG_SCROLLS", "8"))        # how hard to scroll each page
HEADLESS = os.environ.get("BROWSER_HEADLESS", "1") == "1"

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def enabled():
    """On only when IG_SESSION is truthy AND we have a saved session to replay."""
    if os.environ.get("IG_SESSION", "").strip().lower() in ("", "0", "false", "no"):
        return False
    return os.path.exists(SESSION_FILE)


# ---------------------------------------------------------------- browser

def _new_context(pw, headless=None):
    browser = pw.chromium.launch(
        headless=HEADLESS if headless is None else headless,
        args=["--disable-blink-features=AutomationControlled"],
    )
    context = browser.new_context(
        user_agent=_UA, viewport={"width": 1280, "height": 900}, locale="en-US",
        storage_state=SESSION_FILE if os.path.exists(SESSION_FILE) else None,
    )
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
    return browser, context


# ---------------------------------------------------------------- parsing

def _count(v):
    if isinstance(v, dict):
        v = v.get("count")
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _is_video(node):
    if node.get("is_video") is True:
        return True
    if node.get("media_type") in (2, "VIDEO"):
        return True
    if node.get("product_type") in ("clips", "igtv"):
        return True
    return bool(node.get("video_versions") or node.get("video_url"))


def _caption(node):
    cap = node.get("caption")
    if isinstance(cap, dict):
        return cap.get("text", "") or ""
    if isinstance(cap, str):
        return cap
    edges = (node.get("edge_media_to_caption") or {}).get("edges") or []
    return edges[0].get("node", {}).get("text", "") if edges else ""


def _audio(node):
    """Best-effort Reel sound from the api/v1 clips metadata (richer than Apify's IG).
    Returns (audio_id, audio_name); ('','') when there's no music track."""
    cm = node.get("clips_metadata") or node.get("clips") or {}
    if isinstance(cm, dict):
        mai = ((cm.get("music_info") or {}).get("music_asset_info")) or {}
        if mai:
            aid = mai.get("audio_cluster_id") or mai.get("id") or ""
            name = " - ".join(x for x in (mai.get("title"), mai.get("display_artist")) if x)
            if aid or name:
                return str(aid), name
        osi = cm.get("original_sound_info") or {}
        if osi:
            return str(osi.get("audio_asset_id") or ""), (osi.get("original_audio_title") or "original audio")
    mm = node.get("music_metadata") or {}
    if isinstance(mm, dict):
        mai = ((mm.get("music_info") or {}).get("music_asset_info")) or {}
        if mai:
            return str(mai.get("audio_cluster_id") or ""), (mai.get("title") or "")
    # public web-profile feed (no-login): the compact attribution blob carries the song
    # name + a stable audio_id, so shared sounds still cluster across accounts.
    cmai = node.get("clips_music_attribution_info") or {}
    if isinstance(cmai, dict) and (cmai.get("audio_id") or cmai.get("song_name")):
        aid = str(cmai.get("audio_id") or "")
        if cmai.get("uses_original_audio"):
            name = "original audio"
        else:
            name = " - ".join(x for x in (cmai.get("song_name"), cmai.get("artist_name")) if x)
        return aid, name
    return "", ""


def _cover(node):
    img = node.get("display_url") or node.get("thumbnail_src")
    if img:
        return img
    cands = (node.get("image_versions2") or {}).get("candidates") or []
    return cands[0].get("url") if cands else ""


def _video_url(node):
    vv = node.get("video_versions") or []
    if vv and isinstance(vv, list):
        return vv[0].get("url", "")
    return node.get("video_url") or ""


def _parse_node(node, source):
    """IG media node -> JS Entertainment's standard post dict (Reels and photos). None if unusable."""
    code = node.get("shortcode") or node.get("code")
    if not code:
        return None

    is_vid = _is_video(node)
    owner = node.get("owner") or node.get("user") or {}
    username = owner.get("username", "") if isinstance(owner, dict) else ""
    caption = _caption(node)
    ts = (node.get("taken_at") or node.get("taken_at_timestamp")
          or node.get("device_timestamp"))

    likes = node.get("like_count")
    likes = _count(likes) if likes is not None else _count(
        node.get("edge_liked_by") or node.get("edge_media_preview_like"))
    comments = node.get("comment_count")
    comments = _count(comments) if comments is not None else _count(node.get("edge_media_to_comment"))
    plays = _count(node.get("play_count") or node.get("ig_play_count")
                   or node.get("view_count") or node.get("video_view_count"))

    cover = _cover(node)
    if not cover or (likes == 0 and comments == 0 and plays == 0):
        return None    # skip placeholder/unusable nodes

    aid, aname = (_audio(node) if is_vid else ("", ""))
    # IG hides photo views → rank photos by likes; Reels use real play_count when present.
    views = plays if (is_vid and plays) else likes
    # paid-partnership / sponsored signal (so the scan can drop ads — they trend on ad spend,
    # not on the format working). IG marks these with is_paid_partnership + sponsor_tags.
    sponsored = bool(node.get("is_paid_partnership") or node.get("sponsor_tags")
                     or node.get("partnership_info") or node.get("is_ad")
                     or re.search(r"\bpaid partnership\b|#ad\b|#sponsored\b", (caption or "").lower()))
    return {
        "platform": "instagram",
        "video_id": str(node.get("pk") or node.get("id") or code),
        "author": username or None,
        "url": f"https://www.instagram.com/{'reel' if is_vid else 'p'}/{code}/",
        "cover": cover,
        "video_url": _video_url(node) if is_vid else "",
        "caption": caption,
        "audio_id": aid,
        "audio_name": aname or ("original audio" if is_vid else None),
        "hashtags": re.findall(r"#(\w+)", caption or ""),
        "followers": _count(owner.get("follower_count")) if isinstance(owner, dict) else 0,
        "account_posts": _count(owner.get("media_count")) if isinstance(owner, dict) else 0,
        "media_type": "reel" if is_vid else "post",
        "source": source,
        "post_time": float(ts) if ts else None,
        "is_sponsored": sponsored,
        "views": views,
        "likes": likes,
        "comments": comments,
        "shares": 0,
    }


def _walk(obj, out):
    """Find post nodes (anything with a shortcode/code) anywhere in IG's nested JSON."""
    if isinstance(obj, dict):
        if obj.get("shortcode") or obj.get("code"):
            out.append(obj)
        for v in obj.values():
            _walk(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk(v, out)


def _scrape_url(context, url, source, want):
    captured = []
    page = context.new_page()

    def on_response(resp):
        if any(m in resp.url for m in _DATA_MARKERS):
            try:
                _walk(resp.json(), captured)
            except Exception:
                pass

    page.on("response", on_response)
    try:
        page.goto(url, timeout=45000, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        for _ in range(SCROLLS):
            if len(captured) >= want * 2:     # *2 — many nodes are dupes/unusable
                break
            page.mouse.wheel(0, 3200)
            page.wait_for_timeout(1800)
    except Exception as e:
        print(f"  [ig_session] page issue for {source}: {type(e).__name__}: {str(e)[:120]}")
    page.close()

    out, seen = [], set()
    for node in captured:
        p = _parse_node(node, source)
        if p and p["video_id"] not in seen:
            seen.add(p["video_id"])
            out.append(p)
    return out[:want]


def fetch(seeds, scan_index=0):
    """Pull recent Instagram posts for the creator's IG seeds (hashtags + accounts) via
    the saved session. Same return shape as sources.apify.fetch. [] on any failure."""
    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError:
        print("  [ig_session] playwright not installed — skipping.")
        return []
    if not os.path.exists(SESSION_FILE):
        print("  [ig_session] no saved session — run `python sources/ig_session.py login` first.")
        return []

    tags = [s["value"].lstrip("#").replace(" ", "")
            for s in seeds if s["platform"] == "instagram" and s["kind"] == "hashtag"]
    accts = [s["value"].strip("/").lstrip("@")
             for s in seeds if s["platform"] == "instagram" and s["kind"] in ("account", "self")]
    targets = ([(f"https://www.instagram.com/explore/tags/{t}/", t) for t in tags]
               + [(f"https://www.instagram.com/{a}/", a) for a in accts])
    if not targets:
        return []

    results = []
    try:
        with sync_playwright() as pw:
            browser, context = _new_context(pw)
            for url, source in targets:
                got = _scrape_url(context, url, source, PER_SEED)
                print(f"  [ig_session] {source}: {len(got)} posts")
                results.extend(got)
            context.close()
            browser.close()
    except Exception as e:
        print(f"  [ig_session] failed ({type(e).__name__}: {str(e)[:120]}) — skipping, scan OK.")
        return []

    seen, unique = set(), []
    for v in results:
        if v["video_id"] in seen:
            continue
        seen.add(v["video_id"])
        unique.append(v)
    return [v for v in unique if v.get("author")]


# ---------------------------------------------------------------- login (one-time, interactive)

def login():
    """Open a real browser, let you finish login BY HAND (solve any challenge/2FA), then
    save the warm session. Polls up to 3 min for the sessionid cookie."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser, context = _new_context(pw, headless=False)
        page = context.new_page()
        page.goto("https://www.instagram.com/accounts/login/", timeout=60000,
                  wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        # prefill if creds are set — you still press the button / solve any challenge
        for label in ("Allow all cookies", "Only allow essential cookies", "Decline optional cookies"):
            try:
                b = page.get_by_role("button", name=label)
                if b.count():
                    b.first.click(timeout=2000)
                    break
            except Exception:
                pass
        if IG_USERNAME:
            try:
                page.fill("input[name='username']", IG_USERNAME)
            except Exception:
                pass
        if IG_PASSWORD:
            try:
                page.fill("input[name='password']", IG_PASSWORD)
            except Exception:
                pass
        print("Finish logging in IN THE BROWSER (handle any 'suspicious login' / 2FA prompt).")
        print("Waiting up to 3 minutes for a session…")
        deadline = time.time() + 180
        ok = False
        while time.time() < deadline:
            if any(c["name"] == "sessionid" for c in context.cookies()):
                ok = True
                break
            page.wait_for_timeout(2000)
        if ok:
            context.storage_state(path=SESSION_FILE)
            print(f"✅ session saved to {os.path.abspath(SESSION_FILE)}")
        else:
            print("❌ no sessionid cookie — login not completed. Nothing saved.")
        context.close()
        browser.close()
        return ok


def save_cookies(raw):
    """Build ig_session.json from cookies copied out of YOUR OWN logged-in browser —
    avoids the automated-login challenge entirely. `raw` can be just the sessionid value,
    or a full 'name=value; name=value' cookie string. Only sessionid is required."""
    import json
    pairs = {}
    raw = (raw or "").strip()
    if "=" in raw:
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                pairs[k.strip()] = v.strip().strip('"')
    else:
        pairs["sessionid"] = raw          # they pasted only the sessionid value
    if not pairs.get("sessionid"):
        print("❌ no 'sessionid' found in what you pasted. Copy the sessionid cookie value "
              "(DevTools → Application → Cookies → instagram.com).")
        return False
    wanted = ("sessionid", "ds_user_id", "csrftoken", "mid", "ig_did", "rur")
    cookies = [{"name": k, "value": pairs[k], "domain": ".instagram.com", "path": "/",
                "expires": -1, "httpOnly": k == "sessionid", "secure": True, "sameSite": "Lax"}
               for k in wanted if pairs.get(k)]
    with open(SESSION_FILE, "w") as f:
        json.dump({"cookies": cookies, "origins": []}, f)
    print(f"✅ session saved to {os.path.abspath(SESSION_FILE)} "
          f"({len(cookies)} cookies: {', '.join(c['name'] for c in cookies)})")
    return True


if __name__ == "__main__":
    import sys
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(_HERE, "..", ".env"))
        IG_USERNAME = os.environ.get("IG_USERNAME", "")
        IG_PASSWORD = os.environ.get("IG_PASSWORD", "")
    except Exception:
        pass

    cmd = sys.argv[1] if len(sys.argv) > 1 else "test"
    if cmd == "login":
        sys.exit(0 if login() else 1)     # nonzero on failure so `&&` chains stop cleanly
    elif cmd == "cookies":
        sys.exit(0 if save_cookies(" ".join(sys.argv[2:])) else 1)
    else:
        args = sys.argv[2:] if cmd == "test" else sys.argv[1:]
        # accounts by default; prefix a term with '#' to scrape that hashtag instead.
        seeds = [{"platform": "instagram",
                  "kind": "hashtag" if a.startswith("#") else "account",
                  "value": a.lstrip("#@")} for a in (args or ["instagram"])]
        posts = fetch(seeds)
        print(f"\n{len(posts)} posts:")
        for p in sorted(posts, key=lambda x: x["likes"], reverse=True)[:15]:
            snd = f"  🎵 {p['audio_name']}" if p["media_type"] == "reel" else ""
            print(f"  [{p['media_type']:4}] @{p['author']:20} {p['likes']:>8,} likes  {p['url']}{snd}")
