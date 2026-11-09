"""SQLite logbook for JS Entertainment.

One file, no server. Stores the roster (creators + their niches), the seed
accounts/hashtags we watch, every video we've seen, a time-series of each
video's stats (snapshots), and a per-scan record of detected trends so we can
measure momentum scan-over-scan.
"""

import json
import os
import re
import secrets
import sqlite3
import time

DB_PATH = os.path.join(os.path.dirname(__file__), "jsent.db")


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def init_db():
    conn = connect()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS creators (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            niche       TEXT NOT NULL,
            created_at  REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS seeds (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            creator_id  INTEGER NOT NULL,
            platform    TEXT NOT NULL,          -- 'tiktok' | 'instagram'
            kind        TEXT NOT NULL,          -- 'account' | 'hashtag' | 'self'
            value       TEXT NOT NULL,
            niche       TEXT,                   -- which of the creator's niches this seed serves
            UNIQUE(creator_id, platform, kind, value)
        );

        CREATE TABLE IF NOT EXISTS videos (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            platform           TEXT NOT NULL,
            platform_video_id  TEXT NOT NULL,
            creator_id         INTEGER NOT NULL,
            author             TEXT,
            url                TEXT,
            caption            TEXT,
            audio_id           TEXT,
            audio_name         TEXT,
            hashtags           TEXT,             -- json list
            topic              TEXT,             -- youtube: the niche keyword
            media_type         TEXT,             -- instagram: 'reel' | 'post'
            source             TEXT,             -- the seed keyword/handle that surfaced this post
            cover              TEXT,             -- thumbnail/cover image URL
            video_url          TEXT,             -- downloadable video URL (for real-frame visual AI analysis)
            slides             TEXT,             -- json: carousel child images/clips
            post_time          REAL,
            first_seen         REAL,
            UNIQUE(platform, platform_video_id)
        );

        CREATE TABLE IF NOT EXISTS snapshots (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id   INTEGER NOT NULL,
            scan_time  REAL NOT NULL,
            views      INTEGER DEFAULT 0,
            likes      INTEGER DEFAULT 0,
            comments   INTEGER DEFAULT 0,
            shares     INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS trend_snapshots (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            creator_id    INTEGER NOT NULL,
            scan_time     REAL NOT NULL,
            unit_type     TEXT NOT NULL,        -- 'audio' | 'hashtag'
            unit_key      TEXT NOT NULL,        -- stable id for the trend unit
            unit_label    TEXT,                 -- human label
            hit_count     INTEGER,              -- overperforming videos using it
            account_count INTEGER,              -- distinct accounts using it
            overperf_sum  REAL,                 -- total overperformance
            score         REAL,
            momentum      TEXT,                 -- new|rising|steady|fading
            status        TEXT,                 -- 'buy' | 'watch' | 'saturated'
            niche         TEXT,                 -- which creator niche this trend belongs to
            medium        TEXT,                 -- 'video' | 'photo' — film idea vs photo-post idea
            description   TEXT,                 -- AI: what the trend concept actually is
            examples      TEXT,                 -- AI: json list of example posts {url,author,views}
            sound         TEXT,                 -- the driving sound/audio name (concept or audio trend)
            your_take     TEXT,                 -- AI: how to adapt this viral trend into the creator's niche
            channel       TEXT,                 -- 'tiktok' | 'reels' | 'carousel' (primary section)
            platforms     TEXT                  -- json list of every platform this trend's riders came from
        );

        CREATE TABLE IF NOT EXISTS briefs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            creator_id  INTEGER NOT NULL,
            unit_key    TEXT NOT NULL,
            brief_json  TEXT NOT NULL,         -- the generated brief
            created_at  REAL NOT NULL,
            UNIQUE(creator_id, unit_key)
        );

        CREATE TABLE IF NOT EXISTS plan (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            creator_id  INTEGER NOT NULL,
            unit_key    TEXT,
            title       TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'idea',   -- idea | filming | posted
            created_at  REAL NOT NULL,
            updated_at  REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS deals (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            creator_id    INTEGER NOT NULL,
            message_id    TEXT NOT NULL,        -- email Message-ID (dedup); synthetic in demo
            from_addr     TEXT,
            brand         TEXT,
            subject       TEXT,
            body          TEXT,
            received_at   REAL,
            offer_type    TEXT,                 -- paid | gifting | affiliate | unclear
            offer_amount  TEXT,                 -- as stated, e.g. "$2,000"
            deliverables  TEXT,
            deadline      TEXT,
            value_score   INTEGER DEFAULT 0,    -- 0-100 how good the offer is
            fit_score     INTEGER DEFAULT 0,    -- 0-100 alignment with the niche
            flags         TEXT,                 -- json list of red-flag warnings
            summary       TEXT,
            contact_name  TEXT,                 -- first name of the person who emailed
            status        TEXT DEFAULT 'new',   -- new | interested | passed
            reply_status  TEXT DEFAULT 'none',  -- none | yes | no | negotiate
            reply_body    TEXT,
            replied_at    REAL,
            replied_by    TEXT,                 -- 'creator' | 'manager'
            created_at    REAL NOT NULL,
            UNIQUE(creator_id, message_id)
        );

        -- the full back-and-forth for a deal: each brand email ('in') and each
        -- creator/manager reply ('out') is one row, so a deal reads as a conversation.
        CREATE TABLE IF NOT EXISTS deal_messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            deal_id     INTEGER NOT NULL,
            direction   TEXT NOT NULL,          -- 'in' (brand) | 'out' (creator/manager)
            from_addr   TEXT,
            from_name   TEXT,
            body        TEXT,
            message_id  TEXT,                   -- email Message-ID, for threading brand replies
            created_at  REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS groups (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT NOT NULL,
            invite_token TEXT,                  -- shareable self-signup link for creators
            created_at   REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS stream_snapshots (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            creator_id    INTEGER,
            scan_time     REAL NOT NULL,
            source        TEXT,                 -- 'twitch' | 'youtube_live'
            category      TEXT,
            viewers       INTEGER,
            streams_count INTEGER,
            opportunity   REAL,
            momentum      TEXT,
            status        TEXT
        );

        CREATE TABLE IF NOT EXISTS saved (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            creator_id  INTEGER NOT NULL,
            url         TEXT NOT NULL,        -- the post URL (also the dedup key)
            cover       TEXT,                 -- thumbnail for the board
            video_url   TEXT,                 -- raw video (proxied) so a saved reel plays
            slides      TEXT,                 -- json: carousel child images/clips to swipe
            author      TEXT,
            medium      TEXT,                 -- 'video' | 'photo' | 'carousel'
            likes       INTEGER,
            views       INTEGER,
            saved_at    REAL NOT NULL,
            UNIQUE(creator_id, url)
        );

        CREATE INDEX IF NOT EXISTS idx_plan_creator ON plan(creator_id);
        CREATE INDEX IF NOT EXISTS idx_snap_video ON snapshots(video_id);
        CREATE INDEX IF NOT EXISTS idx_video_creator ON videos(creator_id);

        CREATE INDEX IF NOT EXISTS idx_trend_creator ON trend_snapshots(creator_id, scan_time);
        CREATE INDEX IF NOT EXISTS idx_deals_creator ON deals(creator_id);
        """
    )
    conn.commit()
    conn.close()
    _migrate_groups()


def _add_column(conn, table, col, ddl):
    """Add `col` to `table` if missing. Idempotent and safe when two gunicorn
    workers race init_db on boot: both can read the column as missing and both
    issue the ALTER — the loser gets 'duplicate column', which we swallow."""
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if not cols or col in cols:        # table absent, or column already present
        return
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
        conn.commit()
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():
            raise


def _migrate_groups():
    """Add columns to existing tables as the schema grows, and reparent any
    creators with no group into a default group (so old rosters aren't orphaned).
    Every column add goes through _add_column so a concurrent worker racing
    this on boot can't crash the migration mid-flight."""
    conn = connect()
    for col, ddl in (
        ("group_id", "group_id INTEGER"),
        ("password", "password TEXT"),
        ("formats", "formats TEXT"),
        ("deal_slug", "deal_slug TEXT"),
        ("gender", "gender TEXT"),
        # a 'hub' is a hidden creator that owns the SHARED scan for one niche+gender,
        # so a niche is scraped once for everyone in it (real creators read its trends).
        ("is_hub", "is_hub INTEGER"),
        # the creator's age — used to find similar-age accounts to watch (best-effort).
        ("age", "age INTEGER"),
        # a rich characterization of the seed's STYLE + content (built from their real
        # posts), used to find + filter genuinely similar accounts — across any niche.
        ("style_profile", "style_profile TEXT"),
    ):
        _add_column(conn, "creators", col, ddl)
    # deals reply columns (added after the deals table first shipped)
    for col, ddl in (
        ("reply_status", "reply_status TEXT DEFAULT 'none'"),
        ("reply_body", "reply_body TEXT"),
        ("replied_at", "replied_at REAL"),
        ("replied_by", "replied_by TEXT"),
        ("contact_name", "contact_name TEXT"),
    ):
        _add_column(conn, "deals", col, ddl)
    for col, ddl in (
        ("topic", "topic TEXT"),
        ("media_type", "media_type TEXT"),
        ("source", "source TEXT"),
        ("cover", "cover TEXT"),
        ("video_url", "video_url TEXT"),
        ("slides", "slides TEXT"),        # json: carousel child images/clips
    ):
        _add_column(conn, "videos", col, ddl)
    _add_column(conn, "seeds", "niche", "niche TEXT")
    for col, ddl in (
        ("niche", "niche TEXT"),
        ("description", "description TEXT"),
        ("examples", "examples TEXT"),
        ("sound", "sound TEXT"),
        ("your_take", "your_take TEXT"),
        ("channel", "channel TEXT"),
        ("platforms", "platforms TEXT"),
        ("platform", "platform TEXT"),
        ("medium", "medium TEXT"),     # 'video' | 'photo' — is the trend a film or a photo-post idea
    ):
        _add_column(conn, "trend_snapshots", col, ddl)
    _add_column(conn, "saved", "video_url", "video_url TEXT")   # play saved reels in the lightbox
    _add_column(conn, "saved", "slides", "slides TEXT")         # swipe saved carousels
    _add_column(conn, "groups", "invite_token", "invite_token TEXT")   # creator self-signup link
    # stream_snapshots moved from group-keyed to creator-keyed; it's just a cache,
    # so rebuild the table if it still has the old group_id column.
    scols = [r["name"] for r in conn.execute("PRAGMA table_info(stream_snapshots)").fetchall()]
    if scols and "group_id" in scols:
        conn.execute("DROP TABLE stream_snapshots")
        conn.execute("""CREATE TABLE stream_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, creator_id INTEGER, scan_time REAL NOT NULL,
            source TEXT, category TEXT, viewers INTEGER, streams_count INTEGER,
            opportunity REAL, momentum TEXT, status TEXT)""")
        conn.commit()
    conn.execute("CREATE INDEX IF NOT EXISTS idx_stream_creator ON stream_snapshots(creator_id, scan_time)")
    conn.commit()
    orphans = conn.execute(
        "SELECT COUNT(*) AS n FROM creators WHERE group_id IS NULL"
    ).fetchone()["n"]
    if orphans:
        g = conn.execute("SELECT id FROM groups ORDER BY id LIMIT 1").fetchone()
        if g:
            gid = g["id"]
        else:
            old = conn.execute(
                "SELECT value FROM settings WHERE key='group_name'"
            ).fetchone()
            name = (old["value"] if old and old["value"] else "Group 1")
            cur = conn.execute(
                "INSERT INTO groups (name, created_at) VALUES (?,?)", (name, time.time())
            )
            gid = cur.lastrowid
        conn.execute("UPDATE creators SET group_id=? WHERE group_id IS NULL", (gid,))
        conn.commit()
    # one-time backfill: seed conversation threads from existing deals so old deals
    # still show their history (brand email + any reply) in the new thread view.
    if not conn.execute("SELECT COUNT(*) AS n FROM deal_messages").fetchone()["n"]:
        for d in conn.execute("SELECT * FROM deals").fetchall():
            conn.execute("INSERT INTO deal_messages (deal_id,direction,from_addr,from_name,body,message_id,created_at) VALUES (?,?,?,?,?,?,?)",
                         (d["id"], "in", d["from_addr"], d["contact_name"] or d["brand"], d["body"], d["message_id"], d["received_at"] or d["created_at"]))
            if d["reply_body"]:
                conn.execute("INSERT INTO deal_messages (deal_id,direction,from_addr,from_name,body,message_id,created_at) VALUES (?,?,?,?,?,?,?)",
                             (d["id"], "out", None, None, d["reply_body"], None, d["replied_at"] or d["created_at"]))
        conn.commit()
    conn.close()


# ---------- settings ----------

def get_setting(key, default=None):
    conn = connect()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key, value):
    conn = connect()
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, value)
    )
    conn.commit()
    conn.close()


# ---------- per-niche handle sets (blocklist + 'already mined for collabs') ----------
# Kept in settings (JSON), keyed by niche+gender, so they survive hub-seed rebuilds.

def _lane(niche, gender):
    return f"{(niche or '').strip().lower()}:{(gender or 'any').strip().lower()}"


def _get_handle_set(key):
    raw = get_setting(key, "")
    try:
        return set(json.loads(raw)) if raw else set()
    except Exception:
        return set()


def _add_handles(key, handles):
    s = _get_handle_set(key)
    s.update((h or "").strip().lstrip("@").lower() for h in handles if (h or "").strip())
    set_setting(key, json.dumps(sorted(s)))


def blocked_accounts(niche, gender):
    """Handles the manager REMOVED from the watch-list — discovery must never re-add them
    (so a brand you delete can't sneak back in on the next scan)."""
    return _get_handle_set("blocked:" + _lane(niche, gender))


def block_account(niche, gender, handle):
    _add_handles("blocked:" + _lane(niche, gender), [handle])


def unblock_account(niche, gender, handle):
    key = "blocked:" + _lane(niche, gender)
    s = _get_handle_set(key)
    s.discard((handle or "").strip().lstrip("@").lower())
    set_setting(key, json.dumps(sorted(s)))


def mined_accounts(niche, gender):
    """Watch accounts whose collabs/tags/mentions we've already harvested this cycle."""
    return _get_handle_set("mined:" + _lane(niche, gender))


def mark_mined(niche, gender, handles):
    _add_handles("mined:" + _lane(niche, gender), handles)


def reset_mined(niche, gender):
    set_setting("mined:" + _lane(niche, gender), "")


def seed_value(seed_id):
    conn = connect()
    row = conn.execute("SELECT value FROM seeds WHERE id=?", (seed_id,)).fetchone()
    conn.close()
    return row["value"] if row else None


# ---------- saved board (a creator's "save these ideas for later" list) ----------

def add_saved(creator_id, item):
    conn = connect()
    conn.execute(
        "INSERT OR IGNORE INTO saved (creator_id, url, cover, video_url, slides, author, medium, likes, views, saved_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (creator_id, item.get("url"), item.get("cover"), item.get("video_url"), json.dumps(item.get("slides") or []),
         item.get("author"), item.get("medium"), int(item.get("likes") or 0), int(item.get("views") or 0), time.time()),
    )
    conn.commit()
    conn.close()


