"""The radar: one scan for one creator/niche.

Pipeline:
  fetch videos for the seeds  ->  log them
  -> compute each account's baseline
  -> flag overperforming "hits"
  -> cluster hits by trend-unit (audio / hashtag)
  -> keep units that span multiple accounts
  -> score momentum vs the last scan  ->  BUY / WATCH / SATURATED
"""

import os
import re
import threading
import time

import ai_trends
import db
import free_radar
import scoring
from sources import apify, ig_public, ig_session, mock, photos, sounds, tiktok_web, youtube

_SCAN_LOCK = threading.Lock()

# textual gender markers, to keep opposite-gender content out of a creator's lane.
# (heuristic — catches obvious cases like #womensfashion; visual-only needs the AI key.)
_FEMALE = ("women", "woman", "girl", "female", "ladies", "feminine", "girly")
_MALE = ("mens", "menswear", "male", "guys", "boys", "masculine", "gentlemen")


def _coded_gender(text):
    low = (text or "").lower()
    if any(t in low for t in _FEMALE):           # check female first ("women" contains "men")
        return "woman"
    if any(t in low for t in _MALE) or re.search(r"\bmen\b|\bman\b", low):
        return "man"
    return None


def maybe_scan_all(min_interval=None):
    """Throttled, non-blocking radar refresh for every creator. Called on page
    loads so trends stay fresh without anyone clicking Scan; actually runs at most
    once per min_interval. Default 12h (tunable via RADAR_SCAN_INTERVAL seconds) to
    keep Apify credit use sane now that real scraping is on."""
    if min_interval is None:
        try:
            min_interval = int(os.environ.get("RADAR_SCAN_INTERVAL", "43200"))
        except ValueError:
            min_interval = 43200
    try:
        last = float(db.get_setting("radar_last_scan", 0) or 0)
    except (TypeError, ValueError):
        last = 0
    if time.time() - last < min_interval:
        return
    db.set_setting("radar_last_scan", str(time.time()))   # claim the slot up front
    threading.Thread(target=_scan_all_safe, daemon=True).start()


def _scan_all_safe():
    if not _SCAN_LOCK.acquire(blocking=False):
        return
    try:
        scan_niches()           # one shared scan per niche+gender (not per creator)
    except Exception:
        pass
    finally:
        _SCAN_LOCK.release()


def _gather(seeds, scan_index, niche=""):
    """Pull videos/posts from every platform a creator's seeds cover."""
    videos = []
    # Instagram scraping engine. IG_SCRAPER=apify routes scanning through Apify (the sones
    # lowcost actor — cheap + reliable); otherwise the free in-house scrapers (public
    # endpoint, then logged-in session). Apify SCANNING is separate from discovery, which
    # still uses the in-house reader regardless — so IG_PUBLIC can stay on for discovery.
    ig_via_apify = (os.environ.get("IG_SCRAPER", "").strip().lower() == "apify") and apify.enabled()
    ig_seeds = [s for s in seeds if s["platform"] == "instagram"]
    ig_handled = False
    if ig_seeds and not ig_via_apify and ig_public.enabled():
        videos += ig_public.fetch(ig_seeds, scan_index=scan_index)
        ig_handled = True
    elif ig_seeds and not ig_via_apify and ig_session.enabled():
        videos += ig_session.fetch(ig_seeds, scan_index=scan_index)
        ig_handled = True
    # when ig_via_apify, ig_handled stays False → the short_form block below sends IG to Apify
    # SCAN_TIKTOK=0 turns TikTok off entirely (Instagram-only mode). Default on.
    scan_tiktok = os.environ.get("SCAN_TIKTOK", "1").strip().lower() not in ("0", "false", "no", "")
    # TikTok via our own headless-browser scraper (free), when enabled.
    tt_seeds = [s for s in seeds if s["platform"] == "tiktok"]
    tt_handled = False
    if tt_seeds and scan_tiktok and tiktok_web.enabled():
        videos += tiktok_web.fetch(tt_seeds, scan_index=scan_index)
        tt_handled = True
    # whatever's left (TikTok if in-house off, IG if in-house off) falls back to Apify/mock.
    short_form = [s for s in seeds
                  if (s["platform"] == "tiktok" and scan_tiktok and not tt_handled)
                  or (s["platform"] == "instagram" and not ig_handled)]
    if short_form:
        if apify.enabled():
            videos += apify.fetch(short_form, scan_index=scan_index)
        else:
            videos += mock.fetch(short_form, scan_index=scan_index, niche=niche)
    yt = [s for s in seeds if s["platform"] == "youtube"]
    if yt:
        videos += youtube.fetch(yt, scan_index=scan_index)
    ph = [s for s in seeds if s["platform"] == "instagram_photo"]
    if ph:
        videos += photos.fetch(ph, scan_index=scan_index)
    return videos


