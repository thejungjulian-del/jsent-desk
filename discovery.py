"""Auto-discover what to watch for a creator, from their niche alone.

The manager enters only a niche. JS Entertainment derives the hashtags to track; the
radar then discovers the actual accounts automatically from whoever is
overperforming under those hashtags — so no one has to list competitors by hand.

Uses Claude for good niche hashtags when ANTHROPIC_API_KEY is set; otherwise a
simple word-based fallback that always works offline.
"""

import json
import os
import re

import briefs

STOP = {"the", "and", "for", "with", "a", "an", "of", "to", "in", "on", "my",
        "content", "videos", "video", "creator", "niche", "stuff", "about"}

# discovered "similar" accounts must have at least this many followers (the user's own
# example is exempt). Keeps the watch-list to creators of real, comparable size.
MIN_DISCOVER_FOLLOWERS = int(os.environ.get("MIN_DISCOVER_FOLLOWERS", "5000"))


def _clean(w):
    return re.sub(r"[^a-z0-9]", "", w.lower())


def _clean_term(t):
    """Broad search term — keep spaces (TikTok keyword search likes real phrases);
    IG tag URLs get de-spaced at fetch time."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", "", (t or "").lower())).strip()


def _deterministic(niche, gender=None):
    words = [_clean(w) for w in niche.split()]
    words = [w for w in words if w and w not in STOP and len(w) > 2]
    tags = list(dict.fromkeys(words))            # unique, order kept
    if words:
        tags.append("".join(words))              # e.g. gymworkouts
        tags.append(words[0] + "tok")            # e.g. gymtok
    if gender in ("man", "woman") and words:     # gender-specific variants, prioritized
        g = "mens" if gender == "man" else "womens"
        tags.insert(0, g + words[0])             # e.g. mensfashion
        tags.insert(1, g + "".join(words))       # e.g. mensstreetwear
    return list(dict.fromkeys(tags))[:6]


def _llm_hashtags(niche, gender=None):
    import anthropic

    client = anthropic.Anthropic()
    schema = {
        "type": "object",
        "properties": {"hashtags": {"type": "array", "items": {"type": "string"}}},
        "required": ["hashtags"], "additionalProperties": False,
    }
    aud = {"man": " This creator is a MALE — lean the terms to the men's side of the space.",
           "woman": " This creator is a FEMALE — lean the terms to the women's side of the space."}.get(gender, "")
    resp = client.messages.create(
        model=briefs.MODEL,
        max_tokens=400,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content":
            f'Give 8 BROAD search terms to find the most popular/viral videos in this space on '
            f'TikTok & Instagram: "{niche}".{aud} Make them broad enough to surface what is genuinely '
            f'going viral in the wider space — NOT narrow sub-niche tags. Real phrases people search '
            f'(1-3 words each), lowercase, no "#".'}],
    )
    text = next(b.text for b in resp.content if b.type == "text")
    terms = [_clean_term(t) for t in json.loads(text).get("hashtags", [])]
    return [t for t in dict.fromkeys(terms) if t][:8]


def derive_hashtags(niche, gender=None):
    """Broad search terms for the niche (despite the legacy name). Used as TikTok
    keyword searches + Instagram tags. Broad capture; relevance is filtered later by AI."""
    if briefs._llm_enabled():
        try:
            tags = _llm_hashtags(niche, gender)
            if tags:
                return tags
        except Exception:
            pass
    return _deterministic(niche, gender)


def _use_apify():
    """Discovery fetches profiles via Apify (batch, no rate limits) when IG_SCRAPER=apify;
    otherwise the in-house ig_public reader."""
    from sources import apify
    return os.environ.get("IG_SCRAPER", "").strip().lower() == "apify" and apify.enabled()


def _discovery_available():
    from sources import ig_public
    return _use_apify() or ig_public.enabled()


def _fetch_profiles(handles):
    """Profile data for a batch of handles → [{username, followers, posts, is_private, bio,
    full_name, category}]. Apify batches all at once; the in-house path validates one at a
    time (paced, rate-limit-aware via the caller)."""
    from sources import apify, ig_public
    if _use_apify():
        return apify.profiles(handles)
    out = []
    for h in handles:
        info = ig_public.validate(h)   # raises RateLimited (caller handles)
        if info:
            out.append(info)
    return out


def infer_niche(handles, gender=None):
    """Work out a specific content niche (1-3 words) from one or more example accounts.
    Reads EACH account's name/category/bio (more accounts = less misfire), then Claude
    names the common lane. Accepts a single handle or a list. '' if unavailable."""
    if isinstance(handles, str):
        handles = [handles]
    handles = [(h or "").strip().lstrip("@") for h in (handles or []) if (h or "").strip()][:5]
    if not handles or not (briefs._llm_enabled() and _discovery_available()):
        return ""
    try:
        profs = _fetch_profiles(handles)
    except Exception as e:
        print(f"[discovery] infer_niche fetch failed: {type(e).__name__}: {str(e)[:100]}")
        return ""
    if not profs:
        return ""
    lines = []
    for p in profs:
        lines.append(f'@{p.get("username") or ""} — name "{p.get("full_name") or ""}", '
                     f'category "{p.get("category") or ""}", bio: "{(p.get("bio") or "")[:220]}"')
    desc = "\n".join(lines)
    import anthropic
    client = anthropic.Anthropic()
    schema = {"type": "object", "properties": {"niche": {"type": "string"}},
              "required": ["niche"], "additionalProperties": False}
    try:
        resp = client.messages.create(
            model=briefs.MODEL, max_tokens=120,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content":
                f'These Instagram creators are all in the same space:\n{desc}\n\n'
                f'In 1-3 words, what SPECIFIC content niche do they share? Just the topic '
                f'(e.g. "streetwear", "home espresso", "trail running") — NOT broad words like '
                f'"fashion"/"lifestyle", and do NOT include gender. Lowercase.'}],
        )
        text = next(b.text for b in resp.content if b.type == "text")
        return (json.loads(text).get("niche") or "").strip().lower()
    except Exception as e:
        print(f"[discovery] infer_niche llm failed: {type(e).__name__}: {str(e)[:100]}")
        return ""


def build_style_profile(creator_id, anchors=None, niche=None, gender=None):
    """Characterize the seed account(s)' actual STYLE + content from their real posts (already
    scraped — no extra IG calls), so discovery can match on the specific style/vibe/format
    rather than a one-word niche. Niche-agnostic (works for music, cars, beauty, etc.).
    Returns a short profile string and stores it on the creator. '' if no content/AI."""
    import db
    if not briefs._llm_enabled():
        return ""
    creator = db.get_creator(creator_id) or {}
    gender = gender or creator.get("gender")
    niche = niche or creator.get("niche") or ""
    if not anchors:
        anchors = [s["value"].lstrip("@").lower() for s in db.list_seeds(creator_id)
                   if s["kind"] == "account"][:4]
    anchor_set = {a.lstrip("@").lower() for a in anchors}
    # pull the anchors' already-stored posts (captions + sound + type) — no IG call
    vids = [v for v in db.latest_video_stats(creator_id)
            if (v.get("author") or "").lstrip("@").lower() in anchor_set]
    samples = []
    for v in sorted(vids, key=lambda x: int(x.get("likes") or 0), reverse=True)[:18]:
        cap = re.sub(r"\s+", " ", (v.get("caption") or "")).strip()[:160]
        mt = v.get("media_type") or "post"
        samples.append(f'@{v.get("author")} · {mt} · "{cap}"')
    who = {"man": "male", "woman": "female"}.get((gender or "").lower(), "")
    ref = ", ".join("@" + a for a in anchors) or niche
    body = ("\n".join(samples) if samples
            else f'(no posts captured yet — base it on the niche "{niche}" and accounts {ref})')
    schema = {"type": "object", "properties": {"profile": {"type": "string"}},
              "required": ["profile"], "additionalProperties": False}
    prompt = (f'Below are posts from {len(anchor_set)} {who} creator(s) (refs: {ref}) that '
              f'together define a STYLE TERRITORY we want to explore and find MORE accounts '
              f'across. In 2-4 sentences, describe that territory: the shared sub-niche/genre '
              f'and through-line, AND the RANGE/variety it spans (the kinds of content, formats, '
              f'aesthetics and themes within it). The goal is a profile broad enough to capture '
              f'a VARIED set of related creators in this space — not to clone one exact style — '
              f'while still clearly excluding what is genuinely outside it. Posts:\n{body}')
    try:
        import anthropic
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=briefs.MODEL, max_tokens=400,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": prompt}])
        text = next(b.text for b in resp.content if b.type == "text")
        profile = (json.loads(text).get("profile") or "").strip()
        if profile:
            db.set_style_profile(creator_id, profile)
        return profile
    except Exception as e:
        print(f"[discovery] style_profile failed: {type(e).__name__}: {str(e)[:120]}")
        return ""


def _llm_creator_handles(niche, gender=None, examples=None, n=24, profile=None):
    """Ask Claude for candidate Instagram handles of REAL creators like the EXAMPLE(s) the
    creator gave (anchoring on a real handle makes the suggestions far better than a bare
    niche). These are only CANDIDATES — each is validated against the live scraper before
    use, so a hallucinated/dead handle is harmless (it's dropped)."""
    import anthropic
    client = anthropic.Anthropic()
    schema = {
        "type": "object",
        "properties": {"handles": {"type": "array", "items": {"type": "string"}}},
        "required": ["handles"], "additionalProperties": False,
    }
    who = {"man": "MALE creators (men's side of the space)",
           "woman": "FEMALE creators (women's side of the space)"}.get(gender, "creators")
    ex = [e.strip().lstrip("@") for e in (examples or []) if e and e.strip()]
    anchor = (f'The creator likes these accounts: {", ".join("@" + e for e in ex)}. '
              if ex else "")
    style = (f'\nThese accounts define a STYLE TERRITORY: {profile}\nFind a VARIED set of '
             f'creators ACROSS this space (different takes/sub-styles within it), not '
             f'near-identical clones — but all genuinely within the territory.\n'
             if profile else
             f'Find {who} genuinely in this same space, not just the broad "{niche}" label.\n')
    ask = max(n, 30)        # over-ask: many suggestions are unverifiable, so cast a wide net
    resp = client.messages.create(
        model=briefs.MODEL,
        max_tokens=900,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content":
            f'{anchor}{style}'
            f'List up to {ask} Instagram handles of INDIVIDUAL creators/influencers in this space '
            f'(real people, mixing big and mid-size — not just megastars).\n'
            f'CRITICAL — accuracy over quantity:\n'
            f'• Only handles you are genuinely confident EXIST and are active, with a real audience. '
            f'A made-up or wrong handle is WORSE than returning fewer. Do NOT invent, guess, or '
            f'pattern-fill plausible-looking usernames — if you are not sure of the exact handle, omit it.\n'
            f'• NO brands, labels, fashion houses, designers-as-labels, retailers, shops, or magazines '
            f'— only individual human creators.\n'
            f'Return ONLY exact handles, lowercase, no "@".'}],
    )
    text = next(b.text for b in resp.content if b.type == "text")
    raw = json.loads(text).get("handles", [])
    return [h.strip().lstrip("@").lower() for h in raw if h and h.strip()][:ask]


