# Free-Platform Trend Engine — Build Spec (v2: unified watch-accounts model)

_Status: DESIGN. Supersedes v1 (niche-search). Decisions locked 2026-07-07 across a design convo._

## 0. Intent

**Every platform works the SAME way Instagram already does: watch a list of real creators, find
what overperforms their own baseline, cluster into trends.** No niche search, no niche identification
required. For any creator, any niche.

Why this model (user's reasoning): watching real creators captures the *variety* in a space (one
gaming creator plays a single game, another jumps around) instead of flattening it into one niche
search. It's grounded in creators you actually care about, it drops the fragile `infer_niche` step,
and — because YouTube/Twitch APIs are free — you can watch many creators at almost no cost.

## 1. The one model (all four platforms)

```
watch accounts (per platform)  ->  pull each account's recent CONTENT with view counts
   ->  a piece of content OUTPERFORMING that account's own median = a hit
   ->  cluster the hits into named SUBJECTS (Claude reads titles/captions)
   ->  a subject is a trend if it spans >= MIN_ACCOUNTS watched accounts
   ->  rank by overperformance  ->  store as trend_snapshots rows
```

This is the Instagram scanner, generalized. Per-account median baseline = neutralizes account size
(a small creator's breakout beats a big creator's routine post). Same philosophy the user confirmed
earlier: **relative overperformance, not biggest, not rising.**

| Platform | Watch | Content unit | Source | Cost |
|---|---|---|---|---|
| Instagram | accounts | posts / reels | Apify (existing) | paid |
| TikTok | accounts | videos | Apify (existing, currently off) | paid |
| YouTube | channels | recent uploads | YouTube Data API v3 | free |
| Twitch | streamers | VODs (+ live snapshot) | Twitch Helix | free |

## 2. Per-platform data path

### Instagram / TikTok (already built)
Account watchlist -> Apify scrapes recent posts -> `scoring.account_baselines` /
`ai_trends` overperformance + concept-naming. No change. Keep the per-niche HUB so creators in one
lane SHARE a scrape (Apify cost control) — see §5.

### YouTube (rebuild from the v1 niche-search version)
1. Resolve each watched channel handle -> channelId (`channels.list?forHandle`, 1 unit; batch 50).
2. Get each channel's uploads playlist id (`channels.list part=contentDetails`, batched, 1 unit/50).
3. Recent uploads per channel (`playlistItems.list`, 1 unit/call) -> video ids.
4. Stats (`videos.list part=statistics,snippet`, batched 50, 1 unit) -> views/title/publishedAt.
- **Quota: ~50–60 units to watch ~50 channels** (vs 100 units for ONE niche search). Watching is
  cheap; this is a big win over v1.
- Baseline = each channel's **median recent-upload views** (real median now — we have their history,
  so this is more faithful than v1's subscriber-ratio proxy). Overperformer = views > median × MULT.

### Twitch (the VOD model — new)
Twitch is live, but it HAS a content archive: VODs.
1. Resolve streamer login -> user_id (`/users`, batch 100).
2. Recent VODs (`/videos?user_id=&type=archive&first=N`) -> each has **view_count**, title, date.
3. Baseline = streamer's **median VOD views**; overperformer = a VOD beating it.
4. Subject = inferred from the VOD **title** (Helix VODs have no structured game field) — AI reads
   titles, same as the YouTube lane.
- **Bonus live overlay:** when a watched streamer is live during a scan (`/streams?user_id=`), also
  capture current game + concurrent viewers as a secondary real-time signal.
- **Caveats (honest):** VOD retention is short + optional — auto-deleted after 7d (regular) / 14d
  (Affiliate/Prime) / 60d (Partner), and some streamers disable VODs. So coverage is partial and
  recent-only (recent-only is FINE — we want current trends). Include Highlights (persist longer) too.

## 3. Scoring — relative overperformance (unchanged)

Per account, baseline = median views of that account's recent content. A piece OUTPERFORMS if
`views > baseline × OUTPERF_MULT`. Reuse `scoring.account_baselines` + `scoring.overperformance`
directly (this is what they were built for — and now every account has multiple items, so the median
is real, no proxy needed). Guards from the IG model: FLOOR (drop tiny), and a subject must span
>= MIN_ACCOUNTS distinct watched accounts. Rank by aggregate overperformance × account spread. No
rising/momentum.