def scan_niches():
    """Scan every distinct niche+gender ONCE, shared across all creators in it.

    Each niche+gender has a hidden 'hub' creator whose watch-list is the union of its
    creators' accounts. We sync that list, then run the normal per-creator scan on the
    hub. Real creators read the hub's trends (see app.creator_feed), so scraping + AI cost
    scale with the number of NICHES, not the number of creators."""
    done = []
    for niche, gender in db.distinct_niche_genders():
        hub_id, n_accounts = db.sync_hub_seeds(niche, gender)
        if not n_accounts:
            print(f"[scan_niches] {niche}/{gender or 'any'}: no accounts to scan — skipping")
            continue
        print(f"[scan_niches] {niche}/{gender or 'any'}: hub {hub_id}, {n_accounts} accounts")
        try:
            res = run_scan(hub_id)
            done.append({"niche": niche, "gender": gender, "hub_id": hub_id,
                         **{k: res.get(k) for k in ("videos_seen", "hits", "trends")}})
        except Exception as e:
            print(f"[scan_niches] {niche}/{gender or 'any'} failed: {type(e).__name__}: {str(e)[:160]}")
    return done


def scan_for_creator(creator_id):
    """On-demand scan (the 'Scan now' button): refresh THIS creator's niche lane(s) by
    scanning the shared hub(s) for their niche+gender. Feeds read those hubs, so this is
    what makes a creator's page update. Sets the creator's own scan-done marker so the
    button stops waiting even when the result is shared."""
    creator = db.get_creator(creator_id)
    if not creator:
        return {"error": "no such creator"}
    gender = creator.get("gender")
    niches = [n.strip() for n in (creator.get("niche") or "").split(",") if n.strip()]
    out = []
    for nz in niches:
        hub_id, n_acc = db.sync_hub_seeds(nz, gender)
        if not n_acc:
            print(f"[scan_for_creator] {nz}/{gender or 'any'}: no accounts — skipping")
            continue
        try:
            out.append(run_scan(hub_id))
        except Exception as e:
            print(f"[scan_for_creator] {nz} failed: {type(e).__name__}: {str(e)[:160]}")
    db.set_setting(f"scan_done_{creator_id}", str(time.time()))   # button stops waiting
    # NOTE: watch-list growth is now a SEPARATE action (the 'Find creators' button →
    # grow_for_creator), so a trend scan never spends IG/Apify calls expanding the roster.
    return {"creator_id": creator_id, "niches": out}


def grow_for_creator(creator_id):
    """Expand a creator's niche watch-list by a small accurate batch — the 'Find creators'
    button, now separate from the trend scan. Tops each niche toward the target without a
    rate-limit burst. Best-effort. Returns {niche: added_count}."""
    creator = db.get_creator(creator_id)
    if not creator:
        return {"error": "no such creator"}
    added = {}
    try:
        import discovery
        added = discovery.grow_watchlist(creator_id)
    except Exception as e:
        print(f"[grow] {creator_id} failed: {type(e).__name__}: {str(e)[:160]}")
    n = sum(added.values()) if isinstance(added, dict) else 0
    db.set_setting(f"grow_added_{creator_id}", str(n))
    db.set_setting(f"grow_done_{creator_id}", str(time.time()))
    return {"creator_id": creator_id, "added": added}


def reset_for_creator(creator_id):
    """Clean-slate a creator's niche lane(s): wipe the hub's old trends + scraped posts, then
    re-scan. (Per-niche, so this clears the SHARED lane for everyone in that niche+gender.)"""
    creator = db.get_creator(creator_id)
    if not creator:
        return {"error": "no such creator"}
    gender = creator.get("gender")
    for nz in [n.strip() for n in (creator.get("niche") or "").split(",") if n.strip()]:
        hub_id = db.ensure_hub(nz, gender)
        db.reset_scanned_for_relane(hub_id, keep_authors=[])     # hubs have no own posts
    return scan_for_creator(creator_id)


