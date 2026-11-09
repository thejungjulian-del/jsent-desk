"""JS Entertainment — the intelligence desk.

A Flask dashboard. Add creators (each with a niche + seed accounts), then run
the radar to see which audios/formats are overperforming in their lane right now.
"""

import getpass
import os
import shutil
import subprocess
import sys
import threading
import time

from urllib.parse import urlparse

import requests
from flask import Flask, Response, jsonify, redirect, render_template, request, session

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except Exception:
    pass

import ai_trends
import briefs
import db
import deals
import discovery
import group
import popsounds
import radar
import free_radar
import replies
import scoring
import stream_radar
from sources import apify, inbox

def env(name, default=None):
    """Read JSENT_<name>, falling back to the old TRENDY_<name>.

    The fallback exists because the password gate disables itself when its variable
    is unset — so a half-migrated env file (code renamed, server env not yet) would
    silently leave the admin side wide open rather than fail loudly. Drop the
    fallback once every .env in play uses JSENT_*.
    """
    return os.environ.get(f"JSENT_{name}") or os.environ.get(f"TRENDY_{name}") or default


app = Flask(__name__)
app.secret_key = env("SECRET", "jsent-dev-secret-change-me")
db.init_db()

# Password gate: enabled only when JSENT_PASSWORD is set (so local dev stays open).
# Protects the ADMIN side; creator links (/me, their feed/brief/plan) stay public.
ADMIN_PASSWORD = env("PASSWORD")
_PUBLIC_PREFIXES = ("/me/", "/static/", "/api/feed/", "/api/brief/", "/api/plan/", "/api/img", "/api/vid",
                    "/api/saved/", "/api/deals/reply/", "/join/", "/api/watchlist/")
_PUBLIC_PATHS = {"/login", "/logout", "/healthz"}


def _is_public(path):
    return path in _PUBLIC_PATHS or any(path.startswith(p) for p in _PUBLIC_PREFIXES)


def _parse_niches(niche):
    """A creator's niche field may list several niches (comma-separated) — each is its
    own lane. Returns the distinct niches, or [the whole string] if there's just one."""
    parts = [n.strip() for n in (niche or "").split(",") if n.strip()]
    seen, out = set(), []
    for p in parts:
        if p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return out or [(niche or "").strip()]


@app.before_request
def _gate():
    if not ADMIN_PASSWORD or _is_public(request.path) or session.get("auth"):
        return None
    if request.path.startswith("/api/"):
        return jsonify({"error": "unauthorized"}), 401
    return redirect("/login")


@app.route("/healthz")
def healthz():
    return "ok"


@app.route("/login", methods=["GET", "POST"])
def login():
    if not ADMIN_PASSWORD or session.get("auth"):
        return redirect("/")
    error = False
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["auth"] = True
            session.permanent = True
            return redirect("/")
        error = True
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/")
def index():
    return render_template("dashboard.html", live=apify.enabled(), ai=briefs._llm_enabled())


# ---------- groups ----------

@app.route("/api/groups", methods=["GET"])
def get_groups():
    return jsonify(db.list_groups())


@app.route("/api/groups", methods=["POST"])
def create_group():
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    return jsonify({"id": db.add_group(name), "name": name})


@app.route("/api/groups/<int:gid>", methods=["POST"])
def rename_group(gid):
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    if name:
        db.rename_group(gid, name)
    return jsonify({"ok": True})


@app.route("/api/groups/<int:gid>", methods=["DELETE"])
def remove_group(gid):
    db.delete_group(gid)
    return jsonify({"ok": True})


def _trend_examples(cid, unit_key, videos=None, baselines=None, niche_median=None, limit=2):
    """Most VIRAL example posts for a trend-unit (so a card shows the post to recreate).
    Shared by the creator feed + admin. (baselines/niche_median kept for call-site
    compatibility; ranking is now pure virality.)"""
    if videos is None:
        videos = db.latest_video_stats(cid)
    ms = []
    for v in videos:
        tp = (v.get("topic") or "").lower()
        is_photo = v.get("platform") == "instagram_photo"
        match = (unit_key == f"audio:{v.get('audio_id')}") or \
                (tp and not is_photo and unit_key == f"topic:{tp}") or \
                (tp and is_photo and unit_key == f"photo:{tp}")
        if match and v.get("url"):
            ms.append((scoring.virality(v), v))
    ms.sort(key=lambda x: x[0], reverse=True)
    return [{"url": v.get("url"), "author": v.get("author") or "",
             "cover": v.get("cover") or "", "video_url": v.get("video_url") or "",
             "slides": v.get("slides") or [],
             "views": int(v.get("views", 0)), "likes": int(v.get("likes", 0))}
            for _, v in ms[:limit]]


def _only_current_niches(trends, niche):
    """Hide trends tagged to a niche the creator no longer has (so a former lane like
    'gym' can't linger on the list). Untagged/legacy trends (no niche) are kept."""
    cur = {n.strip().lower() for n in _parse_niches(niche)}
    return [t for t in trends
            if not (t.get("niche") or "").strip() or (t["niche"]).strip().lower() in cur]


def _creator_trends(creator):
    """Trends for a creator — read from the shared niche HUB(s): one scan per niche+gender,
    shared across all creators in that lane. Falls back to the creator's OWN latest scan
    when no hub has run yet, so the legacy per-creator path keeps working unchanged.
    Returns (scan_time, trends) like db.latest_radar."""
    rows, ts = [], None
    for nz in _parse_niches(creator.get("niche")):
        hub_id = db.hub_for(nz, creator.get("gender"))
        if hub_id:
            h_ts, h_trends = db.latest_radar(hub_id)
            rows.extend(h_trends)
            if h_ts:
                ts = max(ts or 0, h_ts)
    if rows:
        return ts, rows
    return db.latest_radar(creator["id"])      # legacy per-creator fallback


