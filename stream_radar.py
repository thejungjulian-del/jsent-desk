"""The 'what to go live with' radar — per creator, scoped to their niche.

Same overperformance philosophy as the video radar, but the unit is a
game/category relevant to the creator's niche, and the signal is viewer demand:
  - momentum: is the category's viewership rising vs the last scan?
  - opportunity: viewers per live channel (high = lots of eyeballs, few streamers)
  -> GO LIVE = rising + above-average opportunity. CROWDED = thin opportunity.
"""

import statistics
import time

import db
from sources import streams as streams_src


def momentum(now_v, prior_v):
    if prior_v == 0 and now_v > 0:
        return "new"
    if now_v > prior_v * 1.1:
        return "rising"
    if now_v < prior_v * 0.9:
        return "fading"
    return "steady"


def _status(mom, opp, median_opp):
    if mom in ("new", "rising") and opp >= median_opp:
        return "go live"
    if opp < median_opp * 0.5:
        return "crowded"
    return "watch"


def scan(creator):
    cid = creator["id"]
    scan_time = time.time()
    cats = streams_src.fetch(creator.get("niche"), db.stream_scan_count(cid))
    prior = db.prior_stream_viewers(cid, scan_time)
    opps = [c["viewers"] / max(c["streams_count"], 1) for c in cats]
    median_opp = statistics.median(opps) if opps else 0

    results = []
    for c in cats:
        opp = c["viewers"] / max(c["streams_count"], 1)
        mom = momentum(c["viewers"], prior.get(c["category"], 0))
        st = _status(mom, opp, median_opp)
        row = {
            "creator_id": cid, "scan_time": scan_time, "source": c["source"],
            "category": c["category"], "viewers": c["viewers"],
            "streams_count": c["streams_count"], "opportunity": round(opp, 1),
            "momentum": mom, "status": st,
        }
        db.insert_stream_snapshot(row)
        results.append(row)
    results.sort(key=lambda r: (r["status"] == "go live", r["opportunity"]), reverse=True)
    return results


def latest(creator_id):
    _, rows = db.latest_streams(creator_id)
    rows.sort(key=lambda r: (r["status"] == "go live", r["opportunity"]), reverse=True)
    return rows