_BRAND_CATEGORIES = ("clothing", "brand", "retail", "apparel", "product/service", "shopping",
                     "store", "wholesale", "boutique", "manufactur", "e-commerce", "commercial",
                     "company")
_BRAND_BIO_SIGNALS = ("shop now", "worldwide shipping", "free shipping", "dm to order",
                      "dm for order", "order now", "shop the", "shop here", "official store",
                      "link to shop", "link in bio to shop", "we ship", "sizes available",
                      "available now", "restock", "new collection", "new drop", "our store",
                      "checkout", "™", "®", "clothing brand", "streetwear brand", "apparel brand")


_STORE_URL_SIGNALS = ("myshopify.com", "shopify", "/collections/", "/products/", "/product/",
                      ".shop", ".store", "/shop", "/store", "depop.com", "etsy.com", "bigcartel",
                      "big.cartel", "storenvy", "gumroad.com", "fourthwall", "teespring",
                      "stan.store", "shopmy.us", "spring.by")
_SOCIAL_URL_HOSTS = ("youtube.com", "youtu.be", "tiktok.com", "instagram.com", "twitter.com",
                     "x.com", "twitch.tv", "spotify.com", "soundcloud.com", "facebook.com",
                     "snapchat.com", "discord.gg", "discord.com", "patreon.com", "cameo.com",
                     "threads.net", "pinterest.com", "onlyfans.com", "linkedin.com", "apple.co",
                     "linktr.ee", "beacons.ai", "lnk.bio", "bio.link", "komi.io")   # ignore these