def _creator_yt_trends(creator):
    """YouTube overperformance subjects for a creator, read from the niche hub(s) — the free
    'make a video about ___' lane (no Apify). Empty until a scan with YouTube enabled has run."""
    out = []
    for nz in _parse_niches(creator.get("niche")):
        hub_id = db.hub_for(nz, creator.get("gender"))
        if hub_id:
            out.extend(free_radar.latest(hub_id))
    return out


@app.route("/api/creators", methods=["GET"])
def get_creators():
    deals.maybe_scan()           # quietly pull any new brand emails in the background (free IMAP)
    gid = request.args.get("group_id", type=int)
    out = []
    for c in db.list_creators(gid):
        c["seeds"] = db.list_seeds(c["id"])
        ts, trends = _creator_trends(c)         # shared niche-hub trends (or legacy own)
        trends = _only_current_niches(trends, c["niche"])    # drop trends from a dropped lane
        vids = None
        for t in trends:         # ensure every trend has example videos (audio trends store none)
            if not t.get("examples"):
                if vids is None:
                    vids = db.latest_video_stats(c["id"])
                t["examples"] = _trend_examples(c["id"], t["unit_key"], vids)
        c["radar"] = trends
        c["last_scan"] = ts
        if "stream" in (c.get("formats") or []):
            c["streams"] = stream_radar.latest(c["id"])
        out.append(c)
    return jsonify(out)


@app.route("/api/creators", methods=["POST"])
def add_creator():
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    niche = (data.get("niche") or "").strip()
    example = (data.get("example") or "").strip().lstrip("@")
    group_id = data.get("group_id")
    # accounts they want to post like, PER PLATFORM: {instagram:[...], tiktok:[...], youtube:[...], twitch:[...]}
    ex_by_plat = {}
    for plat, handles in (data.get("examples_by_platform") or {}).items():
        if plat in ("instagram", "tiktok", "youtube", "twitch"):
            clean = [(h or "").strip().lstrip("@") for h in (handles or []) if (h or "").strip()]
            if clean:
                ex_by_plat[plat] = list(dict.fromkeys(clean))
    if not ex_by_plat:                      # backward-compat with the old single IG field
        legacy = [(h or "").strip().lstrip("@")
                  for h in ([example] + list(data.get("example_creators") or [])) if (h or "").strip()]
        if legacy:
            ex_by_plat["instagram"] = list(dict.fromkeys(legacy))
    formats = [f for f in (data.get("formats") or [])
               if f in ("tiktok", "reels", "post", "stream", "yt_long", "yt_short", "yt_live")]
    if not name:
        return jsonify({"error": "a creator name is required"}), 400
    if not niche and not ex_by_plat:
        return jsonify({"error": "add at least one account they post like"}), 400
    if not group_id:
        return jsonify({"error": "create or select a group first"}), 400
    if not formats:
        return jsonify({"error": "pick at least one content format"}), 400
    gender = (data.get("gender") or "").strip().lower()
    gender = gender if gender in ("man", "woman") else None
    try:
        age = int(data.get("age")) if str(data.get("age") or "").strip() else None
    except (TypeError, ValueError):
        age = None
    # niche is auto-detected from the Instagram example accounts (the profile scraper is IG-only);
    # a YouTube/Twitch-only creator falls back to 'general' — niche is optional in the watch model.
    if not niche:
        niche = discovery.infer_niche(ex_by_plat.get("instagram", []), gender) or "general"
    cid = db.add_creator(name, niche, group_id, data.get("password"), formats, gender, age)
    # auto-build the scene per niche (separate lanes), gender-tuned, scoped to formats
    for nz in _parse_niches(niche):
        for s in discovery.derive_seeds(nz, formats, gender):
            db.add_seed(cid, s["platform"], s["kind"], s["value"], niche=nz)
    # the creator's own accounts (optional), passed from the form
    for s in data.get("seeds", []):
        if s.get("value", "").strip():
            db.add_seed(cid, s["platform"], s["kind"], s["value"])
    # Seed the example accounts they gave DIRECTLY as watch-targets, PER PLATFORM — no discovery
    # burst on add (that would slam the rate limit). Auto-grow expands the lane later.
    for nz in _parse_niches(niche):
        seen = set()
        for plat, handles in ex_by_plat.items():
            for h in handles:
                key = (plat, h.lower())
                if key not in seen:
                    db.add_seed(cid, plat, "account", h, niche=nz)
                    seen.add(key)
    return jsonify({"id": cid})


@app.route("/api/creators/<int:cid>/seeds", methods=["POST"])
def add_seeds(cid):
    data = request.get_json(force=True)
    for s in data.get("seeds", []):
        if s.get("value", "").strip():
            db.add_seed(cid, s["platform"], s["kind"], s["value"])
    return jsonify({"ok": True})


# ---------- watch-list (the accounts scanned for a creator's niche) ----------

