"""AI trend detection — let Claude read the overperforming posts in a creator's
niche and name the ACTUAL trends (the format / concept / hook), not just hashtags.

It also filters to the creator's exact niche + gender/aesthetic as it reads, so a
men's-gym creator never gets women's-fashion suggestions. Falls back to hashtag
clustering (radar.py) when ANTHROPIC_API_KEY isn't set.
"""

import json
import os
import re
import time

import briefs
import frames
import scoring

MODEL = os.environ.get("JSENT_TREND_MODEL") or os.environ.get("TRENDY_TREND_MODEL", briefs.MODEL)
VISION = (os.environ.get("JSENT_VISION") or os.environ.get("TRENDY_VISION", "1")).strip().lower() not in ("", "0", "false", "no")
VISION_MAX = int(os.environ.get("VISION_MAX", "8"))    # how many top posts to analyze visually (cost/time cap)
VISION_FRAMES = int(os.environ.get("VISION_FRAMES", "8"))   # frames sampled evenly across each video
MIN_RIDERS = int(os.environ.get("MIN_RIDERS", "2"))         # a trend needs >= this many DIFFERENT creators riding it
TREND_LENS = os.environ.get("TREND_LENS", "both").strip().lower()   # both | sound | format
                                                            # (2: realistic for same-sound popular posts w/o sound-discovery)

TREND_SCHEMA = {
    "type": "object",
    "properties": {
        "trends": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Name the trend recognizably in a few words — the dance, the sound, or the format (e.g. 'the Jie.Muzic lean-in pose', 'bootcut jeans for men', 'unexpected color-pairing tips'). Recognizable, not vague like 'street style', but DON'T over-describe every detail."},
                    "concept": {"type": "string", "description": "A SHORT plain recognition of the trend (1-2 sentences): what it IS at a glance — the dance / the sound / the format — and why it's taking off. Recognize it at a HIGH level; do NOT dissect every structural beat."},
                    "why_it_works": {"type": "string", "description": "The specific, replicable mechanism that makes this work across the cited posts (1-2 sentences) — the transferable formula, NOT the individual creator's personality or luck."},
                    "your_take": {"type": "string", "description": "How THIS creator turns this viral trend into a video in THEIR niche — the specific adaptation. Keep the trend's format/mechanic (the sound, the transition, the structure), swap the content to the creator's niche. E.g. a viral dance trend → 'do the same beat-synced moves but it's an outfit-change on each drop'. 1-2 concrete sentences."},
                    "shared_element": {"type": "string", "description": "In a few words, the ONE concrete thing the cited posts have in common that defines the trend (e.g. 'the X sound + slow-mo outfit reveal on the beat'). This is the proof it's a real cross-creator trend, not a one-off."},
                    "status": {"type": "string", "enum": ["buy", "watch"],
                               "description": "buy = jump on it now; watch = warming up."},
                    "medium": {"type": "string", "enum": ["video", "photo"],
                               "description": "Is this trend a VIDEO trend (TikTok / Instagram Reel) or a PHOTO trend (static Instagram photo or carousel post)?"},
                    "sound": {"type": "string",
                              "description": "If this trend is driven by a specific SOUND/audio, the song/sound name (from the posts' 'sound:' tags). Empty string if it's not sound-driven."},
                    "example_indices": {"type": "array", "items": {"type": "integer"},
                                        "description": "Indices of ALL the posts in the list RIDING this trend (the same sound) — at least 2, from DIFFERENT accounts. These are the videos riding the trend; if fewer than 2 ride it, it is NOT a trend."},
                },
                "required": ["title", "concept", "why_it_works", "your_take", "shared_element", "status", "medium", "sound", "example_indices"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["trends"],
    "additionalProperties": False,
}


def enabled():
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:48] or "trend"


