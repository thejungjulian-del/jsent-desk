"""TikTok via our own headless browser — NO login, NO account, a free alternative to the
Apify TikTok actor.

TikTok's data API needs signed requests (X-Bogus / msToken / device sig), so a plain HTTP
GET can't pull a profile's videos. But the public profile PAGE signs those requests itself
as it loads — so we drive a headless browser to `tiktok.com/@handle`, let the page fire its
own `/api/post/item_list/` request, and read the videos straight out of the intercepted
response (the same trick `ig_session.py` uses for Instagram). This gives the full item
struct, including the SOUND (music id + title + artist) — richer than Instagram's.

Account-based only: this reads a profile's own videos, so there is NO hashtag search / no
For-You discovery. Intake = the niche TikTok accounts you seed.

⚠️  No login and only public data, but still against TikTok's ToS to scrape. Run from a
    RESIDENTIAL IP (your Mac); a datacenter IP (the server) is challenged far faster.

CLI:
    python sources/tiktok_web.py test <handle> [<handle> ...]
Enable it for scans:
    TIKTOK_WEB=1 in .env      # radar then uses this for TikTok instead of Apify
"""

import os
import random
import time

PER_ACCOUNT = int(os.environ.get("TIKTOK_WEB_PER_ACCOUNT", "30"))   # videos to keep per account
SCROLLS = int(os.environ.get("TIKTOK_WEB_SCROLLS", "5"))            # how hard to scroll each profile
SLEEP_MIN = float(os.environ.get("TIKTOK_WEB_SLEEP_MIN", "5"))      # gap between accounts (anti-throttle)
SLEEP_MAX = float(os.environ.get("TIKTOK_WEB_SLEEP_MAX", "10"))
HEADLESS = os.environ.get("BROWSER_HEADLESS", "1") == "1"

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def enabled():
    """On when TIKTOK_WEB is truthy. Needs nothing else — no login, no session file."""
    return os.environ.get("TIKTOK_WEB", "").strip().lower() not in ("", "0", "false", "no")