@app.route("/api/watchlist/<int:cid>", methods=["GET"])
def get_watchlist(cid):
    """The accounts being watched for this creator — both ones they added and ones the app
    discovered. Editable by the creator (their own view) or the admin."""
    guard = _creator_guard(cid)
    if guard:
        return guard
    creator = db.get_creator(cid)
    accounts = [{"id": s["id"], "value": s["value"], "platform": s["platform"], "niche": s.get("niche")}
                for s in db.list_seeds(cid) if s["kind"] == "account"]
    accounts.sort(key=lambda a: -a["id"])   # most-recently-added first
    # how many the whole niche lane is watching (shared hub) → toward the target
    total = 0
    for nz in _parse_niches(creator["niche"]):
        hub_id = db.hub_for(nz, creator.get("gender"))
        if hub_id:
            db.sync_hub_seeds(nz, creator.get("gender"))
            total += sum(1 for s in db.list_seeds(hub_id) if s["kind"] == "account")
    target = int(os.environ.get("WATCHLIST_TARGET", "50"))
    return jsonify({"accounts": accounts, "count": total or len(accounts), "target": target,
                    "niches": _parse_niches(creator["niche"])})


@app.route("/api/watchlist/<int:cid>", methods=["POST"])
def add_watchlist(cid):
    """Manually add an account to watch. Counts toward the 50, so the auto-grow fills fewer.
    Added directly (no IG call) — a creator's manual pick is trusted; a bad handle just
    returns nothing on the next scan."""
    guard = _creator_guard(cid)
    if guard:
        return guard
    creator = db.get_creator(cid)
    data = request.get_json(force=True)
    value = (data.get("value") or "").strip().strip("/").lstrip("@")
    if not value:
        return jsonify({"error": "enter a username"}), 400
    platform = data.get("platform") if data.get("platform") in ("instagram", "tiktok", "youtube", "twitch") else "instagram"
    niche = (data.get("niche") or _parse_niches(creator["niche"])[0] or "").strip()
    db.add_seed(cid, platform, "account", value, niche=niche or None)
    db.unblock_account(niche, creator.get("gender"), value)   # explicit manual pick overrides a prior block
    return jsonify({"ok": True})


@app.route("/api/watchlist/<int:cid>/remove", methods=["POST"])
def remove_watchlist(cid):
    guard = _creator_guard(cid)
    if guard:
        return guard
    data = request.get_json(force=True)
    sid = data.get("seed_id")
    if sid is not None:
        val = db.seed_value(int(sid))            # capture the handle before deleting
        db.remove_seed(int(sid))
        creator = db.get_creator(cid)            # blocklist it so discovery can't re-add it
        if val and creator:
            for nz in _parse_niches(creator.get("niche") or ""):
                db.block_account(nz, creator.get("gender"), val)
    return jsonify({"ok": True})


@app.route("/api/watchlist/<int:cid>/niches", methods=["POST"])
def add_creator_niche(cid):
    """Add another niche/genre lane to a creator (the 'add a creator in a different niche' flow).
    Appends to the comma-separated niche field — NON-destructive (unlike a niche EDIT, this never
    triggers reset_scanned_for_relane), then ensures the new lane's hub exists. Creator or admin."""
    guard = _creator_guard(cid)
    if guard:
        return guard
    creator = db.get_creator(cid)
    new = (request.get_json(force=True).get("niche") or "").strip()
    if not new:
        return jsonify({"error": "enter a niche"}), 400
    current = _parse_niches(creator.get("niche") or "")
    if new.lower() in [n.lower() for n in current]:
        return jsonify({"ok": True, "niches": current})     # already a lane
    merged = ", ".join(current + [new])
    db.update_creator(cid, creator["name"], merged, creator.get("formats"),
                      creator.get("gender"), creator.get("age"))
    db.sync_hub_seeds(new, creator.get("gender"))            # spin up the new lane's shared hub
    return jsonify({"ok": True, "niches": current + [new]})


@app.route("/api/watchlist/<int:cid>/formats", methods=["GET", "POST"])
def creator_formats(cid):
    """Get (GET) or update (POST) a creator's platforms/content formats from their own view."""
    guard = _creator_guard(cid)
    if guard:
        return guard
    creator = db.get_creator(cid)
    if request.method == "GET":
        return jsonify({"formats": creator.get("formats") or []})
    formats = [f for f in (request.get_json(force=True).get("formats") or [])
               if f in ("tiktok", "reels", "post", "stream", "yt_long", "yt_short", "yt_live")]
    if not formats:
        return jsonify({"error": "pick at least one content format"}), 400
    db.update_creator(cid, creator["name"], creator["niche"], formats,
                      creator.get("gender"), creator.get("age"))
    return jsonify({"ok": True, "formats": formats})


@app.route("/api/creators/<int:cid>", methods=["PATCH"])
def edit_creator(cid):
    creator = db.get_creator(cid)
    if not creator:
        return jsonify({"error": "not found"}), 404
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    niche = creator["niche"]                      # niche is auto-detected at creation, not edited
    formats = [f for f in (data.get("formats") or [])
               if f in ("tiktok", "reels", "post", "stream", "yt_long", "yt_short", "yt_live")]
    if not name:
        return jsonify({"error": "a creator name is required"}), 400
    if not formats:
        return jsonify({"error": "pick at least one content format"}), 400

    gender = (data.get("gender") or "").strip().lower()
    gender = gender if gender in ("man", "woman") else None
    try:
        age = int(data.get("age")) if str(data.get("age") or "").strip() else None
    except (TypeError, ValueError):
        age = None

    formats_changed = set(formats) != set(creator.get("formats") or [])
    gender_changed = gender != creator.get("gender")

    db.update_creator(cid, name, niche, formats, gender, age)
    db.set_creator_password(cid, (data.get("password") or "").strip())
    db.replace_self_seeds(cid, data.get("seeds", []))

    # if formats or gender moved, rebuild the niche-derived hashtags (niche itself never
    # changes here, so a creator's trends are never wiped by an edit)
    if formats_changed or gender_changed:
        db.clear_derived_seeds(cid)
        for nz in _parse_niches(niche):
            for s in discovery.derive_seeds(nz, formats, gender):
                db.add_seed(cid, s["platform"], s["kind"], s["value"], niche=nz)
    return jsonify({"ok": True, "rescan": formats_changed or gender_changed})