TOP_PER_TYPE = int(os.environ.get("TOP_PER_TYPE", "8"))        # best posts shown by default + AI-labeled
STORE_PER_TYPE = int(os.environ.get("STORE_PER_TYPE", "24"))   # total high-performers stored (rest = "show more")
PER_AUTHOR_CAP = int(os.environ.get("PER_AUTHOR_CAP", "2"))   # max posts from one account per section
OUTPERFORM_MIN = float(os.environ.get("OUTPERFORM_MIN", "1.0"))   # must beat the account's own median by this ×


def _content_type(v):
    """Which of the three content types a post is: 'tiktok' | 'reels' | 'post'."""
    if v.get("platform") == "tiktok":
        return "tiktok"
    if v.get("media_type") in ("post", "carousel") or v.get("platform") == "instagram_photo":
        return "post"
    return "reels"                                  # instagram video / reel


def _likes(v):
    return int(v.get("likes") or 0)


def _like_baselines(posts, acct_overall=None):
    """Median LIKES per account FOR ONE CONTENT TYPE, so a reel is judged vs the account's
    other reels and a photo vs their other photos (photos get more likes than reels, so a
    shared baseline would bias photos). For an account seen <2× in THIS type, fall back to that
    account's OWN overall median (across all its posts) — never the tiny global median, so a
    big-following account's single post isn't measured against everyone's norm and falsely
    flagged as a huge overperformer (that's the follower-skew guard)."""
    import statistics
    by = {}
    for v in posts:
        by.setdefault((v.get("author") or "?").lower(), []).append(_likes(v))
    bucket_med = statistics.median([_likes(v) for v in posts]) if posts else 0
    base = {}
    for a, ls in by.items():
        if len(ls) >= 2:
            base[a] = statistics.median(ls)
        else:
            base[a] = (acct_overall or {}).get(a) or bucket_med
    return base, bucket_med


def top_posts(creator, niche, posts, scan_time=0.0, other_niches=None):
    """RELATIVE-PERFORMANCE model: for each watched account, surface the recent posts that
    OUTPERFORM that account's own recent norm (by LIKES — the one metric both reels & photos
    expose; Instagram hides photo views). Split into the three content types, each judged
    against that account's OTHER posts of the SAME type. Posts with no likes / hidden likes
    are disregarded. Each becomes a card to recreate, with a short title + a 'your take'.
    Returns rows in detect()'s shape (storage + feed pipeline unchanged)."""
    # likes is the universal metric; drop posts with hidden/missing likes
    posts = [v for v in posts if v.get("url") and _likes(v) > 0]
    if not posts:
        return []
    # the SAME post can end up in the DB twice (its id keyed by pk vs code across scrapes) —
    # dedup by post URL so an identical post never surfaces twice; keep the higher-likes row.
    _by_url = {}
    for v in posts:
        u = v["url"]
        if u not in _by_url or _likes(v) > _likes(_by_url[u]):
            _by_url[u] = v
    posts = list(_by_url.values())

    buckets = {"tiktok": [], "reels": [], "post": []}
    for v in posts:
        buckets[_content_type(v)].append(v)
    # each account's OWN overall median (across all its posts), used as the fallback baseline
    # for accounts we've seen <2× in a given content type — keeps follower-size out of it.
    import statistics
    _by_acct = {}
    for v in posts:
        _by_acct.setdefault((v.get("author") or "?").lower(), []).append(_likes(v))
    acct_overall = {a: statistics.median(ls) for a, ls in _by_acct.items() if ls}
    chosen, ratio_of, to_label = [], {}, []
    for ctype, lst in buckets.items():
        base, bucket_med = _like_baselines(lst, acct_overall)   # baseline within THIS content type

        def overperf(v):
            b = base.get((v.get("author") or "?").lower()) or bucket_med or 1
            return _likes(v) / (b or 1)

        lst.sort(key=overperf, reverse=True)        # most-overperforming first
        per_author, kept = {}, []
        for v in lst:
            if overperf(v) <= OUTPERFORM_MIN:        # must beat the account's own norm
                continue
            a = (v.get("author") or "").lower()
            if per_author.get(a, 0) >= PER_AUTHOR_CAP:   # diversity: cap per account
                continue
            per_author[a] = per_author.get(a, 0) + 1
            ratio_of[id(v)] = overperf(v)
            kept.append(v)
            if len(kept) >= STORE_PER_TYPE:          # store a deep bench for "show more"
                break
        to_label += kept[:TOP_PER_TYPE]              # only the top few per type get an AI title
        chosen += [(ctype, v) for v in kept]
    if not chosen:
        return []

    labels = _label_posts(creator, niche, to_label) if _llm_labels_enabled() else {}

    out = []
    for ctype, v in chosen:
        vid = str(v.get("video_id") or v.get("url"))
        lab = labels.get(vid) or {}
        title = lab.get("title") or _caption_title(v)
        premise = lab.get("premise") or ""
        is_post = ctype == "post"
        ratio = ratio_of.get(id(v), 1.0)
        # carousel (multi-image album) vs single photo vs video — kept distinct in `medium`
        medium = ("carousel" if v.get("media_type") == "carousel"
                  else "photo" if is_post else "video")
        out.append({
            "creator_id": creator["id"], "scan_time": scan_time,
            "unit_type": "concept", "unit_key": f"top:{ctype}:{vid}",
            "unit_label": title, "platform": v.get("platform"),
            "hit_count": 1, "account_count": 1,
            "overperf_sum": round(ratio, 2),
            "score": round(ratio, 4),               # rank by how much it beat its own norm
            "momentum": "rising", "status": "buy", "niche": niche,
            "medium": medium,
            "description": premise or (v.get("caption") or "").strip()[:400],
            "examples": [{"url": v.get("url"), "author": v.get("author") or "",
                          "cover": v.get("cover") or "", "video_url": v.get("video_url") or "",
                          "slides": v.get("slides") or [],
                          "views": int(v.get("views", 0) or 0), "likes": _likes(v)}],
            "sound": (v.get("audio_name") if not is_post and v.get("audio_name")
                      not in (None, "", "original sound", "original audio") else None),
            "your_take": lab.get("your_take") or "", "channel": ctype,
            "platforms": [{"tiktok": "TikTok", "reels": "Instagram Reels",
                           "post": "Instagram"}[ctype]],
        })
    out.sort(key=lambda r: -r["score"])
    return out


