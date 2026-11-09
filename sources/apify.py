"""Live data source: Apify actors for TikTok + Instagram.

Uses Apify's run-sync endpoint (runs an actor and returns its dataset in one call).
Set APIFY_TOKEN to enable. Actors (overridable via env):

  TikTok profiles:  apidojo/tiktok-profile-scraper   (input: usernames[],  maxItems)
  TikTok search:    apidojo/tiktok-scraper           (input: keywords[],   maxItems)
  Instagram:        apidojo/instagram-scraper        (input: startUrls[],  maxItems)

These apidojo actors return a normalized shape: counts arrive as STRINGS, captions
are `title`/`caption`, the author is nested (`channel`/`owner`), hashtags are plain
strings, and TikTok timestamps are unix seconds (`uploadedAt`). The mappers below
flatten all of that to the dict the radar expects.
"""

import os
import re

import requests

TIMEOUT = 300
BASE = "https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"


def enabled():
    return bool(os.environ.get("APIFY_TOKEN", "").strip())


def _run(actor, payload):
    token = os.environ["APIFY_TOKEN"].strip()
    url = BASE.format(actor=actor.replace("/", "~"))
    r = requests.post(url, params={"token": token}, json=payload, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _num(v):
    """apidojo returns counts as strings ('304319'); coerce to int."""
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _epoch(v):
    try:
        return float(v)            # unix seconds (TikTok `uploadedAt`)
    except (TypeError, ValueError):
        return None


def _iso(s):
    try:
        import datetime
        return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _ts(*vals):
    """A post's timestamp from whichever field the actor provides — unix seconds or ISO."""
    for v in vals:
        if v in (None, "", 0):
            continue
        t = _epoch(v)
        if t and t > 1_000_000_000:       # plausible unix seconds (after 2001)
            return t
        t = _iso(v)
        if t:
            return t
    return None


def _tags_from_caption(caption):
    return re.findall(r"#(\w+)", caption or "")


def _pick_url(*vals):
    """First usable http(s) image URL among the candidates (handles str, list, or
    list-of-{url}). Used to grab a post's cover/thumbnail defensively across actors."""
    for v in vals:
        if isinstance(v, str) and v.startswith("http"):
            return v
        if isinstance(v, list) and v:
            first = v[0]
            if isinstance(first, str) and first.startswith("http"):
                return first
            if isinstance(first, dict):
                u = first.get("url") or first.get("src") or ""
                if isinstance(u, str) and u.startswith("http"):
                    return u
    return ""


def _ig_source(s):
    """Normalize an Instagram inputSource URL to the bare tag/handle that surfaced it."""
    s = s or ""
    m = re.search(r"/explore/tags/([^/?#]+)", s)
    if m:
        return m.group(1)
    m = re.search(r"instagram\.com/([^/?#]+)", s)
    if m and m.group(1) not in ("explore", "p", "reel", "reels"):
        return m.group(1)
    return s


def _map_tiktok(item):
    ch = item.get("channel") or {}
    song = item.get("song") or {}
    vid = item.get("video") or {}
    _v = vid if isinstance(vid, dict) else {}
    cover = _pick_url(_v.get("cover"), _v.get("originCover"),
                      item.get("cover"), item.get("covers"),
                      item.get("originCover"), item.get("thumbnail"))
    video_url = _pick_url(_v.get("url"), _v.get("downloadAddr"), _v.get("playAddr"),
                          item.get("mediaUrls"), item.get("videoUrl"), item.get("downloadAddr"))
    return {
        "platform": "tiktok",
        "video_id": item.get("id") or item.get("postPage"),
        "author": ch.get("username") or ch.get("name"),
        "url": item.get("postPage") or item.get("url"),
        "cover": cover,
        "video_url": video_url,
        "caption": item.get("title"),
        "audio_id": str(song.get("id") or ""),
        "audio_name": song.get("title") or "original sound",
        "hashtags": [h for h in (item.get("hashtags") or []) if h],   # strings; drop None
        "followers": _num(ch.get("followers")),     # account follower count
        "account_likes": _num(ch.get("likes")),     # account LIFETIME likes (TikTok profile total)
        "account_posts": _num(ch.get("videos")),    # account total video count
        "source": item.get("inputSource"),          # the keyword/handle that surfaced this post
        "post_time": _ts(item.get("uploadedAt"), item.get("createTime"), item.get("createTimeISO"),
                         item.get("createTimeUnix"), item.get("uploadedAtFormatted")),
        "views": _num(item.get("views")),
        "likes": _num(item.get("likes")),
        "comments": _num(item.get("comments")),
        "shares": _num(item.get("shares")),
    }


def _map_tiktok_cw(item):
    """Clockworks/tiktok-scraper shape — crucially returns the music id as an EXACT
    STRING (musicMeta.musicId), unlike apidojo which rounds the 19-digit id in JS."""
    am = item.get("authorMeta") or {}
    mm = item.get("musicMeta") or {}
    vm = item.get("videoMeta") or {}
    am = am if isinstance(am, dict) else {}
    mm = mm if isinstance(mm, dict) else {}
    vm = vm if isinstance(vm, dict) else {}
    hashtags = []
    for h in (item.get("hashtags") or []):
        hashtags.append(h.get("name") if isinstance(h, dict) else h)
    return {
        "platform": "tiktok",
        "video_id": item.get("id") or item.get("webVideoUrl"),
        "author": am.get("name") or am.get("nickName") or am.get("uniqueId"),
        "url": item.get("webVideoUrl") or item.get("url"),
        "cover": _pick_url(vm.get("coverUrl"), vm.get("originalCoverUrl"),
                           vm.get("cover"), item.get("covers")),
        "video_url": _pick_url(vm.get("downloadAddr"), item.get("mediaUrls"), vm.get("playAddr")),
        "caption": item.get("text"),
        "audio_id": str(mm.get("musicId") or ""),            # EXACT string id
        "audio_name": mm.get("musicName") or "original sound",
        "hashtags": [h for h in hashtags if h],
        "followers": _num(am.get("fans")),
        "account_likes": _num(am.get("heart") or am.get("heartCount")),
        "account_posts": _num(am.get("video") or am.get("videoCount")),
        "source": item.get("input") or item.get("searchQuery") or item.get("hashtag") or "",
        "post_time": _ts(item.get("createTimeISO"), item.get("createTime")),
        "views": _num(item.get("playCount")),
        "likes": _num(item.get("diggCount")),
        "comments": _num(item.get("commentCount")),
        "shares": _num(item.get("shareCount")),
    }


def _ig_audio(item):
    """Best-effort Instagram Reel sound. apidojo's IG output doesn't reliably expose
    audio, so we probe the field shapes other actors use and degrade to none if absent.
    (Verify field names on a real run — see note in radar/sounds.)"""
    for blob in (item.get("musicInfo"), item.get("music"), item.get("audio"),
                 (item.get("clips_metadata") or {}).get("music_info") if isinstance(item.get("clips_metadata"), dict) else None):
        if isinstance(blob, dict):
            mid = blob.get("music_id") or blob.get("audio_id") or blob.get("id") or blob.get("musicId") or ""
            name = (blob.get("song_name") or blob.get("title") or blob.get("name")
                    or blob.get("artist_name") or blob.get("musicName") or "")
            if mid or name:
                return str(mid), name
    return "", ""


def _map_instagram(item):
    owner = item.get("owner") or {}
    likes = _num(item.get("likeCount"))
    aid, aname = _ig_audio(item)
    cover = _pick_url(item.get("displayUrl"), item.get("thumbnailUrl"),
                      item.get("thumbnail"), item.get("images"))
    _vd = item.get("video") if isinstance(item.get("video"), dict) else {}
    video_url = _pick_url(item.get("videoUrl"), _vd.get("url"), item.get("mediaUrls"))
    return {
        "platform": "instagram",
        "video_id": item.get("id") or item.get("code") or item.get("url"),
        "author": owner.get("username"),
        "url": item.get("url"),
        "cover": cover,
        "video_url": video_url,
        "caption": item.get("caption"),
        "audio_id": aid,                      # best-effort (apidojo IG audio unconfirmed)
        "audio_name": aname or "original audio",
        "hashtags": _tags_from_caption(item.get("caption")),
        "followers": _num(owner.get("followerCount")),
        "account_posts": _num(owner.get("postCount")),   # IG total post count (no lifetime-likes API)
        "media_type": "reel" if item.get("isVideo") else "post",   # Reel (video) vs feed post
        "source": _ig_source(item.get("inputSource")),    # the tag/handle that surfaced this post
        "post_time": _ts(item.get("createdAt"), item.get("timestamp"), item.get("takenAt"),
                         item.get("taken_at_timestamp")),
        "views": likes,                       # IG hides view counts → rank by engagement (likes)
        "likes": likes,
        "comments": _num(item.get("commentCount")),
        "shares": 0,
    }


def profiles(handles):
    """Fetch IG profile data (followers, bio, etc.) for a batch of usernames — used by
    discovery to validate + style-match candidates. No rate limits, so we validate ALL
    candidates in ONE call. Returns [{username, followers, posts, is_private, bio,
    full_name, category, verified}]. [] on failure."""
    hs = [h.strip().strip("/").lstrip("@") for h in (handles or []) if h and h.strip()]
    if not hs:
        return []
    actor = os.environ.get("APIFY_PROFILE_ACTOR", "apify/instagram-profile-scraper")
    try:
        items = _run(actor, {"usernames": hs})
    except Exception as e:
        print(f"[apify] profiles failed: {type(e).__name__}: {str(e)[:160]}")
        return []
    out = []
    for r in items:
        if not isinstance(r, dict) or r.get("noResults") or not r.get("username"):
            continue
        ext = r.get("externalUrl") or ""
        if not ext:
            urls = r.get("externalUrls") or []
            if urls and isinstance(urls[0], dict):
                ext = urls[0].get("url") or ""
        # recent posts (for looking at what they ACTUALLY post — brand collections/product
        # drops vs a real person's own content) + the profile pic (for a visual gender/person check)
        lp = r.get("latestPosts") or []
        post_captions, post_images = [], []
        for p in lp[:6]:
            if not isinstance(p, dict):
                continue
            cap = (p.get("caption") or "").strip()
            if cap:
                post_captions.append(cap[:140])
            img = p.get("displayUrl") or (isinstance(p.get("images"), list) and p["images"] and p["images"][0])
            if img:
                post_images.append(img)
        out.append({
            "username": r.get("username"),
            "followers": _num(r.get("followersCount")),
            "posts": _num(r.get("postsCount")),
            "is_private": bool(r.get("private")),
            "bio": (r.get("biography") or "")[:300],
            "full_name": r.get("fullName") or "",
            "category": r.get("businessCategoryName") or "",
            "external_url": ext,
            "verified": bool(r.get("verified")),
            "pic": r.get("profilePicUrlHD") or r.get("profilePicUrl") or "",
            "post_captions": post_captions,
            "post_images": post_images,
        })
    return out


def _map_instagram_sones(item):
    """sones/instagram-posts-scraper-lowcost → standard post dict. Cheap ($0.30/1k) +
    reliable (99.9% success). media_type: 1=photo, 2=video/reel, 8=carousel. No audio /
    follower / bio fields (fine: the likes model doesn't need audio; discovery validates
    via the in-house reader)."""
    user = item.get("user") or {}
    mt = item.get("media_type")
    is_video = (mt == 2) or item.get("product_type") in ("clips", "igtv")
    cap = item.get("caption")
    if isinstance(cap, dict):
        cap = cap.get("text", "")
    code = item.get("code")
    author = (item.get("scraped_username") or user.get("username") or "").lstrip("@") or None
    likes = _num(item.get("like_count"))
    plays = _num(item.get("play_count"))
    capl = (cap or "").lower()
    return {
        "platform": "instagram",
        "video_id": str(item.get("pk") or item.get("id") or code),
        "author": author,
        "url": item.get("post_url") or (f"https://www.instagram.com/p/{code}/" if code else None),
        "cover": item.get("image_url") or "",
        "video_url": item.get("video_url") or "",
        # carousel children so the viewer can swipe every image/clip, not just the cover
        "slides": [{"url": ch.get("image_url") or "", "video_url": ch.get("video_url") or "",
                    "type": "video" if (ch.get("media_type") == 2 or ch.get("video_url")) else "image"}
                   for ch in (item.get("carousel_media") or []) if isinstance(ch, dict)],
        "caption": cap,
        "audio_id": "",
        "audio_name": "original audio" if is_video else None,
        "hashtags": _tags_from_caption(cap),
        "followers": _num(user.get("follower_count")),
        "account_posts": _num(user.get("media_count")),
        # single photo (1) vs carousel/album (8) vs reel (2). Some albums come through with a
        # non-8 media_type but still carry carousel_media children — treat those as carousels too.
        "media_type": "reel" if is_video else ("carousel" if (mt == 8 or len(item.get("carousel_media") or []) > 1) else "post"),
        "source": item.get("scraped_username"),
        "post_time": _ts(item.get("taken_at")),
        "is_sponsored": bool("#ad" in capl or "#sponsored" in capl or "paid partnership" in capl),
        "views": plays if (is_video and plays) else likes,
        "likes": likes,
        "comments": _num(item.get("comment_count")),
        "shares": 0,
    }


def fetch(seeds, scan_index=0):
    """Pull recent posts for the creator's seeds across TikTok + Instagram.
    TikTok uses clockworks by default (exact STRING music ids → reliable sound trends +
    fetchable for sound-discovery); set APIFY_TIKTOK_ENGINE=apidojo to revert (cheaper,
    but rounded music ids)."""
    engine = os.environ.get("APIFY_TIKTOK_ENGINE", "clockworks").strip().lower()
    ig_actor = os.environ.get("APIFY_IG_ACTOR", "apidojo/instagram-scraper")
    per = int(os.environ.get("APIFY_PER_SEED", "30"))

    tt_accounts = [s["value"] for s in seeds if s["platform"] == "tiktok" and s["kind"] in ("account", "self")]
    ig_accounts = [s["value"] for s in seeds if s["platform"] == "instagram" and s["kind"] in ("account", "self")]
    tt_tags = [s["value"] for s in seeds if s["platform"] == "tiktok" and s["kind"] == "hashtag"]
    ig_tags = [s["value"] for s in seeds if s["platform"] == "instagram" and s["kind"] == "hashtag"]

    def _safe(label, actor, payload, mapper):
        # one failing source (e.g. a handle that doesn't exist) must not kill the whole scan
        try:
            items = _run(actor, payload)
            print(f"[apify] {label}: {len(items)} items")
            return [mapper(i) for i in items]
        except Exception as e:
            print(f"[apify] {label} failed: {type(e).__name__}: {str(e)[:160]}")
            return []

    ig_urls = [f"https://www.instagram.com/{u.strip('/').lstrip('@')}/" for u in ig_accounts] \
        + [f"https://www.instagram.com/explore/tags/{t.lstrip('#').replace(' ', '')}/" for t in ig_tags]

    # each actor run is an independent network call — fire them in PARALLEL, not serially
    tasks = []
    if engine == "apidojo":
        profile_actor = os.environ.get("APIFY_TIKTOK_ACTOR", "apidojo/tiktok-profile-scraper")
        tt_search_actor = os.environ.get("APIFY_TIKTOK_HASHTAG_ACTOR", "apidojo/tiktok-scraper")
        if tt_accounts:
            tasks.append(("tiktok-profiles", profile_actor,
                          {"usernames": tt_accounts, "maxItems": per * len(tt_accounts)}, _map_tiktok))
        if tt_tags:
            tasks.append(("tiktok-search", tt_search_actor,
                          {"keywords": tt_tags, "maxItems": per * len(tt_tags)}, _map_tiktok))
    else:  # clockworks (default) — one run handles profiles + keyword search; exact music ids
        cw_actor = os.environ.get("APIFY_TIKTOK_ACTOR", "clockworks/tiktok-scraper")
        if tt_accounts or tt_tags:
            payload = {"resultsPerPage": per, "shouldDownloadVideos": False,
                       "shouldDownloadCovers": False}
            if tt_accounts:
                payload["profiles"] = tt_accounts
                payload["profileScrapeSections"] = ["videos"]
            if tt_tags:
                payload["searchQueries"] = tt_tags
                payload["searchSection"] = "/video"
            tasks.append(("tiktok-clockworks", cw_actor, payload, _map_tiktok_cw))
    # Instagram: sones lowcost actor by default (cheap + reliable, accounts only via
    # `usernames`); APIFY_IG_ENGINE=starturls reverts to the apidojo/official startUrls path.
    ig_engine = os.environ.get("APIFY_IG_ENGINE", "sones").strip().lower()
    if ig_engine == "sones":
        if ig_accounts:
            tasks.append(("instagram-sones", "sones/instagram-posts-scraper-lowcost",
                          {"usernames": [u.strip("/").lstrip("@") for u in ig_accounts],
                           "postsPerProfile": per}, _map_instagram_sones))
        # (sones is profile-only; IG hashtag seeds aren't used in the current model)
    elif ig_urls:
        tasks.append(("instagram", ig_actor,
                      {"startUrls": ig_urls, "maxItems": per * len(ig_urls)}, _map_instagram))

    videos = []
    if tasks:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(tasks)) as ex:
            for res in ex.map(lambda t: _safe(*t), tasks):
                videos += res

    # keep only rows we can score
    return [v for v in videos if v.get("video_id") and v.get("author")]