_DROP_SIGNALS = ("new drop", "just dropped", "latest drop", "next drop", "dropping soon",
                 "drop coming", "shop the drop", "pre-order", "preorder", "sold out", "restock")


def _looks_like_brand(info):
    """Deterministic guard so clothing/streetwear BRANDS + shops don't slip into the watch-list:
    they post product/lookbook shots that look like creator content and sit right in the niche.
    Signals (any one = brand): IG business category; a bio LINK to a STORE (Shopify/Depop/.shop/
    /collections etc. — but NOT a YouTube/TikTok/Linktree link, which real creators use); product
    "drop" language; or ≥2 commerce phrases. We only ever watch individual CREATORS, never labels/
    stores. (Examples/user picks are exempt — checked by the caller.)"""
    cat = (info.get("category") or "").lower()
    if cat and any(k in cat for k in _BRAND_CATEGORIES):
        return True
    text = ((info.get("bio") or "") + " " + (info.get("full_name") or "")).lower()
    url = (info.get("external_url") or "").lower()
    if url and not any(h in url for h in _SOCIAL_URL_HOSTS) and any(s in url for s in _STORE_URL_SIGNALS):
        return True                                    # bio link points to a store
    if any(d in text for d in _DROP_SIGNALS):
        return True                                    # product-"drop" language
    return sum(1 for k in _BRAND_BIO_SIGNALS if k in text) >= 2