@app.route("/api/creators/<int:cid>", methods=["DELETE"])
def remove_creator(cid):
    db.delete_creator(cid)
    return jsonify({"ok": True})


@app.route("/api/creators/<int:cid>/password", methods=["POST"])
def creator_password(cid):
    data = request.get_json(force=True)
    db.set_creator_password(cid, (data.get("password") or "").strip())
    return jsonify({"ok": True})


def _bg_job(cid, script, fallback_fn, tag):
    """Run a job (scan or grow) DETACHED from gunicorn (a transient systemd unit) so a
    deploy/restart can't kill it mid-run — that was wiping in-progress scans before any
    trends saved. Falls back to an in-process thread if systemd-run isn't usable (local dev)."""
    here = os.path.dirname(os.path.abspath(__file__))
    script_path = os.path.join(here, script)
    if shutil.which("systemd-run"):
        unit = f"jsent{tag}-{cid}-{int(time.time())}"
        cmd = ["sudo", "-n", "systemd-run", "--quiet", "--collect", "--unit", unit,
               "-p", f"User={getpass.getuser()}", "-p", f"Group={getpass.getuser()}",
               "-p", f"EnvironmentFile={os.path.join(here, 'jsent.env')}",
               "-p", f"WorkingDirectory={here}",
               sys.executable, script_path, str(cid)]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=15)
            if r.returncode == 0:
                return
            print(f"[{tag}] systemd-run rc={r.returncode}: {r.stderr.decode(errors='replace')[:160]}")
        except Exception as e:
            print(f"[{tag}] systemd-run error: {type(e).__name__}: {str(e)[:120]}")
    # fallback: in-process (dies on restart, but better than nothing)
    def _run():
        try:
            fallback_fn(cid)
        except Exception as e:
            print(f"[{tag}] {cid} failed: {type(e).__name__}: {str(e)[:200]}")
    threading.Thread(target=_run, daemon=True).start()


def _bg_scan(cid):
    _bg_job(cid, "scan_one.py", radar.scan_for_creator, "scan")


def _bg_grow(cid):
    _bg_job(cid, "grow_one.py", radar.grow_for_creator, "grow")


@app.route("/api/scan/<int:cid>", methods=["POST"])
def scan(cid):
    _bg_scan(cid)
    return jsonify({"started": True})


@app.route("/api/grow/<int:cid>", methods=["POST"])
def grow(cid):
    _bg_grow(cid)
    return jsonify({"started": True})


@app.route("/api/grow_status/<int:cid>", methods=["GET"])
def grow_status(cid):
    """Timestamp of the last completed 'Find creators' run — the button polls this to know
    when the (1-2 min) background grow has finished and the watch-list has new accounts."""
    return jsonify({"grow_done": db.get_setting(f"grow_done_{cid}"),
                    "grow_added": db.get_setting(f"grow_added_{cid}")})


@app.route("/api/reset/<int:cid>", methods=["POST"])
def reset_creator(cid):
    """Clean slate: wipe the niche hub's old trends + scraped posts, then re-scan fresh.
    Use after a niche change to clear a former lane (e.g. leftover 'gym' trends)."""
    def _run():
        try:
            radar.reset_for_creator(cid)
        except Exception as e:
            print(f"[reset] {cid} failed: {type(e).__name__}: {str(e)[:200]}")
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"started": True})


@app.route("/api/last_scan/<int:cid>", methods=["GET"])
def last_scan(cid):
    # prefer the scan-completion marker (advances even when a scan finds 0 trends, so the
    # 'Scan now' button stops waiting); fall back to the latest trend timestamp.
    done = db.get_setting(f"scan_done_{cid}")
    if done:
        return jsonify({"last_scan": float(done)})
    ts, _ = db.latest_radar(cid)
    return jsonify({"last_scan": ts})


# ---------- brand deals ----------

@app.route("/api/deals/<int:cid>", methods=["GET"])
def get_deals(cid):
    creator = db.get_creator(cid)
    if not creator:
        return jsonify({"error": "not found"}), 404
    return jsonify({
        "address": inbox.address_for(db.deal_slug(cid)),
        "live": inbox.enabled(),
        "deals": db.list_deals(cid),
    })


@app.route("/api/deals/scan/<int:cid>", methods=["POST"])
def scan_deals(cid):
    creator = db.get_creator(cid)
    if not creator:
        return jsonify({"error": "not found"}), 404
    return jsonify(deals.run_scan(creator))


@app.route("/api/deals/item/<int:deal_id>", methods=["POST"])
def set_deal_status(deal_id):
    data = request.get_json(force=True)
    db.set_deal_status(deal_id, data.get("status", "new"))
    return jsonify({"ok": True})


@app.route("/api/deals/activity/<int:gid>", methods=["GET"])
def deals_activity(gid):
    return jsonify(db.group_deals(gid))