def remove_saved(creator_id, url):
    conn = connect()
    conn.execute("DELETE FROM saved WHERE creator_id=? AND url=?", (creator_id, url))
    conn.commit()
    conn.close()


def list_saved(creator_id):
    conn = connect()
    rows = conn.execute(
        "SELECT id, url, cover, video_url, slides, author, medium, likes, views, saved_at FROM saved "
        "WHERE creator_id=? ORDER BY saved_at DESC", (creator_id,)
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["slides"] = json.loads(d.get("slides") or "[]")
        out.append(d)
    return out


# ---------- groups ----------

def add_group(name):
    conn = connect()
    cur = conn.execute(
        "INSERT INTO groups (name, created_at) VALUES (?,?)", (name, time.time())
    )
    conn.commit()
    gid = cur.lastrowid
    conn.close()
    return gid


def list_groups():
    conn = connect()
    rows = conn.execute("SELECT * FROM groups ORDER BY created_at").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_group(group_id):
    conn = connect()
    row = conn.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def rename_group(group_id, name):
    conn = connect()
    conn.execute("UPDATE groups SET name=? WHERE id=?", (name, group_id))
    conn.commit()
    conn.close()


def group_invite_token(group_id, regenerate=False):
    """The shareable creator-signup token for a group; created on first use.
    regenerate=True mints a fresh one (revoking any link already sent out)."""
    conn = connect()
    row = conn.execute("SELECT invite_token FROM groups WHERE id=?", (group_id,)).fetchone()
    if not row:
        conn.close()
        return None
    tok = row["invite_token"]
    if not tok or regenerate:
        tok = secrets.token_urlsafe(12)
        conn.execute("UPDATE groups SET invite_token=? WHERE id=?", (tok, group_id))
        conn.commit()
    conn.close()
    return tok


def group_by_invite(token):
    """The group a signup token belongs to, or None."""
    if not token:
        return None
    conn = connect()
    row = conn.execute("SELECT * FROM groups WHERE invite_token=?", (token,)).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_group(group_id):
    """Delete a group and every creator in it (and their data)."""
    for c in list_creators(group_id):
        delete_creator(c["id"])
    conn = connect()
    conn.execute("DELETE FROM groups WHERE id=?", (group_id,))
    conn.commit()
    conn.close()


# ---------- roster ----------

def add_creator(name, niche, group_id, password=None, formats=None, gender=None, age=None):
    conn = connect()
    cur = conn.execute(
        "INSERT INTO creators (name, niche, group_id, password, formats, gender, age, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (name, niche, group_id, (password or None), json.dumps(formats or []),
         (gender or None), (int(age) if age else None), time.time()),
    )
    conn.commit()
    cid = cur.lastrowid
    conn.close()
    return cid