def _judge_niche_fit(niche, gender, examples, cands, age=None, profile=None):
    """Claude reads each validated candidate's bio/category/name and keeps ONLY the ones that
    genuinely MATCH THE SEED'S STYLE (when a style profile is given) — not just the broad
    niche — filtering off-style, brands/retailers, wrong-gender, and (if set) clear age
    mismatches. `cands` = [{username, full_name, bio, category, ...}]. Returns kept usernames
    (set). No IG calls (bios already fetched). Falls back to keeping all if AI off / fails."""
    if not (briefs._llm_enabled() and cands):
        return {c["username"].lower() for c in cands}
    who = {"man": "MALE", "woman": "FEMALE"}.get(gender, "")
    ex = ", ".join("@" + e for e in (examples or [])) or "(none given)"
    age_rule = ""
    if age:
        lo, hi = max(16, int(age) - 5), int(age) + 5
        age_rule = (f' The creator is around {int(age)} years old, so prefer creators who look '
                    f'roughly {lo}-{hi}. Exclude only CLEAR age mismatches; if unclear, KEEP.')
    def _cline(c):
        ext = (c.get("external_url") or "").strip()
        caps = " | ".join(c.get("post_captions") or [])[:300]
        return (f'@{c["username"]} · name:"{c.get("full_name","")}" · category:"{c.get("category","")}"'
                f' · bio:"{(c.get("bio") or "")[:160]}"'
                + (f' · website:{ext}' if ext else "")
                + (f' · recent post captions: "{caps}"' if caps else ""))
    lines = [_cline(c) for c in cands]
    schema = {"type": "object", "properties": {"keep": {"type": "array", "items": {"type": "string"}}},
              "required": ["keep"], "additionalProperties": False}
    excl = ('a different niche/topic entirely, ANY brand/label/shop/store — INCLUDING clothing & '
            'streetwear brands that sit right in this niche (if the account SELLS products, has a '
            'shop/website link, uses ™/®, posts product or lookbook/campaign shots rather than '
            'one real person’s own outfits, or its business category is Clothing/Retail/Product/'
            'Shopping/Company, it is a BRAND — EXCLUDE it, even if on-niche), magazines/media '
            'outlets, photographers/videographers/content studios/creative agencies/management '
            '(accounts that SHOOT, PROMOTE, or REPRESENT creators rather than BEING one), or the '
            'wrong gender. Keep ONLY individual real PEOPLE who post THEIR OWN ' + niche + ' content '
            '(their own outfits/fits/looks) — never a company or the businesses around creators')
    if profile:
        target = (f'We are exploring THIS style territory and want a WIDE VARIETY of creators '
                  f'across it (not near-identical clones):\n"{profile}"\n(seed accounts: {ex}). '
                  f'KEEP every username that plausibly belongs ANYWHERE within this space — '
                  f'variety of takes/sub-styles within it is GOOD. EXCLUDE only what is clearly '
                  f'OUTSIDE the territory: {excl}. When unsure but plausibly an in-space creator '
                  f'posting their own content, KEEP.')
    else:
        target = (f'We are building a watch-list of REAL {who} "{niche}" CREATORS, similar to {ex}. '
                  f'KEEP every plausibly individual {who.lower() or ""} creator in or adjacent to '
                  f'the "{niche}" space. ONLY EXCLUDE clear misses: {excl}.')
    prompt = f'{target}{age_rule}\n' + "\n".join(lines)
    try:
        import anthropic
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=briefs.MODEL, max_tokens=800,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": prompt}])
        text = next(b.text for b in resp.content if b.type == "text")
        keep = {h.strip().lstrip("@").lower() for h in json.loads(text).get("keep", [])}
        return keep
    except Exception as e:
        print(f"[discovery] niche-fit judge failed: {type(e).__name__}: {str(e)[:120]}")
        return {c["username"].lower() for c in cands}


