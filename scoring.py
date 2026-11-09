"""Trend math for JS Entertainment.

Ported from videotrends (engagement weighting, freshness, velocity) and
extended with the core radar idea: detect OVERPERFORMANCE, not raw popularity.

A trend = a trend-unit (audio or hashtag) that shows up in videos
overperforming their own account's baseline, across multiple accounts, and
rising scan-over-scan.
"""

import math
import os
import statistics
import time

# Engagement weights: likes/comments/shares count for much more than a view.
ENGAGEMENT_WEIGHTS = {"views": 1.0, "likes": 5.0, "comments": 10.0, "shares": 15.0}

FRESHNESS_HALFLIFE_HOURS = 72.0   # a 3-day-old video is worth ~half a brand-new one

# Only consider posts from the last RECENCY_DAYS — trends must be CURRENT, not evergreen
# mega-hits from months ago. Static photo POSTS age slower than video, so they get a
# longer window (RECENCY_DAYS_POST).
RECENCY_DAYS = int(os.environ.get("RECENCY_DAYS", "21"))
RECENCY_DAYS_POST = int(os.environ.get("RECENCY_DAYS_POST", "30"))


def _is_post(v):
    return v.get("media_type") in ("post", "carousel") or v.get("platform") == "instagram_photo"


def recent(videos, now=None):
    """Drop posts older than the window for their type (keeps undated posts — can't judge)."""
    now = now or time.time()

    def keep(v):
        days = RECENCY_DAYS_POST if _is_post(v) else RECENCY_DAYS
        return not v.get("post_time") or v["post_time"] >= now - days * 86400
    return [v for v in videos if keep(v)]


# Only consider genuinely POPULAR posts. Videos are measured by VIEWS (POP_FLOOR); static
# photo posts have no public views and far fewer likes than reels get views, so they get a
# lower, likes-based bar (POP_FLOOR_POST) — else photos never qualify against video.
POP_FLOOR = int(os.environ.get("POP_FLOOR", "100000"))
POP_FLOOR_POST = int(os.environ.get("POP_FLOOR_POST", "10000"))


def _reach(v):
    return max(int(v.get("views", 0) or 0), int(v.get("likes", 0) or 0))


def popular(videos):
    """Keep posts at/above their type's floor (videos: POP_FLOOR by views; static posts:
    POP_FLOOR_POST by likes). If nothing clears either bar, fall back to the top few by
    virality so the scan isn't left empty."""
    big = [v for v in videos if _reach(v) >= (POP_FLOOR_POST if _is_post(v) else POP_FLOOR)]
    if big:
        return big
    return sorted(videos, key=virality, reverse=True)[:12]

# Radar tuning
OVERPERF_MULT = 3.0       # a "hit" beats its account baseline by >= this multiple
MIN_ACCOUNTS = 2          # a real trend appears across at least this many accounts
SATURATION_ACCOUNTS = 8   # once this many accounts use it, it's likely peaked


def total_engagement(stats):
    return sum(stats.get(k, 0) * w for k, w in ENGAGEMENT_WEIGHTS.items())


def age_hours(post_time, now=None):
    now = now or time.time()
    if not post_time:
        return 1.0
    return max((now - post_time) / 3600.0, 0.5)


def freshness(post_time, now=None):
    return math.exp(-age_hours(post_time, now) / FRESHNESS_HALFLIFE_HOURS)


# ---------- VIRALITY (pure reach + speed, no per-account baseline) ----------

def virality(video, now=None):
    """How viral a post is on its own terms: absolute reach (engagement-weighted)
    boosted by recency, so big AND fast ranks highest. No account baseline — a huge
    post counts as huge no matter whose account it's on."""
    reach = total_engagement(video)
    return reach * (0.4 + 0.6 * freshness(video.get("post_time"), now))


def is_photo(v):
    return v.get("platform") == "instagram_photo" or v.get("media_type") in ("post", "carousel")


def viral_posts(videos, now=None):
    """The most viral posts in the pool (biggest, fastest-climbing). Ranked PER MEDIUM
    (video vs photo) so photos — which engage lower than video in raw numbers — keep
    their own slots and aren't starved out of the trend pool."""
    top = int(os.environ.get("VIRAL_TOP", "40"))

    def topn(group):
        return [v for v in sorted(group, key=lambda v: virality(v, now), reverse=True)[:top]
                if virality(v, now) > 0]

    return topn([v for v in videos if not is_photo(v)]) + topn([v for v in videos if is_photo(v)])


def account_baselines(videos):
    """median views per author -> used to judge what 'normal' looks like for them.
    Falls back to a niche-wide median for authors we've only seen once."""
    by_author = {}
    for v in videos:
        by_author.setdefault(v.get("author") or "?", []).append(v.get("views", 0))
    niche_median = statistics.median([v.get("views", 0) for v in videos]) if videos else 0
    baselines = {}
    for author, views in by_author.items():
        # need >=2 posts to trust an author's own baseline, else use the niche
        baselines[author] = statistics.median(views) if len(views) >= 2 else niche_median
    return baselines, niche_median


def overperformance(video, baselines, niche_median):
    """How many times over its expected baseline this video performed.
    1.0 = on baseline, 3.0 = triple. Returns 0 if we can't judge."""
    base = baselines.get(video.get("author") or "?", niche_median) or niche_median
    if not base:
        return 0.0
    return video.get("views", 0) / base


def is_hit(video, baselines, niche_median):
    return overperformance(video, baselines, niche_median) >= OVERPERF_MULT


def momentum(recent_count, prior_count):
    if prior_count == 0 and recent_count > 0:
        return "new"
    if recent_count > prior_count:
        return "rising"
    if recent_count < prior_count:
        return "fading"
    return "steady"


def status(account_count, mom):
    """The traffic light. BUY = rising and not yet everywhere."""
    if account_count >= SATURATION_ACCOUNTS:
        return "saturated"
    if mom in ("new", "rising"):
        return "buy"
    return "watch"


def trend_score(hit_count, account_count, viral_sum, avg_freshness):
    """Rank trends by VIRALITY: total viral weight of the posts in the trend leads;
    a mild bonus for spreading across multiple accounts; a recency boost. The biggest,
    fastest-growing things rise to the top."""
    return viral_sum * (1.0 + 0.15 * account_count) * (0.5 + avg_freshness)
