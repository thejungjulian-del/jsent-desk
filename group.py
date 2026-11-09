"""Group intelligence — each creator's own progress (NOT a competition).

  roster()       -> every creator's own stats, in roster order (never ranked vs each other)
  collabs()      -> trends hot for 2+ creators at once (the group multiplier)
  digest()       -> "Today at the Label" briefing
  standing(cid)  -> one creator's own comeback status, for their phone view
"""

import charts
import db

COMEBACK_LABEL = {
    "rising": "on a hot streak",
    "new": "just getting started",
    "steady": "holding steady",
    "cooling": "cooling off — time to post",
}


def _creator_snapshot(c):
    """Everything we know about one creator right now."""
    ch = charts.compute(c["id"])
    _, trends = db.latest_radar(c["id"])
    buys = [t for t in trends if t["status"] == "buy"]
    has_self = ch.get("has_self") and ch.get("posts")
    recent = ch.get("recent_index", 0) if has_self else 0
    comeback = ch.get("comeback") if has_self else None
    return {
        "id": c["id"], "name": c["name"], "niche": c["niche"],
        "comeback": comeback, "recent_index": recent,
        "buys": len(buys), "best": ch.get("best_views", 0) if has_self else 0,
        "trends": trends,
    }


def _all_snapshots(group_id=None):
    return [_creator_snapshot(c) for c in db.list_creators(group_id)]


def roster(snaps=None):
    """Each creator in roster order with how many trends are ready to film — never ranked
    against each other."""
    snaps = snaps or _all_snapshots()
    return [{"id": s["id"], "name": s["name"], "niche": s["niche"], "buys": s["buys"]}
            for s in snaps]


def collabs(snaps=None):
    """Trends that are hot (buy/watch) for 2+ creators → collab opportunities."""
    snaps = snaps or _all_snapshots()
    by_unit = {}  # unit_key -> {label, creators:[{name,status}]}
    for s in snaps:
        for t in s["trends"]:
            if t["status"] in ("buy", "watch"):
                u = by_unit.setdefault(t["unit_key"], {"label": t["unit_label"], "creators": []})
                u["creators"].append({"name": s["name"], "status": t["status"]})
    ops = []
    for key, u in by_unit.items():
        names = list({c["name"]: c for c in u["creators"]}.values())
        if len(names) >= 2:
            buy_n = sum(1 for c in names if c["status"] == "buy")
            ops.append({
                "label": u["label"],
                "creators": [c["name"] for c in names],
                "heat": buy_n,  # how many have it as a full BUY
            })
    ops.sort(key=lambda o: (o["heat"], len(o["creators"])), reverse=True)
    return ops


def digest(snaps=None):
    """The daily briefing — what moved across the whole label (no winners/losers)."""
    snaps = snaps or _all_snapshots()
    ops = collabs(snaps)

    total_buys = sum(s["buys"] for s in snaps)
    new_trends = sum(
        1 for s in snaps for t in s["trends"] if t["momentum"] == "new"
    )
    rising = [s["name"] for s in snaps if s["comeback"] == "rising"]
    cooling = [s["name"] for s in snaps if s["comeback"] == "cooling"]

    return {
        "creators": len(snaps),
        "hot_trends": total_buys,
        "new_trends": new_trends,
        "rising": rising,             # creators improving — celebrate them
        "cooling": cooling,           # these need a post — your nudge list
        "top_collab": ops[0] if ops else None,
        "collab_count": len(ops),
    }


def standing(cid):
    """One creator's OWN comeback status, in friendly terms (for their phone).
    Not a rank — just how they personally are trending."""
    creator = db.get_creator(cid)
    if not creator:
        return None
    snap = _creator_snapshot(creator)
    return {
        "comeback": snap["comeback"],
        "comeback_label": COMEBACK_LABEL.get(snap["comeback"], "let's get posting"),
    }