def _graph_candidates(seeds, per_account=50, limit=120):
    """Harvest REAL candidate accounts straight from the SEED accounts' own posts — who they
    collaborate with (coauthors), tag, and @mention. Unlike LLM-named handles these are
    guaranteed-real Instagram accounts in the seed's actual scene (it's IG's own data — no
    hallucination), which is the only source that reliably surfaces real niche creators (search
    and hashtags return shops/spam). Ranked by how often each appears across the seeds' posts.
    Returns a list of usernames (seeds removed). [] if Apify off / scrape fails."""
    from collections import Counter
    hs = [h.strip().strip("/").lstrip("@").lower() for h in (seeds or []) if h and h.strip()]
    if not hs or not _use_apify():
        return []
    from sources import apify
    try:
        items = apify._run("sones/instagram-posts-scraper-lowcost",
                           {"usernames": hs, "postsPerProfile": per_account})
    except Exception as e:
        print(f"[discovery] graph scrape failed: {type(e).__name__}: {str(e)[:140]}")
        return []
    counts = Counter()
    for it in items:
        if not isinstance(it, dict):
            continue
        for key in ("coauthor_producers", "invited_coauthor_producers"):   # collabs = strongest
            for c in (it.get(key) or []):
                u = (c.get("username") if isinstance(c, dict) else c) or ""
                if u:
                    counts[u.lower().lstrip("@")] += 3
        for t in (it.get("usertags") or []):                               # tagged on the post
            u = ""
            if isinstance(t, dict):
                u = (t.get("user") or {}).get("username") or t.get("username") or ""
            elif isinstance(t, str):
                u = t
            if u:
                counts[u.lower().lstrip("@")] += 2
        cap = it.get("caption")
        if isinstance(cap, dict):
            cap = cap.get("text", "")
        for m in re.findall(r"@([A-Za-z0-9._]{2,30})", cap or ""):          # @mentions in caption
            counts[m.lower()] += 1
    for s in hs:                                                            # never re-suggest a seed
        counts.pop(s, None)
    stop = {"in", "the", "and", "for", "you", "your", "this", "that", "with", "from",
            "our", "out", "all", "new", "get", "are", "was", "has"}          # common-word junk captures
    return [u for u, _ in counts.most_common(limit)
            if u and len(u) >= 3 and u not in stop and not u.endswith(".")]