def set_style_profile(creator_id, profile):
    conn = connect()
    conn.execute("UPDATE creators SET style_profile=? WHERE id=?", (profile or None, creator_id))
    conn.commit()
    conn.close()


def update_creator(creator_id, name, niche, formats, gender=None, age=None):
    conn = connect()
    conn.execute(
        "UPDATE creators SET name=?, niche=?, formats=?, gender=?, age=? WHERE id=?",
        (name, niche, json.dumps(formats or []), (gender or None),
         (int(age) if age else None), creator_id),
    )
    conn.commit()
    conn.close()


def remove_seed(seed_id):
    conn = connect()
    conn.execute("DELETE FROM seeds WHERE id=?", (seed_id,))
    conn.commit()
    conn.close()


def seed_creator(seed_id):
    conn = connect()
    row = conn.execute("SELECT creator_id FROM seeds WHERE id=?", (seed_id,)).fetchone()
    conn.close()
    return row["creator_id"] if row else None


def set_creator_password(creator_id, password):
    conn = connect()
    conn.execute(
        "UPDATE creators SET password=? WHERE id=?", ((password or None), creator_id)
    )
    conn.commit()
    conn.close()


def plan_item_creator(item_id):
    conn = connect()
    row = conn.execute("SELECT creator_id FROM plan WHERE id=?", (item_id,)).fetchone()
    conn.close()
    return row["creator_id"] if row else None