## 4. Setup UX — no niche step

The creator just **adds who they watch, per platform** (IG handles, YouTube channels, Twitch logins).
That's the whole input. Trends = overperformance across those accounts.
- The platform picker (built 2026-07-07) already declares WHICH platforms a creator uses; extend
  each platform's section to collect that platform's watch handles.
- `infer_niche` becomes OPTIONAL (kept only as a grouping/label convenience + to seed discovery).

## 5. The niche's residual role (don't fully delete it)

Niche stops being load-bearing for detection, but it still earns its keep in two places:
1. **Instagram cost-sharing.** The per-niche HUB lets creators in one lane share ONE Apify scrape.
   Delete niche entirely -> each IG watchlist scrapes separately -> more Apify $. So: keep the
   niche-hub UNDER THE HOOD for IG, while the UX is just "add accounts." (Free platforms don't need
   the hub — each creator's YT/Twitch watchlist is cheap to scan on its own.)
2. **Discovery seeding.** "Find creators" grows a watchlist from the seed graph; a niche label helps
   scope it. Optional.

## 6. Module plan

- **`free_radar.py`** — REWRITE. Drop `_topics`/niche-search. New: `scan(creator_id, accounts)` where
  accounts = the creator's YouTube (and/or Twitch) watch handles. For YouTube: fetch-uploads path
  (§2). For Twitch: VOD path (§2). Then the shared pipeline (overperformance -> cluster -> gate ->
  rank -> store). Keep `_cluster_ai`/`_cluster_fallback`, `_SUBJECT_SCHEMA`, `latest()`,
  `db.latest_trends_of_type` — those survive the rewrite.
- **`sources/youtube.py`** — replace search (`_live`) with a `channel_uploads(handles)` fetcher
  (resolve handles -> uploads -> recent videos + stats). Keep the return dict shape.
- **`sources/twitch_vods.py`** (or extend `streams.py`) — new: `vods(logins)` -> per-streamer recent
  VODs with view_count + title; `live(logins)` -> current game/viewers overlay.
- **DB** — watch handles per platform stored as seeds (kind='account', platform in
  youtube/twitch/instagram/tiktok), reusing the existing seeds table. No schema change for trends.
- **Feed** — the "Make a video about ___" (YouTube) and "Go live with ___" (Twitch) panels already
  scaffolded; point them at the new per-account trends.

## 7. Cost / quota

- **YouTube:** ~50–60 units to watch ~50 channels; 10k/day quota => plenty, even at 2×/day for many
  creators. Far cheaper than v1 search.
- **Twitch:** free, rate-limited per minute. Generous.
- **AI:** one Claude call per creator per platform per scan to cluster titles. Cheap (Haiku/Sonnet).

## 8. What changes vs what's shipped (2026-07-07)

| Piece | State |
|---|---|
| Platform picker (built) | ✅ keep; extend each platform to collect watch handles |
| `free_radar.py` niche-search (built, dormant) | 🔁 REWRITE to channel-watchlist |
| `youtube.py` search + subs | 🔁 replace search with channel-uploads fetch |
| Twitch VOD path | 🔨 new (`twitch_vods`) |
| Twitch live game-board (`stream_radar`, old) | ⚠️ superseded by VOD model; may keep live-overlay only |
| Overperformance scoring, clustering, storage, feed panels | ✅ reuse (survive the rewrite) |

## 9. Open decisions

1. **Where watch handles come from:** creator types them per platform (simple v1) vs auto-derive
   (e.g. map an IG creator to their YouTube/Twitch) vs discovery-grown. Leaning: creator adds them;
   discovery later.
2. **Twitch live overlay:** ship VODs first, add the live game/viewers overlay as a follow-up?
3. **IG niche-hub:** keep as-is under the hood (recommended) vs go fully per-creator (simpler, costs
   more Apify).

## 10. Build phases

1. **YouTube channel-watchlist** — rewrite `free_radar` + `youtube.py` to the uploads model; collect
   YT handles in the picker; per-channel overperformance -> subjects -> panel. (The clear win.)
2. **Twitch VODs** — `twitch_vods.vods()`, per-streamer VOD overperformance -> subjects -> "go live
   with" panel; add live overlay.
3. **Polish** — discovery to grow watchlists per platform; tuning (FLOOR, MULT, MIN_ACCOUNTS).