_VISION_MODEL = os.environ.get("JSENT_VISION_MODEL") or os.environ.get("TRENDY_VISION_MODEL", briefs.MODEL)


def _vision_verify(niche, gender, cands):
    """Actually LOOK at each candidate — their profile pic + a few recent posts — and keep only
    real individual people of the target gender. Drops brands/product/clothing-label accounts and
    wrong-gender that the text pass missed. Fail-open: no images or any error → the candidate is
    KEPT, so a vision hiccup can never wipe out real creators. Only filters when gender is set."""
    if not (briefs._llm_enabled() and gender in ("man", "woman") and cands):
        return {c["username"].lower() for c in cands}
    import anthropic
    import base64
    import requests
    client = anthropic.Anthropic()
    opp = "woman" if gender == "man" else "man"
    schema = {"type": "object", "properties": {
        "individual": {"type": "boolean"}, "gender": {"type": "string", "enum": ["man", "woman", "unclear"]}},
        "required": ["individual", "gender"], "additionalProperties": False}
    keep, dropped = set(), []
    for c in cands:
        u = c["username"].lower()
        urls = [x for x in ([c.get("pic")] + list(c.get("post_images") or [])) if x][:3]
        imgs = []
        for url in urls:
            try:
                r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
                if r.ok and r.headers.get("Content-Type", "").startswith("image"):
                    imgs.append(base64.standard_b64encode(r.content).decode())
            except Exception:
                pass
        if not imgs:
            keep.add(u)                      # nothing to look at → don't drop
            continue
        content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b}} for b in imgs]
        content.append({"type": "text", "text":
            f'These are the profile picture and recent posts of Instagram account @{c["username"]} '
            f'(name "{c.get("full_name","")}"). Answer: individual = is this ONE real individual person '
            f'posting their OWN content (a creator) — NOT a brand / store / clothing-label / product '
            f'account (product shots, logos, many different people, no consistent single person). '
            f'gender = what gender does that person present as.'})
        try:
            resp = client.messages.create(model=_VISION_MODEL, max_tokens=100,
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
                messages=[{"role": "user", "content": content}])
            j = json.loads(next(b.text for b in resp.content if b.type == "text"))
            if j.get("individual") and j.get("gender") != opp:
                keep.add(u)
            else:
                dropped.append(f'@{u}({"brand" if not j.get("individual") else j.get("gender")})')
        except Exception as e:
            print(f"[discovery] vision @{u} failed: {type(e).__name__}: {str(e)[:90]}")
            keep.add(u)                      # fail-open on errors
    if dropped:
        print(f"[discovery] vision dropped {len(dropped)}: {', '.join(dropped[:12])}")
    return keep