def _caption_title(v):
    """A short human title from a post's caption when AI labelling is unavailable."""
    cap = re.sub(r"\s+", " ", (v.get("caption") or "")).strip()
    cap = re.sub(r"#\w+", "", cap).strip()
    if cap:
        return cap[:70]
    return f"@{v.get('author') or 'creator'} — top {_content_type(v)} post"


def _llm_labels_enabled():
    return briefs._llm_enabled()


def _label_posts(creator, niche, posts):
    """One Claude call to write a clear TITLE + plain-English PREMISE for every chosen post.
    Returns {video_id: {title, premise}}. {} on any failure (caller falls back to caption
    titles)."""
    gender = (creator.get("gender") or "").lower()
    who = {"man": "male", "woman": "female"}.get(gender, "")
    lines = []
    for v in posts:
        vid = str(v.get("video_id") or v.get("url"))
        ct = {"tiktok": "TikTok video", "reels": "Instagram Reel", "post": "Instagram photo post"}[_content_type(v)]
        cap = re.sub(r"\s+", " ", (v.get("caption") or "")).strip()[:300]
        snd = v.get("audio_name") or ""
        perf = v.get("views") or v.get("likes") or 0
        lines.append(f'id={vid} · {ct} · @{v.get("author")} · {perf:,} · sound:{snd} · "{cap}"')
    schema = {"type": "object", "properties": {"posts": {"type": "array", "items": {
        "type": "object", "properties": {
            "id": {"type": "string"},
            "title": {"type": "string", "description": "A clear, plain-English title (5-10 words) for what the "
                      "creator is actually DOING in this post — readable as a phrase a person instantly gets, NOT a "
                      "pile of stacked keywords. E.g. 'Walking through the city in a layered fall outfit' or "
                      "'Trying on five thrifted jackets and rating each'. Avoid vague tag-soup like 'city street style reel'."},
            "premise": {"type": "string", "description": "Two or three plain sentences describing what this post IS "
                        "and DOES for someone who hasn't seen it: what happens in it, the hook / format / structure, "
                        "and why it grabs attention. Concrete and specific. No hashtags, no emoji."},
        }, "required": ["id", "title", "premise"], "additionalProperties": False}}},
        "required": ["posts"], "additionalProperties": False}
    prompt = (f'These are top-performing posts in the "{niche}" space ({who or "any"} creator). You are briefing a '
              f'busy creator-manager who will NOT watch each post. So for EACH post: write a clear readable title '
              f'for what the creator is doing, and a short premise (2-3 sentences) describing what the post is and '
              f'does so they understand it without watching. Be concrete; never use tag-soup or vague labels. '
              f'(You only have the caption + format + sound, so infer the premise sensibly from those.) '
              f'Posts:\n' + "\n".join(lines))
    try:
        import anthropic
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=MODEL, max_tokens=6000,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": prompt}])
        text = next(b.text for b in resp.content if b.type == "text")
        return {p["id"]: {"title": p.get("title", ""), "premise": p.get("premise", "")}
                for p in json.loads(text).get("posts", [])}
    except Exception as e:
        print(f"[ai_trends] label_posts failed: {type(e).__name__}: {str(e)[:120]}")
        return {}