def list_creators(group_id=None, include_hubs=False):
    conn = connect()
    where, params = [], []
    if group_id is not None:
        where.append("group_id=?"); params.append(group_id)
    if not include_hubs:                     # hubs are internal scan owners, never shown
        where.append("COALESCE(is_hub,0)=0")
    sql = "SELECT * FROM creators" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created_at"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [_creator_row(r) for r in rows]


# ---------- niche hubs (one shared scan per niche+gender) ----------

def distinct_niche_genders():
    """Every (niche, gender) pair that at least one real creator belongs to. A creator's
    niche field may be comma-separated, so each token is its own scannable lane."""
    seen = set()
    for c in list_creators(include_hubs=False):
        g = (c.get("gender") or "").strip().lower()
        for n in (c.get("niche") or "").split(","):
            n = n.strip()
            if n:
                seen.add((n, g))
    return sorted(seen)


def creators_in_niche(niche, gender):
    """Real creators that share one niche+gender lane (read the same hub trends). Niche
    match is case-insensitive so 'Streetwear' and 'streetwear' stay in one lane."""
    g = (gender or "").strip().lower()
    nl = (niche or "").strip().lower()
    out = []
    for c in list_creators(include_hubs=False):
        tokens = [n.strip().lower() for n in (c.get("niche") or "").split(",")]
        if nl in tokens and (c.get("gender") or "").strip().lower() == g:
            out.append(c)
    return out


def hub_for(niche, gender):
    """The hub creator id for a niche+gender, or None if not created yet (read-only).
    Case-insensitive niche match (prefers the oldest hub if casing ever duplicated)."""
    g = (gender or "").strip().lower() or None
    conn = connect()
    row = conn.execute(
        "SELECT id FROM creators WHERE COALESCE(is_hub,0)=1 AND LOWER(TRIM(niche))=LOWER(TRIM(?)) "
        "AND COALESCE(LOWER(gender),'')=COALESCE(?,'') ORDER BY id ASC", ((niche or "").strip(), g)
    ).fetchone()
    conn.close()
    return row["id"] if row else None


def ensure_hub(niche, gender):
    """Find or create the hidden hub creator that owns the shared scan for niche+gender."""
    hid = hub_for(niche, gender)
    if hid:
        return hid
    g = (gender or "").strip().lower() or None
    nz = (niche or "").strip()
    conn = connect()
    cur = conn.execute(
        "INSERT INTO creators (name, niche, group_id, password, formats, gender, is_hub, created_at) "
        "VALUES (?,?,?,?,?,?,1,?)",
        (f"[hub] {nz} · {g or 'any'}", nz, None, None, json.dumps(["short"]), g, time.time()),
    )
    conn.commit(); hid = cur.lastrowid; conn.close()
    return hid


