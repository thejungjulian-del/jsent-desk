"""The Charts — track each creator's OWN posts like an idol's comeback stats.

A creator's 'self' seed accounts are their own handles. This reads how their own
posts are performing against their own baseline, and whether their latest run
('comeback') is trending up or down.
"""

import statistics
import time

import db
import scoring


def compute(creator_id):
    self_handles = {
        s["value"].lower() for s in db.list_seeds(creator_id) if s["kind"] == "self"
    }
    if not self_handles:
        return {"has_self": False}

    videos = [v for v in db.latest_video_stats(creator_id)
              if (v.get("author") or "").lower() in self_handles]
    if not videos:
        return {"has_self": True, "posts": 0}

    views = [v.get("views", 0) for v in videos]
    baseline = statistics.median(views)

    now = time.time()
    recent, prior = [], []
    for v in videos:
        op = (v.get("views", 0) / baseline) if baseline else 0
        v["_op"] = op
        age_h = (now - (v.get("first_seen") or now)) / 3600
        (recent if age_h <= 48 else prior).append(op)

    recent_avg = sum(recent) / len(recent) if recent else 0
    prior_avg = sum(prior) / len(prior) if prior else 0
    if not prior:
        comeback = "new"
    elif recent_avg > prior_avg * 1.1:
        comeback = "rising"
    elif recent_avg < prior_avg * 0.9:
        comeback = "cooling"
    else:
        comeback = "steady"

    top = sorted(videos, key=lambda v: v.get("views", 0), reverse=True)
    ranked = sorted(videos, key=lambda v: v["_op"], reverse=True)[:8]

    return {
        "has_self": True,
        "posts": len(videos),
        "baseline_views": int(baseline),
        "best_views": int(top[0].get("views", 0)) if top else 0,
        "comeback": comeback,
        "recent_index": round(recent_avg, 2),   # avg overperformance of last 48h
        "ranked": [
            {
                "author": v.get("author"),
                "views": int(v.get("views", 0)),
                "op": round(v["_op"], 2),
                "caption": (v.get("caption") or "")[:60],
                "url": v.get("url"),
            }
            for v in ranked
        ],
    }