def _sound_key(name):
    """Normalize a song/sound name so the same audio under cosmetically different names
    collapses to one key. '' (e.g. bare 'original sound') means 'no reliable name' — never
    merge those together."""
    n = re.sub(r"\boriginal (sound|audio)\b", "", (name or "").lower())
    n = re.sub(r"\(.*?\)|\[.*?\]", "", n)          # drop "(sped up)", "[remix]", etc
    n = re.sub(r"[^a-z0-9]+", "", n)
    return n


def _merge_sounds(rows):
    """Collapse sound trends that are really the same sound — keyed on shared audio_id OR
    matching normalized song name. The higher-ranked trend wins; the duplicate's example
    posts are folded in. Format trends (no sound) pass through untouched."""
    out, by_aid, by_name = [], {}, {}
    for r in rows:
        aid = r.pop("_aid", "")
        if not r.get("sound"):
            out.append(r)
            continue
        nk = _sound_key(r.get("sound"))
        tgt = by_aid.get(aid) if aid else None
        if tgt is None and nk:
            tgt = by_name.get(nk)
        if tgt is not None:                        # duplicate → fold examples into the winner
            urls = {e["url"] for e in tgt["examples"]}
            for e in r["examples"]:
                if e["url"] not in urls:
                    tgt["examples"].append(e)
                    urls.add(e["url"])
            tgt["examples"].sort(key=lambda e: e.get("views", 0), reverse=True)
            tgt["examples"] = tgt["examples"][:6]
            tgt["hit_count"] = max(tgt["hit_count"], r["hit_count"])
            tgt["account_count"] = max(tgt["account_count"], r["account_count"])
            continue
        out.append(r)
        if aid:
            by_aid[aid] = r
        if nk:
            by_name[nk] = r
    return out


_RELEVANCE_SCHEMA = {
    "type": "object",
    "properties": {"keep": {"type": "array", "items": {"type": "integer"}}},
    "required": ["keep"], "additionalProperties": False,
}