def sync_hub_seeds(niche, gender):
    """Rebuild a hub's watch-list = the union of WATCH accounts (kind='account') from every
    creator in that niche+gender. The creators' OWN accounts (kind='self') are NOT scanned —
    a creator sees trends from OTHER creators in their lane, not their own posts.
    Returns (hub_id, account_count)."""
    hub_id = ensure_hub(niche, gender)
    pool = {}                                # (platform, lower-handle) -> original-cased handle
    for m in creators_in_niche(niche, gender):
        for s in list_seeds(m["id"]):
            if s["kind"] != "account":       # watch targets only — not 'self', not hashtags
                continue
            if (s.get("niche") or "").strip().lower() != (niche or "").strip().lower():
                continue                     # a multi-niche creator's seed for a DIFFERENT lane
            pool[(s["platform"], s["value"].lower())] = s["value"]
    conn = connect()
    conn.execute("DELETE FROM seeds WHERE creator_id=?", (hub_id,))
    conn.commit(); conn.close()
    for (platform, _), val in pool.items():
        add_seed(hub_id, platform, "account", val, niche)
    return hub_id, len(pool)


def _creator_row(row):
    d = dict(row)
    d["formats"] = json.loads(d.get("formats") or "[]")
    return d


def get_creator(creator_id):
    conn = connect()
    row = conn.execute("SELECT * FROM creators WHERE id=?", (creator_id,)).fetchone()
    conn.close()
    return _creator_row(row) if row else None


def delete_creator(creator_id):
    conn = connect()
    conn.execute(
        "DELETE FROM snapshots WHERE video_id IN (SELECT id FROM videos WHERE creator_id=?)",
        (creator_id,),
    )
    for tbl in ("videos", "trend_snapshots", "briefs", "plan", "seeds"):
        conn.execute(f"DELETE FROM {tbl} WHERE creator_id=?", (creator_id,))
    conn.execute("DELETE FROM creators WHERE id=?", (creator_id,))
    conn.commit()
    conn.close()


# ---------- seeds ----------

def add_seed(creator_id, platform, kind, value, niche=None):
    conn = connect()
    conn.execute(
        "INSERT OR IGNORE INTO seeds (creator_id, platform, kind, value, niche) VALUES (?,?,?,?,?)",
        (creator_id, platform, kind, value.strip().lstrip("#@"), niche),
    )
    conn.commit()
    conn.close()


def replace_self_seeds(creator_id, seeds):
    """Swap the creator's own accounts (kind='self') for a fresh list."""
    conn = connect()
    conn.execute("DELETE FROM seeds WHERE creator_id=? AND kind='self'", (creator_id,))
    conn.commit()
    conn.close()
    for s in seeds:
        if (s.get("value") or "").strip():
            add_seed(creator_id, s["platform"], "self", s["value"])


def clear_derived_seeds(creator_id):
    """Drop the niche-derived HASHTAG seeds so they can be rebuilt when the creator's
    niche or formats change. Keeps 'self' accounts AND 'account' watch-targets (the
    creators they follow) — those are not niche-derived and must survive a re-lane."""
    conn = connect()
    conn.execute("DELETE FROM seeds WHERE creator_id=? AND kind NOT IN ('self','account')", (creator_id,))
    conn.commit()
    conn.close()


def reset_scanned_for_relane(creator_id, keep_authors):
    """When a creator changes WHAT THEY POST ABOUT (niche), wipe their previously
    scanned trends and the scraped (non-self) posts behind them — so old trends vanish
    and old-niche posts can't bleed into / conjoin with the new lane on the next scan.
    The creator's OWN posts (self accounts) are kept; their stats/followers/what's-
    working don't depend on niche."""
    keep = {(a or "").lstrip("@").lower() for a in keep_authors}
    conn = connect()
    conn.execute("DELETE FROM trend_snapshots WHERE creator_id=?", (creator_id,))
    rows = conn.execute("SELECT id, author FROM videos WHERE creator_id=?", (creator_id,)).fetchall()
    drop = [r["id"] for r in rows if (r["author"] or "").lstrip("@").lower() not in keep]
    for vid in drop:
        conn.execute("DELETE FROM snapshots WHERE video_id=?", (vid,))
    if drop:
        conn.executemany("DELETE FROM videos WHERE id=?", [(i,) for i in drop])
    conn.commit()
    conn.close()
    return len(drop)