def _scan_index(creator_id):
    """How many distinct scans we've already run (drives mock ramp + momentum)."""
    conn = db.connect()
    n = conn.execute(
        "SELECT COUNT(DISTINCT scan_time) AS n FROM trend_snapshots WHERE creator_id=?",
        (creator_id,),
    ).fetchone()["n"]
    conn.close()
    return n


def run_scan(creator_id):
    seeds = db.list_seeds(creator_id)
    if not seeds:
        return {"error": "no seeds — add at least one account or hashtag"}

    creator = db.get_creator(creator_id)
    niche = creator["niche"] if creator else ""
    scan_time = time.time()
    fetched = _gather(seeds, _scan_index(creator_id), niche)

    # drop paid-partnership / sponsored posts — they trend on ad spend, not because the
    # format works, so they'd pollute the "what's genuinely performing" read. (KEEP_ADS=1
    # to disable.) Filtered at intake so they never enter the pool.
    if os.environ.get("KEEP_ADS", "").strip().lower() not in ("1", "true", "yes", "on"):
        before = len(fetched)
        fetched = [v for v in fetched if not v.get("is_sponsored")]
        if before - len(fetched):
            print(f"[ads] dropped {before - len(fetched)} sponsored posts")

    # sound-based discovery: pull more posts using the hot sounds in this batch
    # (finds trend content that never carried the niche hashtag). OFF unless enabled.
    if sounds.enabled():
        try:
            fetched += sounds.expand(fetched)
        except Exception as e:
            print(f"[sounds] expand failed: {type(e).__name__}: {str(e)[:160]}")

    for v in fetched:
        vid = db.upsert_video(v, creator_id)
        db.insert_snapshot(vid, v, scan_time)

    # judge against everything we know about this niche (latest stat per video)
    videos = db.latest_video_stats(creator_id)

    # only count posts from accounts CURRENTLY on the watch-list — so a removed/dead account's
    # old scraped posts can't linger as "trends" after it's dropped.
    watched = {(s["value"] or "").lstrip("@").lower()
               for s in seeds if s["kind"] == "account"}
    if watched:
        videos = [v for v in videos if (v.get("author") or "").lstrip("@").lower() in watched]

    # keep opposite-gender content out of the lane (e.g. women's posts for a man) —
    # applied to the whole pool so it covers both AI detection and hashtag clustering.
    opp = {"man": "woman", "woman": "man"}.get(creator.get("gender") if creator else None)
    if opp:
        videos = [v for v in videos
                  if _coded_gender(" ".join(v.get("hashtags") or []) + " " + (v.get("caption") or "")) != opp]

    # trends must be CURRENT: drop posts older than the recency window (DB accumulates posts
    # across scans, and old evergreen mega-hits would otherwise win).
    dated = sum(1 for v in videos if v.get("post_time"))
    videos = scoring.recent(videos)
    before_pop = len(videos)
    # The "top" (relative-performance) model judges each post vs its OWN account's recent
    # norm, so it needs the FULL recent pool — skip the absolute popularity floor there.
    # Other modes keep the absolute floor.
    if os.environ.get("TREND_MODE", "top").strip().lower() != "top":
        videos = scoring.popular(videos)
    if dated:
        print(f"[recency] creator {creator_id}: {dated} dated, {before_pop} recent, "
              f"{len(videos)} in pool")

    # which niche each post belongs to — by the seed keyword that surfaced it, so a
    # creator's separate niches (e.g. fashion vs gym) cluster into separate lanes.
    niches = [n.strip() for n in (creator["niche"].split(",") if creator else []) if n.strip()] or [""]
    seed_niche = {s["value"].lower(): s.get("niche")
                  for s in seeds if s["kind"] in ("hashtag", "account") and s.get("niche")}

    def _video_niche(v):
        if len(niches) == 1:
            return niches[0]                       # single niche → everything is that lane
        return seed_niche.get((v.get("source") or "").lower())

    # ADAPT-ANYTHING: do NOT drop off-niche viral content — keep the broad popular pool;
    # ai_trends.detect surfaces what's viral anywhere and writes a per-niche 'your_take'
    # adaptation for each (only opposite-gender/can't-recreate is excluded, in the prompt).
    # (The textual gender filter already ran on `videos` above.)
    relevant_by_niche = {nz: [v for v in videos if _video_niche(v) == nz] for nz in niches}
    relevant = [v for lst in relevant_by_niche.values() for v in lst]

    # PURE VIRALITY among the niche-relevant posts: the biggest, fastest-climbing — but
    # now only ones that actually fit the lane.
    hits = scoring.viral_posts(relevant)
    by_niche = {}
    for v in hits:
        nz = _video_niche(v)
        if nz is None:
            continue
        by_niche.setdefault(nz, []).append(v)
    prior = db.prior_trend_counts(creator_id, scan_time)

    # promote the niche's most viral accounts to recurring seeds (now only on-niche ones,
    # since by_niche came from the relevance-gated pool).
    _discover_accounts(creator_id, seeds, by_niche)

    # ── DETECTION ──
    # TREND_MODE=top (default): BEST-PERFORMING — surface the top posts per content type
    #   (TikTok / Reels / Posts), each with a 'your take'. Works with or without the AI key
    #   (AI just enriches titles). TREND_MODE=trends: the old cross-creator trend detector.
    mode = os.environ.get("TREND_MODE", "top").strip().lower()
    if mode == "top":
        def _top_niche(nz):
            try:
                return ai_trends.top_posts(creator, nz, relevant_by_niche.get(nz, []),
                                           scan_time, other_niches=niches)
            except Exception as e:
                print(f"[ai_trends] top_posts {nz} failed: {type(e).__name__}: {str(e)[:160]}")
                return []
        results = []
        for nz in niches:
            for row in _top_niche(nz):
                db.insert_trend_snapshot(row)
                results.append(row)
        # free YouTube overperformance lane (niche-in, no Apify) — only for niches whose
        # creators make YouTube content, so we never spend quota on a lane no one reads.
        if free_radar.enabled():        # only with a real YouTube key — never surface demo data in prod
            for nz in niches:
                if free_radar.wants(nz, creator.get("gender") if creator else None):
                    try:
                        results += free_radar.scan(creator_id, nz, creator.get("gender") if creator else None, scan_time)
                    except Exception as e:
                        print(f"[free_radar] {nz} failed: {type(e).__name__}: {str(e)[:160]}")
        return _scan_result(creator_id, scan_time, fetched, hits, results, creator)

    if ai_trends.enabled():
        # each niche's detection is independent → run niches in parallel (no DB writes
        # inside the threads; rows are inserted serially after).
        def _detect_niche(nz):
            try:
                return ai_trends.detect(creator, nz, relevant_by_niche.get(nz, []),
                                        scan_time, other_niches=niches)
            except Exception as e:
                print(f"[ai_trends] {nz} failed: {type(e).__name__}: {str(e)[:160]}")
                return []

        if len(niches) > 1:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(niches))) as ex:
                niche_rows = list(ex.map(_detect_niche, niches))
        else:
            niche_rows = [_detect_niche(nz) for nz in niches]

        results = []
        for rows in niche_rows:
            for row in rows:
                db.insert_trend_snapshot(row)
                results.append(row)
        return _scan_result(creator_id, scan_time, fetched, hits, results, creator)

    # ── fallback: sound/format clustering (no ANTHROPIC_API_KEY) ──
    results = []
    for nz, nhits in by_niche.items():
        for row in _cluster_niche(creator_id, scan_time, nhits, nz, prior, opp):
            db.insert_trend_snapshot(row)
            results.append(row)

    return _scan_result(creator_id, scan_time, fetched, hits, results, creator)