def _reply_deal(deal_id):
    """Fetch a deal + its creator, enforcing the creator's auth gate."""
    deal = db.get_deal(deal_id)
    if not deal:
        return None, (jsonify({"error": "not found"}), 404)
    blocked = _creator_guard(deal["creator_id"])
    if blocked:
        return None, blocked
    return deal, None


@app.route("/api/deals/reply/<int:deal_id>/draft", methods=["POST"])
def deal_reply_draft(deal_id):
    deal, err = _reply_deal(deal_id)
    if err:
        return err
    data = request.get_json(force=True)
    kind = data.get("kind")
    if kind not in ("yes", "no", "negotiate"):
        return jsonify({"error": "bad reply kind"}), 400
    creator = db.get_creator(deal["creator_id"])
    return jsonify(replies.build(creator, deal, kind, data.get("fields")))


@app.route("/api/deals/reply/<int:deal_id>/send", methods=["POST"])
def deal_reply_send(deal_id):
    deal, err = _reply_deal(deal_id)
    if err:
        return err
    data = request.get_json(force=True)
    kind = data.get("kind")
    if kind not in ("yes", "no", "negotiate"):
        return jsonify({"error": "bad reply kind"}), 400
    creator = db.get_creator(deal["creator_id"])
    by = "manager" if session.get("auth") else "creator"
    simulated = str(deal.get("message_id", "")).startswith("demo-")
    try:
        if simulated:   # don't actually email a fake demo brand
            body = replies.build(creator, deal, kind, data.get("fields"))["body"]
        else:
            body = replies.send(creator, deal, kind, data.get("fields"))
    except Exception as e:
        return jsonify({"error": f"couldn't send: {e}"}), 502
    db.set_deal_reply(deal_id, kind, body, by)
    db.add_deal_message(deal_id, "out", body)             # show it in the conversation thread
    return jsonify({"ok": True, "simulated": simulated})


@app.route("/api/deals/reply/<int:deal_id>/message", methods=["POST"])
def deal_reply_message(deal_id):
    """Send a free-form follow-up message in a deal's conversation thread."""
    deal, err = _reply_deal(deal_id)
    if err:
        return err
    body = (request.get_json(force=True).get("body") or "").strip()
    if not body:
        return jsonify({"error": "empty message"}), 400
    creator = db.get_creator(deal["creator_id"])
    simulated = str(deal.get("message_id", "")).startswith("demo-")
    if not simulated:
        try:
            replies.send_message(creator, deal, body)
        except Exception as e:
            return jsonify({"error": f"couldn't send: {e}"}), 502
    db.add_deal_message(deal_id, "out", body)
    return jsonify({"ok": True, "simulated": simulated})


@app.route("/api/brief/<int:cid>", methods=["POST"])
def brief(cid):
    blocked = _creator_guard(cid)
    if blocked:
        return blocked
    data = request.get_json(force=True)
    unit_key = data.get("unit_key")
    if not unit_key:
        return jsonify({"error": "unit_key required"}), 400
    return jsonify(briefs.generate(cid, unit_key, force=data.get("force", False)))


# ---------- creator-facing (the simple phone view) ----------

def _friendly_title(t):
    label = t["unit_label"]
    if t["unit_type"] == "audio":
        pf = {"tiktok": "TikTok", "instagram": "Instagram"}.get(t.get("platform"), "")
        return f'the "{label}" {pf + " " if pf else ""}sound'.replace("  ", " ")
    if t["unit_type"] == "topic":
        return f'a video about "{label}"'
    if t["unit_type"] == "photo":
        return f'a "{label}" picture'
    if t["unit_type"] == "hashtag":
        return f"the {label} hashtag"
    return label


def _blurb(t):
    n = t["account_count"]
    if n >= 5:
        return f"{n} creators in your lane are blowing up with it — and it's spreading fast"
    if n > 1:
        return f"{n} creators in your lane are blowing up with it"
    return "it's just starting to pop in your lane"


def _offer_label(d):
    if (d.get("offer_amount") or "").strip():
        return d["offer_amount"]
    return {"gifting": "Free product", "affiliate": "Commission / affiliate"}.get(
        d.get("offer_type"), "Amount not stated")


def _fit_label(score):
    if score >= 70:
        return "Great match for you"
    if score >= 45:
        return "Decent match"
    return "A bit outside your usual lane"


def _creator_deals(cid):
    """Deals to show the creator: everything except the ones the manager passed on."""
    out = []
    for d in db.list_deals(cid):
        if d["status"] == "passed":
            continue
        replied = (d.get("reply_status") or "none") != "none"
        out.append({
            "id": d["id"],
            "brand": d["brand"],
            "offer": _offer_label(d),
            "fit_label": _fit_label(d["fit_score"]),
            "deliverables": d["deliverables"],
            "flags": d["flags"],
            "reply_status": d.get("reply_status") or "none",
            "interested": d["status"] == "interested" and not replied,
            "messages": db.get_deal_thread(d["id"]),          # the full brand ⇄ creator conversation
        })
    return out


def _creator_authed(cid):
    return bool(session.get("auth") or session.get(f"cr_{cid}"))


def _creator_guard(cid):
    """For creator-facing APIs: 404 if missing, 401 if locked and not authed.
    The admin (manager session) may act on any creator; a creator only on their own."""
    creator = db.get_creator(cid)
    if not creator:
        return jsonify({"error": "not found"}), 404
    if session.get("auth"):                      # admin passes for any creator
        return None
    if creator.get("password") and not _creator_authed(cid):
        return jsonify({"error": "locked"}), 401
    return None