def filter_relevant(creator, niche, posts):
    """Keep only posts genuinely in the creator's niche+gender lane — run BEFORE virality
    ranking so the biggest GENERIC viral creators (dances/comedy/other niches) don't crowd
    out the actual niche. Cheap text pass; returns the on-niche subset (falls back to all
    posts if the AI is off, the pool is tiny, or it errors)."""
    if not enabled() or len(posts) < 6:
        return posts
    cand = sorted(posts, key=scoring.virality, reverse=True)[:250]   # screen wide — top-virality skews generic
    gender = (creator.get("gender") or "").lower()
    who = {"man": "a MALE", "woman": "a FEMALE"}.get(gender, "a")
    lines = []
    for i, p in enumerate(cand):
        cap = re.sub(r"\s+", " ", (p.get("caption") or "")).strip()[:120]
        tags = " ".join("#" + str(h) for h in (p.get("hashtags") or [])[:6])
        snd = p.get("audio_name") or ""
        lines.append(f"[{i}] @{p.get('author')}{' · sound: ' + snd if snd else ''}\n    {cap}\n    {tags}")
    prompt = f"""Screen these posts for {who} "{niche}" creator's lane. Return the indices of posts that \
genuinely belong to "{niche}" — content this exact creator could plausibly make. DROP everything off-niche: \
generic dance / comedy / lip-sync, other niches, big-creator content that isn't really about "{niche}", and \
opposite-gender-body content. Be STRICT — when unsure, drop it. Better to keep 10 truly on-niche posts than \
100 vaguely-related ones.

POSTS:
{chr(10).join(lines)}"""
    try:
        import anthropic
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=MODEL, max_tokens=1500,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": _RELEVANCE_SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
        keep = json.loads(next(b.text for b in resp.content if b.type == "text")).get("keep", [])
        kept = [cand[i] for i in keep if 0 <= i < len(cand)]
        print(f"[relevance] {niche}: kept {len(kept)}/{len(cand)} on-niche")
        return kept or posts          # never strand the scan with an empty pool
    except Exception as e:
        print(f"[relevance] {niche} failed: {type(e).__name__}: {str(e)[:120]}")
        return posts