def _scan_result(creator_id, scan_time, fetched, hits, results, creator):
    # streamers also get a niche-scoped "what to go live with" scan
    if creator and "stream" in (creator.get("formats") or []):
        import stream_radar
        stream_radar.scan(creator)

    results.sort(key=lambda r: r["score"], reverse=True)
    db.set_setting(f"scan_done_{creator_id}", str(scan_time))   # marks completion even if 0 trends
    return {
        "creator_id": creator_id,
        "source": "live" if (apify.enabled() or youtube.enabled()) else "demo",
        "videos_seen": len(fetched),
        "hits": len(hits),
        "trends": len(results),
        "scan_time": scan_time,
        "top": results[:5],
    }


def _units(video):
    """The trend-units a video belongs to → (type, key, label, platform).
    Sounds + video formats drive trends, so those are the units — NOT hashtags
    (hashtags are only an intake/search signal, never a trend on their own).
    Audio is platform-specific (a TikTok sound ≠ an Instagram sound)."""
    units = []
    pf = video.get("platform")
    if video.get("audio_id"):
        units.append(("audio", f"audio:{video['audio_id']}",
                      video.get("audio_name") or video["audio_id"], pf))
    if video.get("topic"):
        if pf == "instagram_photo":
            units.append(("photo", f"photo:{video['topic'].lower()}", video["topic"], "instagram"))
        else:
            units.append(("topic", f"topic:{video['topic'].lower()}", video["topic"], "youtube"))
    return units