@app.route("/me/<int:cid>")
def creator_view(cid):
    creator = db.get_creator(cid)
    if not creator:
        return "Creator not found", 404
    if creator.get("password") and not _creator_authed(cid):
        return render_template("creator_login.html", name=creator["name"], cid=cid, error=False)
    g = db.get_group(creator.get("group_id")) if creator.get("group_id") else None
    return render_template("creator.html", creator=creator,
                           group_name=(g["name"] if g else "your group"))


@app.route("/me/<int:cid>/login", methods=["POST"])
def creator_login(cid):
    creator = db.get_creator(cid)
    if not creator:
        return "Creator not found", 404
    if request.form.get("password") == (creator.get("password") or ""):
        session[f"cr_{cid}"] = True
        session.permanent = True
        return redirect(f"/me/{cid}")
    return render_template("creator_login.html", name=creator["name"], cid=cid, error=True)


# ---------- creator self-signup via an invite link (no manager needed) ----------

@app.route("/join/<token>")
def join_view(token):
    g = db.group_by_invite(token)
    if not g:
        return render_template("join.html", token=None, group_name=None), 404
    return render_template("join.html", token=token, group_name=g["name"])


@app.route("/join/<token>", methods=["POST"])
def join_submit(token):
    g = db.group_by_invite(token)
    if not g:
        return jsonify({"error": "this invite link is no longer valid — ask your manager for a new one"}), 404
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "please enter your name"}), 400
    # accounts you post like, PER PLATFORM (mirrors add_creator); legacy single 'accounts' still works
    ex_by_plat = {}
    for plat, handles in (data.get("examples_by_platform") or {}).items():
        if plat in ("instagram", "tiktok", "youtube", "twitch"):
            clean = [(h or "").strip().lstrip("@") for h in (handles or []) if (h or "").strip()]
            if clean:
                ex_by_plat[plat] = list(dict.fromkeys(clean))
    if not ex_by_plat and data.get("accounts"):
        legacy = [(h or "").strip().lstrip("@") for h in data.get("accounts") if (h or "").strip()]
        if legacy:
            ex_by_plat["instagram"] = list(dict.fromkeys(legacy))
    if not ex_by_plat:
        return jsonify({"error": "add at least one account you post like, so we can set up your trends"}), 400
    gender = (data.get("gender") or "").strip().lower()
    gender = gender if gender in ("man", "woman") else None
    password = (data.get("password") or "").strip() or None
    formats = [f for f in (data.get("formats") or [])
               if f in ("tiktok", "reels", "post", "stream", "yt_long", "yt_short", "yt_live")] or ["reels", "post"]
    niche = discovery.infer_niche(ex_by_plat.get("instagram", []), gender) or "general"   # from IG accounts
    cid = db.add_creator(name, niche, g["id"], password, formats, gender, None)
    for nz in _parse_niches(niche):                      # build the trend lane(s), gender-tuned
        for s in discovery.derive_seeds(nz, formats, gender):
            db.add_seed(cid, s["platform"], s["kind"], s["value"], niche=nz)
    for s in data.get("seeds", []):                      # the creator's OWN handle(s)
        if s.get("value", "").strip():
            db.add_seed(cid, s["platform"], s["kind"], s["value"])
    for nz in _parse_niches(niche):                      # seed the accounts they gave as watch targets, per platform
        seen = set()
        for plat, handles in ex_by_plat.items():
            for h in handles:
                key = (plat, h.lower())
                if key not in seen:
                    db.add_seed(cid, plat, "account", h, niche=nz)
                    seen.add(key)
    db.deal_slug(cid)                                    # give them a brand-deal mailbox
    session[f"cr_{cid}"] = True                          # log them straight into their own view
    session.permanent = True
    return jsonify({"url": f"/me/{cid}"})


@app.route("/api/invite", methods=["GET", "POST"])
def invite_link():
    """Manager: get (GET) or regenerate (POST) the shareable creator-signup link for a group."""
    gid = request.args.get("group_id") or (request.get_json(silent=True) or {}).get("group_id")
    if not gid:
        return jsonify({"error": "group_id required"}), 400
    tok = db.group_invite_token(int(gid), regenerate=(request.method == "POST"))
    if not tok:
        return jsonify({"error": "group not found"}), 404
    return jsonify({"link": request.host_url.rstrip("/") + "/join/" + tok})


_IMG_HOSTS = ("cdninstagram.com", "fbcdn.net", "instagram.com", "tiktokcdn.com",
              "tiktokcdn-us.com", "ibyteimg.com", "tiktok.com", "ytimg.com", "ggpht.com")