def list_seeds(creator_id):
    conn = connect()
    rows = conn.execute(
        "SELECT * FROM seeds WHERE creator_id=? ORDER BY platform, kind", (creator_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------- videos + snapshots ----------

def upsert_video(v, creator_id):
    """Insert a video if new; returns its row id. v is a dict from a source."""
    conn = connect()
    now = time.time()
    conn.execute(
        """INSERT INTO videos
           (platform, platform_video_id, creator_id, author, url, caption,
            audio_id, audio_name, hashtags, topic, media_type, source, cover, video_url, slides, post_time, first_seen)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(platform, platform_video_id) DO UPDATE SET
             cover=COALESCE(NULLIF(excluded.cover,''), cover),
             video_url=COALESCE(NULLIF(excluded.video_url,''), video_url),
             slides=CASE WHEN excluded.slides NOT IN ('','[]') THEN excluded.slides ELSE slides END""",
        (
            v["platform"], str(v["video_id"]), creator_id, v.get("author"),
            v.get("url"), v.get("caption"), v.get("audio_id"),
            v.get("audio_name"), json.dumps(v.get("hashtags", [])),
            v.get("topic"), v.get("media_type"), v.get("source"), v.get("cover"),
            v.get("video_url"), json.dumps(v.get("slides") or []), v.get("post_time"), now,
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM videos WHERE platform=? AND platform_video_id=?",
        (v["platform"], str(v["video_id"])),
    ).fetchone()
    conn.close()
    return row["id"]


def insert_snapshot(video_id, stats, scan_time):
    conn = connect()
    conn.execute(
        """INSERT INTO snapshots (video_id, scan_time, views, likes, comments, shares)
           VALUES (?,?,?,?,?,?)""",
        (video_id, scan_time, stats.get("views", 0), stats.get("likes", 0),
         stats.get("comments", 0), stats.get("shares", 0)),
    )
    conn.commit()
    conn.close()


def latest_video_stats(creator_id):
    """Latest snapshot per video for a creator, joined with video metadata."""
    conn = connect()
    rows = conn.execute(
        """
        SELECT v.*, s.views, s.likes, s.comments, s.shares, s.scan_time
        FROM videos v
        JOIN snapshots s ON s.video_id = v.id
        JOIN (SELECT video_id, MAX(scan_time) AS mx FROM snapshots GROUP BY video_id) last
          ON last.video_id = s.video_id AND last.mx = s.scan_time
        WHERE v.creator_id = ?
        """,
        (creator_id,),
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["hashtags"] = json.loads(d.get("hashtags") or "[]")
        d["slides"] = json.loads(d.get("slides") or "[]")
        out.append(d)
    return out


# ---------- trend snapshots ----------

def insert_trend_snapshot(t):
    conn = connect()
    conn.execute(
        """INSERT INTO trend_snapshots
           (creator_id, scan_time, unit_type, unit_key, unit_label, platform, hit_count,
            account_count, overperf_sum, score, momentum, status, niche, medium, description, examples,
            sound, your_take, channel, platforms)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (t["creator_id"], t["scan_time"], t["unit_type"], t["unit_key"],
         t["unit_label"], t.get("platform"), t["hit_count"], t["account_count"],
         t["overperf_sum"], t["score"], t["momentum"], t["status"], t.get("niche"),
         t.get("medium"), t.get("description"), json.dumps(t["examples"]) if t.get("examples") else None,
         t.get("sound"), t.get("your_take"), t.get("channel"),
         json.dumps(t["platforms"]) if t.get("platforms") else None),
    )
    conn.commit()
    conn.close()


def prior_trend_counts(creator_id, before_scan_time):
    """Map unit_key -> hit_count from the most recent scan BEFORE the given time.
    Used to compute momentum (this scan vs last scan)."""
    conn = connect()
    prev = conn.execute(
        "SELECT MAX(scan_time) AS mx FROM trend_snapshots WHERE creator_id=? AND scan_time < ?",
        (creator_id, before_scan_time),
    ).fetchone()
    if not prev or prev["mx"] is None:
        conn.close()
        return {}
    rows = conn.execute(
        "SELECT unit_key, hit_count FROM trend_snapshots WHERE creator_id=? AND scan_time=?",
        (creator_id, prev["mx"]),
    ).fetchall()
    conn.close()
    return {r["unit_key"]: r["hit_count"] for r in rows}


# ---------- stream radar ----------

def insert_stream_snapshot(s):
    conn = connect()
    conn.execute(
        """INSERT INTO stream_snapshots
           (creator_id, scan_time, source, category, viewers, streams_count, opportunity, momentum, status)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (s["creator_id"], s["scan_time"], s["source"], s["category"], s["viewers"],
         s["streams_count"], s["opportunity"], s["momentum"], s["status"]),
    )
    conn.commit()
    conn.close()


def prior_stream_viewers(creator_id, before_scan_time):
    conn = connect()
    prev = conn.execute(
        "SELECT MAX(scan_time) AS mx FROM stream_snapshots WHERE creator_id=? AND scan_time < ?",
        (creator_id, before_scan_time),
    ).fetchone()
    if not prev or prev["mx"] is None:
        conn.close()
        return {}
    rows = conn.execute(
        "SELECT category, viewers FROM stream_snapshots WHERE creator_id=? AND scan_time=?",
        (creator_id, prev["mx"]),
    ).fetchall()
    conn.close()
    return {r["category"]: r["viewers"] for r in rows}


def latest_streams(creator_id):
    conn = connect()
    last = conn.execute(
        "SELECT MAX(scan_time) AS mx FROM stream_snapshots WHERE creator_id=?", (creator_id,)
    ).fetchone()
    if not last or last["mx"] is None:
        conn.close()
        return None, []
    rows = conn.execute(
        "SELECT * FROM stream_snapshots WHERE creator_id=? AND scan_time=? ORDER BY opportunity DESC",
        (creator_id, last["mx"]),
    ).fetchall()
    conn.close()
    return last["mx"], [dict(r) for r in rows]


def stream_scan_count(creator_id):
    conn = connect()
    n = conn.execute(
        "SELECT COUNT(DISTINCT scan_time) AS n FROM stream_snapshots WHERE creator_id=?",
        (creator_id,),
    ).fetchone()["n"]
    conn.close()
    return n


# ---------- briefs ----------

def save_brief(creator_id, unit_key, brief_json):
    conn = connect()
    conn.execute(
        """INSERT OR REPLACE INTO briefs (creator_id, unit_key, brief_json, created_at)
           VALUES (?,?,?,?)""",
        (creator_id, unit_key, brief_json, time.time()),
    )
    conn.commit()
    conn.close()


def get_brief(creator_id, unit_key):
    conn = connect()
    row = conn.execute(
        "SELECT brief_json, created_at FROM briefs WHERE creator_id=? AND unit_key=?",
        (creator_id, unit_key),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


# ---------- the plan (content calendar) ----------

def add_plan(creator_id, title, unit_key=None):
    conn = connect()
    now = time.time()
    # avoid duplicates of the same unit for the same creator
    if unit_key:
        existing = conn.execute(
            "SELECT id FROM plan WHERE creator_id=? AND unit_key=? AND status!='posted'",
            (creator_id, unit_key),
        ).fetchone()
        if existing:
            conn.close()
            return existing["id"]
    cur = conn.execute(
        """INSERT INTO plan (creator_id, unit_key, title, status, created_at, updated_at)
           VALUES (?,?,?,?,?,?)""",
        (creator_id, unit_key, title, "idea", now, now),
    )
    conn.commit()
    pid = cur.lastrowid
    conn.close()
    return pid


def list_plan(creator_id):
    conn = connect()
    rows = conn.execute(
        "SELECT * FROM plan WHERE creator_id=? ORDER BY created_at, id", (creator_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def all_plan():
    conn = connect()
    rows = conn.execute(
        """SELECT p.*, c.name AS creator_name FROM plan p
           JOIN creators c ON c.id = p.creator_id ORDER BY p.updated_at DESC"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def group_plan(group_id):
    conn = connect()
    rows = conn.execute(
        """SELECT p.*, c.name AS creator_name FROM plan p
           JOIN creators c ON c.id = p.creator_id
           WHERE c.group_id = ? ORDER BY p.created_at, p.id""",
        (group_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_plan_status(item_id, status):
    conn = connect()
    conn.execute(
        "UPDATE plan SET status=?, updated_at=? WHERE id=?",
        (status, time.time(), item_id),
    )
    conn.commit()
    conn.close()


def delete_plan(item_id):
    conn = connect()
    conn.execute("DELETE FROM plan WHERE id=?", (item_id,))
    conn.commit()
    conn.close()


# ---------- brand deals ----------

def _slugify(name):
    s = re.sub(r"[^a-z0-9]+", "", (name or "").lower())
    return s or "creator"


def deal_slug(creator_id):
    """The unique mailbox slug for a creator's brand-deal address. Created lazily
    and stored, so it stays stable once a creator starts handing the address out."""
    cr = get_creator(creator_id)
    if not cr:
        return None
    if cr.get("deal_slug"):
        return cr["deal_slug"]
    base = _slugify(cr["name"])
    conn = connect()
    taken = {r["deal_slug"] for r in
             conn.execute("SELECT deal_slug FROM creators WHERE deal_slug IS NOT NULL").fetchall()}
    slug = base if base not in taken else f"{base}{creator_id}"
    conn.execute("UPDATE creators SET deal_slug=? WHERE id=?", (slug, creator_id))
    conn.commit()
    conn.close()
    return slug


def creator_by_slug(slug):
    conn = connect()
    row = conn.execute("SELECT * FROM creators WHERE deal_slug=?", (slug,)).fetchone()
    conn.close()
    return _creator_row(row) if row else None


def add_deal(d):
    """Insert a scored deal; ignored if we've already seen this message for the creator.
    Returns True if newly inserted."""
    conn = connect()
    cur = conn.execute(
        """INSERT OR IGNORE INTO deals
           (creator_id, message_id, from_addr, brand, subject, body, received_at,
            offer_type, offer_amount, deliverables, deadline, value_score, fit_score,
            flags, summary, contact_name, status, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (d["creator_id"], d["message_id"], d.get("from_addr"), d.get("brand"),
         d.get("subject"), d.get("body"), d.get("received_at"), d.get("offer_type"),
         d.get("offer_amount"), d.get("deliverables"), d.get("deadline"),
         d.get("value_score", 0), d.get("fit_score", 0),
         json.dumps(d.get("flags") or []), d.get("summary"), d.get("contact_name"),
         d.get("status", "new"), time.time()),
    )
    inserted = cur.rowcount > 0
    if inserted:                                    # seed the thread with the brand's first email
        conn.execute("INSERT INTO deal_messages (deal_id,direction,from_addr,from_name,body,message_id,created_at) VALUES (?,?,?,?,?,?,?)",
                     (cur.lastrowid, "in", d.get("from_addr"), d.get("contact_name") or d.get("brand"),
                      d.get("body"), d.get("message_id"), d.get("received_at") or time.time()))
    conn.commit()
    conn.close()
    return inserted


def list_deals(creator_id):
    """Deals for a creator, best-first (combined value + fit), passed ones last."""
    conn = connect()
    rows = conn.execute(
        """SELECT * FROM deals WHERE creator_id=?
           ORDER BY (status='passed') ASC, (value_score + fit_score) DESC, received_at DESC""",
        (creator_id,),
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["flags"] = json.loads(d.get("flags") or "[]")
        out.append(d)
    return out


def seen_message_ids(creator_id):
    conn = connect()
    rows = conn.execute("SELECT message_id FROM deals WHERE creator_id=?", (creator_id,)).fetchall()
    conn.close()
    return {r["message_id"] for r in rows}


def set_deal_status(deal_id, status):
    conn = connect()
    conn.execute("UPDATE deals SET status=? WHERE id=?", (status, deal_id))
    conn.commit()
    conn.close()


def get_deal(deal_id):
    conn = connect()
    row = conn.execute("SELECT * FROM deals WHERE id=?", (deal_id,)).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["flags"] = json.loads(d.get("flags") or "[]")
    return d


def set_deal_reply(deal_id, kind, body, replied_by):
    """Record a sent reply. 'no' declines the deal (passed); yes/negotiate keep it active."""
    status = "passed" if kind == "no" else "interested"
    conn = connect()
    conn.execute(
        "UPDATE deals SET reply_status=?, reply_body=?, replied_at=?, replied_by=?, status=? WHERE id=?",
        (kind, body, time.time(), replied_by, status, deal_id),
    )
    conn.commit()
    conn.close()


# ---------- deal conversation threads ----------

def add_deal_message(deal_id, direction, body, from_addr=None, from_name=None, message_id=None):
    """Append one message to a deal's thread. direction: 'in' (brand) | 'out' (creator)."""
    conn = connect()
    conn.execute("INSERT INTO deal_messages (deal_id,direction,from_addr,from_name,body,message_id,created_at) VALUES (?,?,?,?,?,?,?)",
                 (deal_id, direction, from_addr, from_name, body, message_id, time.time()))
    conn.commit()
    conn.close()


def get_deal_thread(deal_id):
    """All messages for a deal, oldest first."""
    conn = connect()
    rows = conn.execute("SELECT direction, from_name, body, created_at FROM deal_messages WHERE deal_id=? ORDER BY created_at ASC, id ASC",
                        (deal_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def last_inbound_message_id(deal_id):
    """The Message-ID of the most recent brand email — so an outbound reply threads under it."""
    conn = connect()
    row = conn.execute("SELECT message_id FROM deal_messages WHERE deal_id=? AND direction='in' AND message_id IS NOT NULL ORDER BY created_at DESC, id DESC LIMIT 1",
                       (deal_id,)).fetchone()
    conn.close()
    return row["message_id"] if row else None


def find_deal_by_message_ids(creator_id, ids):
    """Find the deal a brand reply belongs to: match any referenced Message-ID against
    this creator's known deal messages (the original email or any prior message). Returns
    the deal id, or None if it's a fresh thread."""
    ids = [i for i in (ids or []) if i]
    if not ids:
        return None
    conn = connect()
    marks = ",".join("?" * len(ids))
    row = conn.execute(
        f"""SELECT d.id FROM deals d
            WHERE d.creator_id=? AND (
              d.message_id IN ({marks})
              OR EXISTS (SELECT 1 FROM deal_messages m WHERE m.deal_id=d.id AND m.message_id IN ({marks}))
            ) LIMIT 1""",
        (creator_id, *ids, *ids),
    ).fetchone()
    conn.close()
    return row["id"] if row else None


def seen_deal_message_ids(creator_id):
    """Every Message-ID we've already stored for this creator (deals + thread messages),
    so a brand reply we've already ingested isn't threaded in twice."""
    conn = connect()
    rows = conn.execute(
        """SELECT message_id FROM deals WHERE creator_id=?
           UNION SELECT m.message_id FROM deal_messages m JOIN deals d ON d.id=m.deal_id WHERE d.creator_id=?""",
        (creator_id, creator_id)).fetchall()
    conn.close()
    return {r["message_id"] for r in rows if r["message_id"]}


def group_deals(group_id):
    """Lightweight deal+reply activity for a group, newest first — drives the admin's
    live 'new deal' / 'creator replied' notifications."""
    conn = connect()
    rows = conn.execute(
        """SELECT d.id, d.creator_id, c.name AS creator_name, d.brand, d.received_at,
                  d.status, d.reply_status, d.replied_at, d.replied_by
           FROM deals d JOIN creators c ON c.id = d.creator_id
           WHERE c.group_id = ? ORDER BY d.received_at DESC LIMIT 100""",
        (group_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def latest_trends_of_type(creator_id, unit_type):
    """Most recent trend rows of ONE unit_type (e.g. 'yt_topic'), ranked. Queried by that
    type's own latest scan_time so a free-lane scan and the IG scan stay independent."""
    conn = connect()
    last = conn.execute(
        "SELECT MAX(scan_time) AS mx FROM trend_snapshots WHERE creator_id=? AND unit_type=?",
        (creator_id, unit_type),
    ).fetchone()
    if not last or last["mx"] is None:
        conn.close()
        return []
    rows = conn.execute(
        "SELECT * FROM trend_snapshots WHERE creator_id=? AND unit_type=? AND scan_time=? ORDER BY score DESC",
        (creator_id, unit_type, last["mx"]),
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["examples"] = json.loads(d["examples"]) if d.get("examples") else []
        d["platforms"] = json.loads(d["platforms"]) if d.get("platforms") else []
        out.append(d)
    return out


def latest_radar(creator_id):
    """The trend list from the most recent IG/TikTok scan, ranked. EXCLUDES the free-lane trends
    (YouTube/Twitch — unit_type yt_video/tw_vod) because those run on their OWN scan_time and are
    served separately; otherwise a YouTube-only scan would hijack the 'latest scan' and hide the
    Instagram/TikTok feed."""
    conn = connect()
    FREE = ("yt_video", "tw_vod")
    ph = ",".join("?" * len(FREE))
    last = conn.execute(
        f"SELECT MAX(scan_time) AS mx FROM trend_snapshots WHERE creator_id=? AND unit_type NOT IN ({ph})",
        (creator_id, *FREE),
    ).fetchone()
    if not last or last["mx"] is None:
        conn.close()
        return None, []
    rows = conn.execute(
        f"SELECT * FROM trend_snapshots WHERE creator_id=? AND scan_time=? AND unit_type NOT IN ({ph}) ORDER BY score DESC",
        (creator_id, last["mx"], *FREE),
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["examples"] = json.loads(d["examples"]) if d.get("examples") else []
        d["platforms"] = json.loads(d["platforms"]) if d.get("platforms") else []
        out.append(d)
    return last["mx"], out