def find_creators(niche, gender=None, formats=None, examples=None, limit=50, exclude=None, age=None, profile=None):
    """Build a watch-list of real creator accounts for a niche, anchored on the EXAMPLE(s).
    Two-stage accuracy: (1) validate each candidate live — exists, public, has posts, ≥the
    follower floor; (2) a Claude niche-fit pass reads the survivors' bios and keeps only
    genuine {niche} creators (drops off-niche/brands/wrong-gender). `exclude` = handles
    already watched (so incremental growth never re-adds). Returns account seeds. Paced +
    rate-limit-aware. The examples themselves are always kept (the user's explicit picks)."""
    from sources import ig_public
    if not _discovery_available():
        return []

    exclude_set = {(e or "").strip().lstrip("@").lower() for e in (exclude or [])}
    example_set = {e.strip().lstrip("@").lower() for e in (examples or []) if e and e.strip()}

    candidates, seen = [], set()
    for h in example_set:
        if h not in exclude_set and h not in seen:
            seen.add(h); candidates.append(h)
    # PRIMARY source: real accounts harvested from the seeds' own posts (collabs/tags/@mentions).
    # This is the only source that reliably finds real niche creators (LLM hallucinates, search
    # returns shops). The LLM is kept below only as a supplement (useful for mainstream niches).
    try:
        n_graph = 0
        for h in _graph_candidates(examples):
            if h not in exclude_set and h not in seen:
                seen.add(h); candidates.append(h); n_graph += 1
        if n_graph:
            print(f"[discovery] graph: +{n_graph} real candidates from seeds' posts")
    except Exception as e:
        print(f"[discovery] graph candidates failed: {type(e).__name__}: {str(e)[:120]}")
    if briefs._llm_enabled():
        try:
            for h in _llm_creator_handles(niche, gender, examples=examples, n=limit, profile=profile):
                if h not in exclude_set and h not in seen:
                    seen.add(h); candidates.append(h)
        except Exception as e:
            print(f"[discovery] llm handles failed: {type(e).__name__}: {str(e)[:120]}")

    # fetch profiles, then filter to public + has-posts + ≥follower-floor (examples exempt)
    survivors = []
    cap = max(limit * 4, 40)        # validation is one batched Apify call, so cast a wide net
    if _use_apify():
        # one batched Apify call — no rate limits, no pacing
        infos = {p["username"].lower(): p for p in _fetch_profiles(candidates[:cap])}
        for h in candidates[:cap]:
            info = infos.get(h)
            if not info or info["is_private"] or info["posts"] <= 0:
                continue
            if h not in example_set and info["followers"] < MIN_DISCOVER_FOLLOWERS:
                continue
            if h not in example_set and _looks_like_brand(info):     # drop clothing brands/shops
                continue
            info["_is_example"] = h in example_set
            survivors.append(info)
    else:
        # in-house: validate one at a time, paced + rate-limit-aware
        import random, time as _time
        checked = 0
        try:
            for h in candidates:
                if len(survivors) >= limit * 2 or checked >= cap:
                    break
                checked += 1
                info = ig_public.validate(h)
                if not info or info["is_private"] or info["posts"] <= 0:
                    continue
                if h not in example_set and info["followers"] < MIN_DISCOVER_FOLLOWERS:
                    continue
                if h not in example_set and _looks_like_brand(info):    # drop clothing brands/shops
                    continue
                info["_is_example"] = h in example_set
                survivors.append(info)
                _time.sleep(random.uniform(2.0, 4.0))
        except ig_public.RateLimited:
            print(f"[discovery] rate-limited after {checked} checks — judging {len(survivors)} so far")

    # stage 2: STYLE-fit (free — bios already fetched). Examples always pass.
    judged = _judge_niche_fit(niche, gender, examples,
                              [s for s in survivors if not s["_is_example"]], age=age, profile=profile)
    # visual pass: LOOK at the survivors the text judge kept — drop brands/product accounts and
    # wrong-gender that text alone missed (examples are trusted picks, not re-checked).
    vok = _vision_verify(niche, gender,
                         [s for s in survivors if not s["_is_example"] and s["username"].lower() in judged])
    kept = []
    for s in survivors:
        u = s["username"].lower()
        if s["_is_example"] or (u in judged and u in vok):
            kept.append({"platform": "instagram", "kind": "account", "value": s["username"]})
        if len(kept) >= limit:
            break
    print(f"[discovery] {niche}/{gender or 'any'}: {len(kept)} kept "
          f"({len(candidates)} candidates, {len(survivors)} passed size, "
          f"{'apify' if _use_apify() else 'in-house'})")
    return kept