@app.route("/api/img")
def img_proxy():
    """Fetch a post thumbnail server-side and serve it — Instagram/TikTok CDN images are
    hotlink/CORS-restricted, so a browser <img> pointing straight at them 403s. Whitelisted
    to IG/TikTok CDN hosts so it can't be used to fetch arbitrary URLs."""
    u = (request.args.get("u") or "").strip()
    try:
        host = (urlparse(u).hostname or "").lower()
    except Exception:
        host = ""
    if not u.startswith("https://") or not any(host == h or host.endswith("." + h) for h in _IMG_HOSTS):
        return "bad url", 400
    try:
        r = requests.get(u, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
    except Exception:
        return "", 502
    ct = r.headers.get("Content-Type", "image/jpeg")
    if not ct.startswith("image/"):
        return "", 502
    return Response(r.content, content_type=ct, headers={"Cache-Control": "public, max-age=86400"})


@app.route("/api/vid")
def vid_proxy():
    """Stream a post's raw video server-side so the lightbox can show JUST the video (no
    Instagram embed chrome). Same host-whitelist as the image proxy; passes Range through so
    the browser can seek."""
    u = (request.args.get("u") or "").strip()
    try:
        host = (urlparse(u).hostname or "").lower()
    except Exception:
        host = ""
    if not u.startswith("https://") or not any(host == h or host.endswith("." + h) for h in _IMG_HOSTS):
        return "bad url", 400
    fwd = {"User-Agent": "Mozilla/5.0"}
    if request.headers.get("Range"):
        fwd["Range"] = request.headers["Range"]
    try:
        r = requests.get(u, headers=fwd, stream=True, timeout=20)
    except Exception:
        return "", 502
    ct = r.headers.get("Content-Type", "video/mp4")
    if not (ct.startswith("video/") or ct == "application/octet-stream"):
        return "", 502

    def _gen():
        try:
            for chunk in r.iter_content(65536):
                if chunk:
                    yield chunk
        finally:
            r.close()
    resp = Response(_gen(), status=r.status_code, content_type=ct)
    for h in ("Content-Range", "Content-Length", "Accept-Ranges"):
        if h in r.headers:
            resp.headers[h] = r.headers[h]
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


@app.route("/me/<int:cid>/manifest.webmanifest")
def creator_manifest(cid):
    """Per-creator PWA manifest so a creator can 'Add to Home Screen' and it opens THEIR page
    full-screen, like a native app."""
    creator = db.get_creator(cid)
    name = (creator.get("name") if creator else None) or "JS Entertainment"
    return jsonify({
        "name": f"{name} · JS Entertainment", "short_name": name,
        "start_url": f"/me/{cid}", "scope": f"/me/{cid}",
        "display": "standalone", "orientation": "portrait",
        "background_color": "#f5f5f3", "theme_color": "#f5f5f3",
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    })


def _best_post_times(creator):
    """Best time to post for EACH day of the week, from when the top-performing posts in the
    creator's niche(s) go up (peak median-engagement hour per day). Times in US Pacific."""
    from statistics import median
    import datetime
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/Los_Angeles")
    except Exception:
        tz = datetime.timezone(datetime.timedelta(hours=-8))
    posts, seen = [], set()
    for nz in _parse_niches(creator["niche"]):
        hub_id = db.hub_for(nz, creator.get("gender"))
        if hub_id and hub_id not in seen:
            seen.add(hub_id)
            posts += db.latest_video_stats(hub_id)
    byslot, total = {}, 0
    for v in posts:
        pt = v.get("post_time")
        eng = int(v.get("likes") or 0) or int(v.get("views") or 0)
        if not pt or eng <= 0:
            continue
        dt = datetime.datetime.fromtimestamp(pt, tz)
        byslot.setdefault((dt.weekday(), dt.hour), []).append(eng)
        total += 1
    if total < 20:
        return None
    DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    def _win(h):
        h2 = (h + 2) % 24
        ap1, ap2 = ("AM" if h < 12 else "PM"), ("AM" if h2 < 12 else "PM")
        hh1, hh2 = (h % 12 or 12), (h2 % 12 or 12)
        return f"{hh1}–{hh2} {ap2}" if ap1 == ap2 else f"{hh1} {ap1}–{hh2} {ap2}"
    days = []
    for d in range(7):
        # only realistic posting hours (6 AM–11 PM) — avoids odd 2 AM peaks from a lone
        # viral post or a global creator posting in another timezone
        hours = {h: median(v) for (dd, h), v in byslot.items() if dd == d and 6 <= h <= 23 and len(v) >= 2}
        peak = max(hours, key=hours.get) if hours else None
        days.append({"day": DAYS[d], "short": DAYS[d][:3], "window": _win(peak) if peak is not None else None})
    return {"days": days, "tz": "PT", "sample": total}


@app.route("/api/feed/<int:cid>")
def creator_feed(cid):
    blocked = _creator_guard(cid)
    if blocked:
        return blocked
    deals.maybe_scan()           # surface any new brand emails without a manual refresh
    creator = db.get_creator(cid)
    formats = creator.get("formats") or []
    accounts = [{"id": s["id"], "platform": s["platform"], "value": s["value"]}
                for s in db.list_seeds(cid) if s["kind"] == "self"]

    # show only the content types this creator makes (tiktok / reels / post). Legacy
    # creators may still carry old format values — map them so nothing drops.
    legacy = {"short": {"tiktok", "reels"}, "photo": {"post"}}
    allowed_ch = set()
    for f in formats:
        allowed_ch |= legacy.get(f, {f})
    allowed_ch &= {"tiktok", "reels", "post"}
    if not allowed_ch:
        allowed_ch = {"tiktok", "reels", "post"}

    # example videos per trend (so each card can link "watch what's working")
    videos = db.latest_video_stats(cid)
    baselines, niche_median = scoring.account_baselines(videos)

    def _examples(unit_key):
        return _trend_examples(cid, unit_key, videos, baselines, niche_median)

    _, trends = _creator_trends(creator)    # shared niche-hub trends (ranked by score), or legacy own
    trends = _only_current_niches(trends, creator["niche"])   # drop trends from a dropped lane
    hot, warming, late = [], [], []         # warming/late no longer shown — only "Film now"
    per_ch = {}
    for t in trends:
        is_ai = t["unit_type"] == "concept"
        ch = t.get("channel") or ("post" if (t.get("medium") == "photo") else "reels")
        if ch not in allowed_ch:            # only the content types this creator makes
            continue
        if t["status"] != "buy":            # only the strongest "Film now" items
            continue
        if per_ch.get(ch, 0) >= ai_trends.STORE_PER_TYPE:   # return the full bench; UI shows 8 + "Show more"
            continue
        per_ch[ch] = per_ch.get(ch, 0) + 1
        hot.append({
            "unit_key": t["unit_key"],
            "title": _friendly_title(t),
            "blurb": (t.get("description") if is_ai else None) or _blurb(t),
            "examples": (t.get("examples") if is_ai else None) or _examples(t["unit_key"]),
            "niche": t.get("niche") or "",
            "medium": t.get("medium") or ("photo" if t["unit_type"] == "photo" else "video"),
            "sound": t.get("sound"),
            "riders": t.get("account_count"),
            "your_take": t.get("your_take"),
            "channel": ch,
            "platforms": t.get("platforms") or [],
        })

    # streamers also get "what to go live with" from their group's stream radar
    streams = []
    if "stream" in formats:
        streams = [{"category": r["category"], "source": r["source"], "viewers": r["viewers"]}
                   for r in stream_radar.latest(cid) if r["status"] == "go live"][:5]

    # YouTube lane — overperforming videos from the channels this creator watches (no Apify)
    youtube_trends = []
    if any(f in ("yt_long", "yt_short", "yt_live") for f in formats):
        for t in _creator_yt_trends(creator):
            ex = (t.get("examples") or [{}])[0]
            youtube_trends.append({
                "unit_key": t.get("unit_key"), "title": t.get("unit_label"),
                "cover": ex.get("cover") or "", "url": ex.get("url") or "",
                "author": ex.get("author") or "", "views": ex.get("views") or 0,
                "mult": round(t.get("overperf_sum") or 0, 1), "niche": t.get("niche") or "",
            })
        youtube_trends = youtube_trends[:24]

    return jsonify({
        "name": creator["name"], "niche": creator["niche"], "formats": formats,
        "scanned": bool(trends),
        "accounts": accounts, "needs_accounts": len(accounts) == 0,
        "hot": hot, "warming": warming, "late": late, "streams": streams,
        "youtube": youtube_trends,
        "niches": _parse_niches(creator["niche"]),
        "popular_sounds": [],
        "post_times": _best_post_times(creator),
        "deals": _creator_deals(cid),
        "plan": db.list_plan(cid),
    })


# ---------- Roster HQ + group intelligence ----------

@app.route("/api/hq")
def hq():
    gid = request.args.get("group_id", type=int)
    snaps = group._all_snapshots(gid)
    return jsonify({
        "digest": group.digest(snaps),
        "roster": group.roster(snaps),
        "collabs": group.collabs(snaps)[:5],
    })


# ---------- the plan ----------

@app.route("/api/plans/<int:gid>", methods=["GET"])
def group_plans(gid):
    return jsonify(db.group_plan(gid))


@app.route("/api/saved/<int:cid>", methods=["GET"])
def get_saved(cid):
    """A creator's saved-ideas board (Pinterest-style save-for-later)."""
    blocked = _creator_guard(cid)
    if blocked:
        return blocked
    return jsonify(db.list_saved(cid))


@app.route("/api/saved/<int:cid>", methods=["POST"])
def add_saved(cid):
    blocked = _creator_guard(cid)
    if blocked:
        return blocked
    data = request.get_json(force=True)
    if not (data.get("url") or "").strip():
        return jsonify({"error": "url required"}), 400
    db.add_saved(cid, data)
    return jsonify({"ok": True})


@app.route("/api/saved/<int:cid>/remove", methods=["POST"])
def remove_saved(cid):
    blocked = _creator_guard(cid)
    if blocked:
        return blocked
    url = (request.get_json(force=True).get("url") or "").strip()
    if url:
        db.remove_saved(cid, url)
    return jsonify({"ok": True})


@app.route("/api/plan/<int:cid>", methods=["GET"])
def get_plan(cid):
    blocked = _creator_guard(cid)
    if blocked:
        return blocked
    return jsonify(db.list_plan(cid))


@app.route("/api/plan/<int:cid>", methods=["POST"])
def add_plan(cid):
    blocked = _creator_guard(cid)
    if blocked:
        return blocked
    data = request.get_json(force=True)
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "title required"}), 400
    pid = db.add_plan(cid, title, data.get("unit_key"))
    return jsonify({"id": pid})


@app.route("/api/plan/item/<int:item_id>", methods=["POST"])
def set_plan_status(item_id):
    blocked = _creator_guard(db.plan_item_creator(item_id) or -1)
    if blocked:
        return blocked
    data = request.get_json(force=True)
    db.update_plan_status(item_id, data.get("status", "idea"))
    return jsonify({"ok": True})


@app.route("/api/plan/item/<int:item_id>", methods=["DELETE"])
def remove_plan(item_id):
    blocked = _creator_guard(db.plan_item_creator(item_id) or -1)
    if blocked:
        return blocked
    db.delete_plan(item_id)
    return jsonify({"ok": True})


@app.route("/api/scan_all", methods=["POST"])
def scan_all():
    results = [radar.run_scan(c["id"]) for c in db.list_creators()]
    return jsonify(results)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8782"))
    app.run(host="127.0.0.1", port=port, debug=True)