def detect(creator, niche, posts, scan_time=0.0, other_niches=None):
    """Return radar-style trend rows for one niche, identified by Claude from the
    posts' actual content. `posts` = the niche's overperforming posts (dicts with
    caption/hashtags/audio_name/likes/views/author/media_type/url). `other_niches` =
    the creator's OTHER lanes, which must never leak into this one."""
    cutoff = time.time() - scoring.RECENCY_DAYS * 86400      # only current posts
    pool = [p for p in posts if (p.get("caption") or p.get("hashtags") or p.get("audio_name"))]
    pool = [p for p in pool if p.get("post_time") is None or p["post_time"] >= cutoff]
    if len(pool) < 4:
        return []
    lane_total = len(pool)

    # lane-wide adoption across ALL posts (viral OR not) — the verification signal: a sound
    # used by many posts is a real theme; one only a single viral post has is a fluke.
    sound_count = {}
    for p in pool:
        aid = p.get("audio_id")
        if aid:
            sound_count[aid] = sound_count.get(aid, 0) + 1
    with_audio = sum(1 for p in pool if p.get("audio_id"))
    shared2 = sum(1 for c in sound_count.values() if c >= 2)
    print(f"[ai_trends] {niche}: pool={len(pool)} with_audio={with_audio} "
          f"distinct_sounds={len(sound_count)} sounds_with_2+={shared2}")

    # candidates = most viral posts (per medium so photos keep their slots); then a sample
    # of ordinary posts so the AI can verify a theme RECURS beyond the viral hits.
    _vids = sorted([p for p in pool if not scoring.is_photo(p)], key=scoring.virality, reverse=True)
    _photos = sorted([p for p in pool if scoring.is_photo(p)], key=scoring.virality, reverse=True)
    candidates = _vids[:55] + _photos[:20]
    n_cand = len(candidates)
    extra = (_vids[55:] + _photos[20:])[:35]          # corroboration sample (lower-engagement)
    posts = candidates + extra

    import anthropic
    client = anthropic.Anthropic()

    gender = (creator.get("gender") or "").lower()
    who = {"man": "a MALE", "woman": "a FEMALE"}.get(gender, "a")
    others = [n for n in (other_niches or []) if n and n.lower() != niche.lower()]
    exclude = (f' This creator ALSO has SEPARATE lanes for: {", ".join(others)} — those are '
               f'handled on their own. Do NOT include, reference, or borrow from them here; '
               f'every trend must be purely about "{niche}".') if others else ""
    MT = {"reel": "Instagram Reel", "post": "Instagram static photo/carousel post",
          "video": "TikTok video"}
    has_static = any((p.get("media_type") in ("post", "carousel")) for p in posts)
    lines = []
    for i, p in enumerate(posts):
        tags = " ".join("#" + str(h) for h in (p.get("hashtags") or [])[:8])
        snd = p.get("audio_name") or ""
        cap = re.sub(r"\s+", " ", (p.get("caption") or "")).strip()[:160]
        mt = MT.get(p.get("media_type"), p.get("platform") or "")
        adopt = sound_count.get(p.get("audio_id"), 0)
        adopt_s = f" · this sound is on {adopt} posts in the lane" if (p.get("audio_id") and adopt > 1) else ""
        flag = "VIRAL" if i < n_cand else "regular"
        lines.append(f"[{i}] ({flag}) @{p.get('author')} · {int(p.get('likes',0) or 0):,} likes"
                     f"{' · '+mt if mt else ''}{' · sound: '+snd if snd else ''}{adopt_s}\n    {cap}\n    {tags}")

    # vision: send REAL frames from each top video (not the posed cover); for photos the
    # post image IS the content. Video downloads run in PARALLEL (the slow part of a scan).
    vis_blocks = []          # [(post_index, [image_block, ...]), ...]
    if VISION:
        targets = []         # the top candidates worth a visual: (index, post, kind)
        for i, p in enumerate(posts):
            if i >= n_cand or len(targets) >= VISION_MAX:
                break
            if scoring.is_photo(p) and p.get("cover"):
                targets.append((i, p, "photo"))
            elif (not scoring.is_photo(p)) and p.get("video_url"):
                targets.append((i, p, "video"))

        def _imgs(item):
            i, p, kind = item
            if kind == "photo":
                return (i, [{"type": "image", "source": {"type": "url", "url": p["cover"]}}])
            blocks = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b}}
                      for b in frames.extract(p["video_url"], n=VISION_FRAMES)]
            return (i, blocks)

        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
            vis_blocks = [(i, imgs) for i, imgs in ex.map(_imgs, targets) if imgs]
        vis_blocks.sort(key=lambda x: x[0])
    vision_note = ("\nVISUALS attached below: real frames sampled from the top videos (and the image for photo "
                   "posts), each labeled by post index. LOOK at them — identify the real visual theme (the pose, "
                   "outfit/styling, setting, framing, movement/transition), not just the caption. Group posts whose "
                   "VISUALS show the same formula.") if vis_blocks else ""

    prompt = f"""You are the trend analyst at a creator agency, working for {who} "{niche}" creator. Below are the \
most VIRAL recent posts (biggest reach, fastest-growing) — pulled BROADLY, so they're from MANY niches, not just {niche}.

Identify the distinct VIRAL TRENDS right now — the real content concepts/formats/hooks/sounds blowing up (from ANY \
niche), NOT just popular hashtags. Group the posts by the underlying idea. Every post is RECENT (last \
{scoring.RECENCY_DAYS} days) — all current. For EACH trend you must also give 'your_take': how {who} "{niche}" creator \
adapts that trend into a {niche} video (keep the format/mechanic, swap the content to {niche}).

Recognize trends at a HIGH LEVEL — if a bunch of videos are doing the same dance or using the same sound, that is \
ONE trend (name it simply, e.g. "the Jie.Muzic lean-in pose"). Do NOT split it into hyper-specific variants or \
dissect every structural beat — keep titles and descriptions simple and recognizable.

CRITICAL — a TREND = a format/sound MANY creators are riding. Find the trend, then find its riders:
- Posts are tagged (VIRAL) = a top performer, or (regular) = an ordinary post in the same lane. There are \
{lane_total} posts in this lane total; each sound shows how many lane posts use it.
- Method: take a (VIRAL) post as a candidate, identify its specific element (a sound, a format, a hook, a look), \
then FIND THE OTHER POSTS in the list riding that SAME element. List ALL of them (the riders) in example_indices.
- HARD RULE: a trend must have **at least {MIN_RIDERS} posts from DIFFERENT accounts riding it**. If you cannot \
find {MIN_RIDERS}+ genuine riders in the list, it is NOT a trend — DROP it. Example to DROP: a single distinctive \
video like a "mom reacts to my outfit" skit that no one else is copying — viral, but a one-off, not a trend.
- Do NOT loosely group merely-similar videos to hit the count — they must genuinely ride the SAME element.
- Prefer trends with the WIDEST adoption. Quality over quantity — return only real, well-ridden trends; if the \
lane has only 2 real trends, return 2.

ADAPT, don't drop: a trend being off-niche (a dance, a comedy bit, a different niche) is NOT a reason to skip it — \
surface it and put the {niche} spin in 'your_take'. The ONLY things to EXCLUDE: content built around the OPPOSITE \
GENDER's body/appearance that {who} creator couldn't recreate (a MALE creator can't do a "girl in tight pants" \
body clip), and trends that physically require something this creator plainly cannot do. Everything else viral is \
fair game — adapt it.{exclude} Never surface a trend WITHOUT a concrete {niche} 'your_take'.
{('Some posts are Instagram STATIC photo/carousel posts (not video) — make sure you ALSO surface distinct trends for static photo posts, not only video/Reels.') if has_static else ''}

LEAD WITH SOUND. Two kinds of trend:
(A) SOUND trends — FIRST and most important: whatever viral SONG/SOUND is trending (see "sound:" / "this sound is \
on N posts in the lane"). Any sound multiple popular videos are riding is a trend — name it by the song, put it in \
'sound'. Surface these FIRST. ONE sound = ONE trend: if the same song appears under slightly different names (a \
remix, "sped up", an artist-vs-title spelling), that is a SINGLE trend — do NOT list it twice.
(B) FORMAT / VISUAL trends — SECOND: a recurring look/outfit-formula/transition/pose/format with NO shared song \
(e.g. "bootcut jeans for men", "unexpected color-pairing tips"). Leave 'sound' empty.
Do NOT force a song onto a format trend, and do NOT skip a real format trend just because it has no common sound.
{vision_note}

Return UP TO 12 trends. ORDER: SOUND trends first (whatever's trending), THEN format trends. Only real cross-creator \
trends (don't pad), distinct, not rephrasings. For each, give a simple recognizable title; the ONE shared element \
(shared_element); a SHORT plain recognition of the trend (concept, 1-2 sentences — what it is + why it's popping, NOT \
a beat-by-beat dissection); the single sharpest why-it-works line; the {niche} adaptation \
(your_take); whether to jump on now (buy) or it's still warming up (watch); whether it's a video trend or a picture \
trend (medium); the driving sound if any (sound); and the indices of the example posts that define it.

POSTS:
{chr(10).join(lines)}"""

    content = [{"type": "text", "text": prompt}]
    for i, imgs in vis_blocks:
        content.append({"type": "text", "text": f"post [{i}] — {len(imgs)} frame(s):"})
        content.extend(imgs)

    def _call(msg_content):
        return client.messages.create(
            model=MODEL,
            max_tokens=6000,
            output_config={"effort": "high", "format": {"type": "json_schema", "schema": TREND_SCHEMA}},
            messages=[{"role": "user", "content": msg_content}],
        )

    try:
        resp = _call(content)
    except Exception as e:
        # images can be unfetchable/expired → don't lose the scan, fall back to text-only
        print(f"[ai_trends] vision call failed ({type(e).__name__}: {str(e)[:120]}) — text-only retry")
        resp = _call(prompt)
    text = next(b.text for b in resp.content if b.type == "text")
    trends = json.loads(text).get("trends", [])

    out = []
    for rank, t in enumerate(trends):
        ex = [posts[i] for i in t.get("example_indices", []) if 0 <= i < len(posts)]
        if len({e.get("author") for e in ex if e.get("author")}) < MIN_RIDERS:
            continue                    # the AI's cited riders must span >= MIN_RIDERS creators

        # Is this a SOUND trend (cited examples share one audio) or a FORMAT trend (no shared song)?
        aids = [e.get("audio_id") for e in ex if e.get("audio_id")]
        dom_aid = max(set(aids), key=aids.count) if aids else ""
        is_sound = bool(dom_aid and sum(1 for e in ex if e.get("audio_id") == dom_aid) >= 2)
        if TREND_LENS == "sound" and not is_sound:
            continue
        if TREND_LENS == "format" and is_sound:
            continue
        if is_sound:                      # SOUND trend → riders = every post on that exact sound
            riders = sorted([p for p in pool if p.get("audio_id") == dom_aid],
                            key=scoring.virality, reverse=True)
            snds = [r.get("audio_name") for r in riders
                    if (r.get("audio_name") or "") not in ("", "original sound", "original audio")]
            sound = (t.get("sound") or "").strip() or (max(set(snds), key=snds.count) if snds else "")
        else:                             # FORMAT trend → riders = the cited posts; NO song
            riders = sorted(ex, key=scoring.virality, reverse=True)
            sound = ""

        authors = {r.get("author") for r in riders if r.get("author")}
        if len(authors) < MIN_RIDERS:    # the ACTUAL riders must also be many
            continue
        examples = [{"url": r.get("url"), "author": r.get("author") or "",
                     "views": int(r.get("views", 0) or 0), "likes": int(r.get("likes", 0) or 0)}
                    for r in riders if r.get("url")][:6]   # videos following this trend

        shared = (t.get("shared_element") or "").strip()
        concept = t.get("concept", "")
        description = f"What these {len(authors)} creators share: {shared}. {concept}" if shared else concept
        medium = "photo" if t.get("medium") == "photo" else "video"
        # every platform this trend's riders actually came from (correct source attribution)
        _PLAT = {"tiktok": "TikTok", "instagram": "Instagram Reels", "instagram_photo": "Instagram"}
        platforms = sorted({_PLAT[p] for r in riders if (p := r.get("platform")) in _PLAT})
        # primary section: photo carousel, or video → tiktok vs reels by where most riders are
        if medium == "photo":
            channel = "carousel"
        else:
            plats = [r.get("platform") for r in riders]
            channel = "tiktok" if plats.count("tiktok") >= sum(1 for p in plats if p == "instagram") else "reels"
        out.append({
            "creator_id": creator["id"], "scan_time": scan_time,
            "unit_type": "concept", "unit_key": f"ai:{niche.lower()}:{_slug(t['title'])}",
            "unit_label": t["title"], "platform": None,
            "hit_count": len(riders), "account_count": max(len(authors), 1),
            "overperf_sum": float(sum(int(r.get("likes", 0) or 0) for r in riders)),
            "score": round(100.0 - rank, 2),       # Claude already ranked them
            "momentum": "rising", "status": t.get("status", "buy"), "niche": niche,
            "medium": medium, "description": description, "examples": examples, "sound": sound,
            "your_take": (t.get("your_take") or "").strip(), "channel": channel, "platforms": platforms,
            "_aid": dom_aid if is_sound else "",   # temp: dedup key, stripped before return
        })

    # ── merge sound trends that are really the SAME sound — Claude (or the data) can name
    #    one audio two different ways, or the same song carries different audio_ids across
    #    re-uploads/platforms. Collapse on shared audio_id OR normalized song name. ──
    out = _merge_sounds(out)

    # SOUND trends first, then format — re-stamp score so the downstream sort keeps that order
    out.sort(key=lambda r: (0 if r.get("sound") else 1, -r["score"]))
    for i, r in enumerate(out):
        r["score"] = round(100.0 - i, 2)
    return out