def grow_watchlist(creator_id, target=None, per_run=None):
    """Top up each of a creator's niches toward TARGET accounts, a small accurate batch at a
    time — so a niche fills to a full roster over several scans WITHOUT a rate-limit burst.
    Adds only NEW, niche-fit, ≥follower-floor accounts (dedup vs the whole niche). Safe on
    one account (small per_run). Returns {niche: added_count}."""
    import db
    target = target or int(os.environ.get("WATCHLIST_TARGET", "50"))
    per_run = per_run or int(os.environ.get("WATCHLIST_GROW_PER_RUN", "15"))
    creator = db.get_creator(creator_id)
    if not creator:
        return {}
    gender = creator.get("gender")
    formats = creator.get("formats") or []
    out = {}
    # refresh the style profile from the seed's real (already-scraped) content so growth
    # matches the exact style — no extra IG calls. Reuse the stored one if profiling fails.
    profile = build_style_profile(creator_id) or creator.get("style_profile")
    niches = [n.strip() for n in (creator.get("niche") or "").split(",") if n.strip()]
    for nz in niches:
        hub_id = db.ensure_hub(nz, gender)
        db.sync_hub_seeds(nz, gender)             # make sure the hub reflects current seeds
        have = [s["value"] for s in db.list_seeds(hub_id)
                if s["kind"] == "account"]
        if len(have) >= target:
            out[nz] = 0
            continue
        need = min(per_run, target - len(have))
        # Anchor on watch accounts we HAVEN'T mined for collabs yet — newest first (their
        # networks are unexplored, so the frontier always advances instead of re-mining the same
        # accounts and stalling). When every account has been mined this cycle, reset and go
        # again (accounts pick up new collabs over time). `have` is oldest→newest.
        mined = db.mined_accounts(nz, gender)
        unmined = [h for h in have if h.lower() not in mined]
        if not unmined:                                  # whole roster mined → start a fresh cycle
            db.reset_mined(nz, gender)
            unmined = list(have)
        anchors = list(reversed(unmined))[:6]            # newest un-mined accounts (frontier
        #  advances 6/run over repeat clicks — keeps each run fast + cheap vs mining all 12)
        # NEVER re-add an account the manager removed (a deleted brand must not sneak back).
        blocked = db.blocked_accounts(nz, gender)
        exclude = list({h.lower() for h in have} | blocked)
        found = find_creators(nz, gender, formats, examples=anchors,
                              limit=need, exclude=exclude, age=creator.get("age"), profile=profile)
        for s in found:
            db.add_seed(creator_id, "instagram", "account", s["value"], niche=nz)
        db.mark_mined(nz, gender, anchors)               # these anchors are now explored
        out[nz] = len(found)
        print(f"[grow] {nz}: +{len(found)} (anchors={len(anchors)} unmined, now ~{len(have) + len(found)}/{target})")
    return out


def derive_seeds(niche, formats=None, gender=None):
    """Hashtag/topic seeds to watch, scoped to what the creator actually makes:
       short-form -> TikTok + Instagram, long-form -> YouTube. Streaming uses the
       group stream radar, not per-creator seeds."""
    formats = formats or ["short", "long"]   # sensible default
    platforms = []
    if "short" in formats:
        platforms += ["tiktok", "instagram"]
    if "long" in formats:
        platforms += ["youtube"]
    if "photo" in formats:
        platforms += ["instagram_photo"]
    if not platforms:                          # stream-only creator
        return []
    tags = derive_hashtags(niche, gender)
    return [{"platform": p, "kind": "hashtag", "value": t}
            for t in tags for p in platforms]