def _num(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _hashtags(item):
    out = []
    for c in (item.get("challenges") or []):
        t = c.get("title") if isinstance(c, dict) else None
        if t:
            out.append(t)
    for te in (item.get("textExtra") or []):
        t = te.get("hashtagName") if isinstance(te, dict) else None
        if t and t not in out:
            out.append(t)
    return out


def _map(item, source):
    """One TikTok item_list entry -> JS Entertainment's standard post dict. None if unusable."""
    vid = item.get("id")
    author = item.get("author")
    handle = author.get("uniqueId") if isinstance(author, dict) else author
    if not vid or not handle:
        return None
    music = item.get("music") or {}
    stats = item.get("stats") or {}
    astats = item.get("authorStats") or {}
    video = item.get("video") or {}

    cover = video.get("cover") or video.get("originCover") or video.get("dynamicCover") or ""
    video_url = video.get("downloadAddr") or video.get("playAddr") or ""
    ct = item.get("createTime")
    return {
        "platform": "tiktok",
        "video_id": str(vid),
        "author": handle,
        "url": f"https://www.tiktok.com/@{handle}/video/{vid}",
        "cover": cover,
        "video_url": video_url,
        "caption": item.get("desc") or "",
        "audio_id": str(music.get("id") or ""),
        "audio_name": music.get("title") or "original sound",
        "hashtags": _hashtags(item),
        "followers": _num(astats.get("followerCount")),
        "account_likes": _num(astats.get("heartCount") or astats.get("heart")),
        "account_posts": _num(astats.get("videoCount")),
        "media_type": "video",
        "source": source,
        "post_time": float(ct) if ct else None,
        "is_sponsored": bool(item.get("isAd") or item.get("isSponsored")
                             or (item.get("adAuthorization"))
                             or "#ad" in (item.get("desc") or "").lower()),
        "views": _num(stats.get("playCount")),
        "likes": _num(stats.get("diggCount")),
        "comments": _num(stats.get("commentCount")),
        "shares": _num(stats.get("shareCount")),
    }


def _proxy():
    """Parse SCRAPER_PROXY (http://user:pass@host:port) into Playwright's proxy dict, so
    this can run from a datacenter/server IP. None when unset (direct connection)."""
    raw = os.environ.get("SCRAPER_PROXY", "").strip()
    if not raw:
        return None
    from urllib.parse import urlparse
    p = urlparse(raw)
    proxy = {"server": f"{p.scheme or 'http'}://{p.hostname}:{p.port}"}
    if p.username:
        proxy["username"] = p.username
    if p.password:
        proxy["password"] = p.password
    return proxy


def _new_context(pw):
    proxy = _proxy()
    browser = pw.chromium.launch(
        headless=HEADLESS, args=["--disable-blink-features=AutomationControlled"],
        **({"proxy": proxy} if proxy else {}))
    context = browser.new_context(
        user_agent=_UA, viewport={"width": 1280, "height": 900}, locale="en-US")
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
    return browser, context


def _scrape_account(context, handle, want):
    captured = []
    page = context.new_page()

    def on_response(resp):
        # ONLY the profile's own-posts feed. A profile page also fires repost/item_list and
        # recommend/item_list (other people's videos) — matching a bare "item_list" pulls in
        # those strangers and pollutes the pool, so pin to post/item_list.
        if "/api/post/item_list" in resp.url:
            try:
                items = resp.json().get("itemList") or []
                captured.extend(items)
            except Exception:
                pass

    page.on("response", on_response)
    try:
        page.goto(f"https://www.tiktok.com/@{handle}", timeout=45000,
                  wait_until="domcontentloaded")
        page.wait_for_timeout(4500)        # the populated post/item_list arrives a bit late
        for _ in range(SCROLLS):
            if len(captured) >= want:
                break
            page.mouse.wheel(0, 3000)
            page.wait_for_timeout(2500)
    except Exception as e:
        print(f"  [tiktok_web] @{handle}: page issue {type(e).__name__}: {str(e)[:120]}")
    page.close()

    out, seen = [], set()
    for item in captured:
        p = _map(item, handle)
        # keep only the creator's OWN videos (defends against any repost leaking into the grid)
        if p and p["video_id"] not in seen and (p["author"] or "").lower() == handle.lower():
            seen.add(p["video_id"])
            out.append(p)
    return out[:want]


def fetch(seeds, scan_index=0):
    """Pull each TikTok account seed's recent videos via the headless-browser intercept.
    Same return shape as sources.apify.fetch. Hashtag seeds are ignored (profile-only);
    [] on any failure (the scan keeps running on whatever else it has)."""
    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError:
        print("  [tiktok_web] playwright not installed — skipping.")
        return []

    accts, seen = [], set()
    for s in seeds:
        if s.get("platform") != "tiktok" or s.get("kind") not in ("account", "self"):
            continue
        h = (s.get("value") or "").strip().strip("/").lstrip("@").lower()
        if h and h not in seen:
            seen.add(h)
            accts.append(h)
    if not accts:
        return []

    results = []
    try:
        with sync_playwright() as pw:
            browser, context = _new_context(pw)
            for i, h in enumerate(accts):
                got = _scrape_account(context, h, PER_ACCOUNT)
                print(f"  [tiktok_web] @{h}: {len(got)} videos")
                results.extend(got)
                if i < len(accts) - 1:               # space requests so TikTok doesn't throttle
                    time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))
            context.close()
            browser.close()
    except Exception as e:
        print(f"  [tiktok_web] failed ({type(e).__name__}: {str(e)[:120]}) — skipping, scan OK.")
        return []

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
    except Exception:
        pass

    args = sys.argv[2:] if (len(sys.argv) > 1 and sys.argv[1] == "test") else sys.argv[1:]
    seeds = [{"platform": "tiktok", "kind": "account", "value": a} for a in (args or ["tiktok"])]
    posts = fetch(seeds)
    print(f"\n{len(posts)} videos:")
    for p in sorted(posts, key=lambda x: x.get("views") or 0, reverse=True)[:15]:
        snd = f"  🎵 {p['audio_name']}" if p.get("audio_name") else ""
        print(f"  @{(p['author'] or '?'):20} {(p.get('views') or 0):>10,} views  {p['url']}{snd}")