def _discover_accounts(creator_id, seeds, by_niche):
    """Add the niche's most viral accounts as recurring 'account' seeds (capped) so
    future scans read their feeds directly. Real mode only."""
    if not apify.enabled():
        return
    cap = int(os.environ.get("DISCOVERED_ACCOUNTS_MAX", "6"))
    existing = [s for s in seeds if s["kind"] == "account"]
    slots = cap - len(existing)
    if slots <= 0:
        return
    self_h = {s["value"].lstrip("@").lower() for s in seeds if s["kind"] == "self"}
    have = {(s["platform"], s["value"].lstrip("@").lower()) for s in existing}

    cands = []
    for nz, nhits in by_niche.items():
        for v in nhits:
            pf = v.get("platform")
            if pf not in ("tiktok", "instagram"):     # photo/youtube aren't profile-scraped here
                continue
            h = (v.get("author") or "").lstrip("@").lower()
            if not h or h in self_h or (pf, h) in have:
                continue
            cands.append((scoring.virality(v), pf, v.get("author"), nz))
    cands.sort(key=lambda x: x[0], reverse=True)

    added = set()
    for _op, pf, author, nz in cands:
        if slots <= 0:
            break
        key = (pf, author.lstrip("@").lower())
        if key in added:
            continue
        db.add_seed(creator_id, pf, "account", author, nz)
        added.add(key)
        slots -= 1


def _cluster_niche(creator_id, scan_time, nhits, nz, prior, opp, only_types=None):
    """Cluster a niche's most VIRAL posts into trend-unit rows (audio / topic / photo),
    weighted by virality (reach + recency). `only_types` limits which unit types to
    emit (e.g. {'audio'} for the viral-sound pass that runs alongside AI detection)."""
    clusters = {}
    for v in nhits:
        for unit_type, key, label, platform in _units(v):
            if only_types and unit_type not in only_types:
                continue
            if opp and _coded_gender(label) == opp:        # drop opposite-gender units
                continue
            c = clusters.setdefault(key, {
                "unit_type": unit_type, "unit_label": label, "pf": {},
                "accounts": set(), "hit_count": 0, "overperf_sum": 0.0, "fresh": [],
            })
            if platform:
                c["pf"][platform] = c["pf"].get(platform, 0) + 1
            c["accounts"].add(v.get("author"))
            c["hit_count"] += 1
            c["overperf_sum"] += scoring.virality(v)        # 'overperf_sum' col now holds viral weight
            c["fresh"].append(scoring.freshness(v.get("post_time")))

    rows = []
    for key, c in clusters.items():
        acct_n = len(c["accounts"])
        if acct_n < scoring.MIN_ACCOUNTS:
            continue
        mom = scoring.momentum(c["hit_count"], prior.get(key, 0))
        st = scoring.status(acct_n, mom)
        avg_fresh = sum(c["fresh"]) / len(c["fresh"]) if c["fresh"] else 0
        score = scoring.trend_score(c["hit_count"], acct_n, c["overperf_sum"], avg_fresh)
        rows.append({
            "creator_id": creator_id, "scan_time": scan_time,
            "unit_type": c["unit_type"], "unit_key": key, "unit_label": c["unit_label"],
            "platform": (max(c["pf"], key=c["pf"].get) if c["pf"] else None),
            "hit_count": c["hit_count"], "account_count": acct_n,
            "overperf_sum": round(c["overperf_sum"], 2), "score": round(score, 2),
            "momentum": mom, "status": st, "niche": nz,
            "medium": "photo" if c["unit_type"] == "photo" else "video",
            "sound": c["unit_label"] if c["unit_type"] == "audio" else None,
        })
    return rows
