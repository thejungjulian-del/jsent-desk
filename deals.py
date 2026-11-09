"""Brand-deal desk: read incoming partnership emails, score each one for a
specific creator, and rank them.

Two scores per deal:
  - value_score (0-100): how good the OFFER is — real pay beats gifting, low
    workload and fair terms beat heavy asks and rights grabs.
  - fit_score   (0-100): how ALIGNED the brand is with this creator's niche.

Uses Claude (ANTHROPIC_API_KEY) for extraction + scoring when available; falls
back to a transparent keyword heuristic so the desk works offline / in demo.
"""

import os
import re
import threading
import time

import db
from sources import inbox

_SCAN_LOCK = threading.Lock()

MODEL = os.environ.get("JSENT_BRIEF_MODEL") or os.environ.get("TRENDY_BRIEF_MODEL", "claude-opus-4-8")

DEAL_SCHEMA = {
    "type": "object",
    "properties": {
        "brand": {"type": "string", "description": "The company/brand reaching out."},
        "contact_name": {"type": "string", "description": "First name of the person who sent the email (from the signature or the From line). '' only if truly none."},
        "offer_type": {"type": "string", "enum": ["paid", "gifting", "affiliate", "unclear"]},
        "offer_amount": {"type": "string", "description": "Pay as stated, e.g. '$2,000'. '' if none."},
        "deliverables": {"type": "string", "description": "What the brand wants made/posted."},
        "deadline": {"type": "string", "description": "Any timing/deadline mentioned, else ''."},
        "value_score": {"type": "integer", "description": "0-100: how good a deal for the creator (real pay, low workload, fair terms = high)."},
        "fit_score": {"type": "integer", "description": "0-100: alignment with this creator's niche/audience."},
        "flags": {"type": "array", "items": {"type": "string"},
                  "description": "Short red-flag warnings, e.g. 'gifting only, no pay', 'perpetual usage rights', 'tight 24h deadline'. Empty if clean."},
        "summary": {"type": "string", "description": "One-sentence recommendation to the manager."},
    },
    "required": ["brand", "contact_name", "offer_type", "offer_amount", "deliverables", "deadline",
                 "value_score", "fit_score", "flags", "summary"],
    "additionalProperties": False,
}


def _demo_deals():
    """Whether to show fake sample brand deals when no real inbox is connected. Off by
    default — set DEALS_DEMO=1 to see placeholder deals (e.g. for a demo)."""
    return os.environ.get("DEALS_DEMO", "").strip().lower() in ("1", "true", "yes", "on")


_MSGID_RE = re.compile(r"<[^>]+>")


def _refs(m):
    """Every Message-ID a fetched email points back to (In-Reply-To + References)."""
    return _MSGID_RE.findall((m.get("in_reply_to") or "") + " " + (m.get("references") or ""))


def _ingest(creator, m):
    """Store one fetched email. If it's a brand's reply to a deal we already have,
    thread it onto that deal; otherwise score it and open a new deal. Returns
    'new' | 'reply' | None (already ingested)."""
    cid = creator["id"]
    if m["message_id"] in db.seen_deal_message_ids(cid):
        return None
    parent = db.find_deal_by_message_ids(cid, _refs(m))
    if parent:
        db.add_deal_message(parent, "in", m.get("body"), m.get("from_addr"),
                            m.get("from_name") or m.get("brand"), m.get("message_id"))
        return "reply"
    m.update(analyze(creator, m))
    return "new" if db.add_deal(m) else None      # add_deal seeds the opening thread message


def run_scan(creator):
    """Pull new deal emails for one creator, score/thread them, store the new ones.
    Returns {checked, new, replies, source}."""
    db.deal_slug(creator["id"])  # ensure the creator has a mailbox slug
    creator = db.get_creator(creator["id"])
    if inbox.enabled():
        msgs = [m for m in inbox.fetch_real([creator]) if m["creator_id"] == creator["id"]]
        source = "imap"
    else:
        # no real inbox connected → only show fake sample deals if DEALS_DEMO is on
        msgs = inbox.demo_messages(creator) if _demo_deals() else []
        source = "demo"
    new = replies = 0
    for m in msgs:
        r = _ingest(creator, m)
        if r == "new":
            new += 1
        elif r == "reply":
            replies += 1
    return {"checked": len(msgs), "new": new, "replies": replies, "source": source}


def scan_inbox():
    """Read the inbox ONCE and store new deals for every creator. Returns count of new deals."""
    creators = db.list_creators()
    for c in creators:
        db.deal_slug(c["id"])           # ensure each creator has a mailbox slug
    creators = db.list_creators()
    if inbox.enabled():
        msgs = inbox.fetch_real(creators)
    elif _demo_deals():
        msgs = [m for c in creators for m in inbox.demo_messages(c)]
    else:
        msgs = []
    by_id, new = {c["id"]: c for c in creators}, 0
    for m in msgs:
        creator = by_id.get(m["creator_id"])
        if not creator:
            continue
        if _ingest(creator, m) == "new":
            new += 1
    return new


def maybe_scan(min_interval=120):
    """Throttled, non-blocking inbox refresh. Called on page loads so new deals
    surface on their own; actually hits the inbox at most once per min_interval."""
    try:
        last = float(db.get_setting("deals_last_scan", 0) or 0)
    except (TypeError, ValueError):
        last = 0
    if time.time() - last < min_interval:
        return
    db.set_setting("deals_last_scan", str(time.time()))   # claim the slot up front
    threading.Thread(target=_scan_safe, daemon=True).start()


def _scan_safe():
    if not _SCAN_LOCK.acquire(blocking=False):
        return
    try:
        scan_inbox()
    except Exception:
        pass
    finally:
        _SCAN_LOCK.release()


def analyze(creator, email):
    if _llm_enabled():
        try:
            return _llm_analyze(creator, email)
        except Exception:
            pass
    return _heuristic(creator, email)


# ---------- Claude ----------

def _llm_enabled():
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


def _llm_analyze(creator, email):
    import anthropic

    client = anthropic.Anthropic()
    prompt = f"""You are the brand-partnerships manager at a creator agency. A brand has \
emailed one of our creators. Extract the offer and score it FOR THIS CREATOR.

CREATOR: {creator['name']}
NICHE: {creator['niche']}

EMAIL
From: {email.get('from_addr', '')}
Subject: {email.get('subject', '')}
Body:
{email.get('body', '')}

Score value_score on how good the deal is (real cash > affiliate > gifting > vague; \
lighter deliverables and fair terms score higher; rights grabs / exclusivity / pressure \
lower it). Score fit_score on how well the brand matches this creator's niche and audience. \
Flag anything the manager should be wary of. Keep summary to one sentence."""

    resp = client.messages.create(
        model=MODEL,
        max_tokens=1200,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": DEAL_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )
    import json
    text = next(b.text for b in resp.content if b.type == "text")
    d = json.loads(text)
    d["value_score"] = max(0, min(100, int(d["value_score"])))
    d["fit_score"] = max(0, min(100, int(d["fit_score"])))
    return d


# ---------- offline / demo heuristic ----------

_GIFT = ("gift", "free product", "in exchange for", "complimentary", "sample",
         "no budget", "unpaid", "exposure")
_AFFIL = ("affiliate", "commission", "% of sales", "percent of sales", "promo code", "referral")
_FLAGS = [
    (r"perpetu|in perpetuity|forever", "wants perpetual usage rights"),
    (r"exclusiv", "demands category exclusivity"),
    (r"whitelist|paid ad|spark ad|usage rights", "wants to run your content as paid ads"),
    (r"24 ?hours|today|asap|urgent|immediately", "high-pressure deadline"),
    (r"no budget|unpaid|gift(ed)? only|exposure", "little or no pay"),
]


_GENERIC_NAMES = {"team", "partnerships", "partnership", "marketing", "info", "hello", "hi",
                  "sales", "brand", "brands", "collabs", "collab", "influencer", "influencers",
                  "pr", "press", "noreply", "no-reply", "contact", "support", "hey", "admin",
                  "outreach", "creators", "social", "there", "company", "deals", "deal"}


def _contact_name(email):
    """Best-effort first name of the human who emailed: signature first, then the
    From display name, then a personal-looking local-part. '' if nothing human."""
    body = email.get("body", "") or ""
    m = re.search(
        r"(?i:best|thanks|thank you|cheers|regards|sincerely|warmly|best regards|"
        r"all the best|talk soon|kind regards|warm regards)[,!.\s]*\n?\s*([A-Z][a-z]+)", body)
    if m and m.group(1).lower() not in _GENERIC_NAMES:
        return m.group(1)
    fn = (email.get("from_name") or "").strip().strip('"')
    if fn:
        first = re.split(r"[\s,]+", fn)[0]
        if re.fullmatch(r"[A-Za-z][a-z]+", first) and first.lower() not in _GENERIC_NAMES:
            return first.capitalize()
    cand = re.split(r"[._]", (email.get("from_addr", "").split("@")[0] or ""))[0]
    if re.fullmatch(r"[a-z]{3,}", cand) and cand not in _GENERIC_NAMES:
        return cand.capitalize()
    return ""


def _amount(text):
    m = re.search(r"\$\s?([\d,]+)\s?(k\b)?", text, re.I)
    if not m:
        return None, ""
    num = int(m.group(1).replace(",", ""))
    if m.group(2):
        num *= 1000
    return num, m.group(0).strip()


def _heuristic(creator, email):
    text = f"{email.get('subject', '')} {email.get('body', '')}"
    low = text.lower()
    num, amount = _amount(text)
    if num:
        offer_type = "paid"
    elif any(w in low for w in _AFFIL):
        offer_type = "affiliate"
    elif any(w in low for w in _GIFT):
        offer_type = "gifting"
    else:
        offer_type = "unclear"

    flags = [msg for pat, msg in _FLAGS if re.search(pat, low)]

    if num:
        value = min(100, 35 + num // 50)        # ~$3,250 -> ~100
    elif offer_type == "affiliate":
        value = 38
    elif offer_type == "gifting":
        value = 20
    else:
        value = 28
    value = max(5, value - 12 * len(flags))

    niche_words = {w for w in re.findall(r"[a-z]+", (creator.get("niche") or "").lower()) if len(w) > 2}
    hits = sum(1 for w in niche_words if w in low)
    fit = 50 if not niche_words else max(20, min(100, 35 + hits * 30))

    brand = email.get("brand") or (email.get("from_addr", "").split("@")[-1] or "Unknown brand")
    deliv = ""
    for sent in re.split(r"(?<=[.!?])\s+", email.get("body", "")):
        if re.search(r"\b(post|reel|video|story|stories|content|feature)\b", sent, re.I):
            deliv = sent.strip()[:160]
            break

    overall = (value + fit) // 2
    if flags and value < 35:
        summary = f"Be careful — {flags[0]}."
    elif overall >= 70:
        summary = "Strong fit and a real offer — worth saying yes."
    elif fit < 40:
        summary = "Decent offer but off-niche for this creator."
    elif offer_type == "gifting":
        summary = "Gifting only — fine if they like the product, but no pay."
    else:
        summary = "Middling — negotiate before committing."

    return {"brand": brand, "contact_name": _contact_name(email), "offer_type": offer_type,
            "offer_amount": amount, "deliverables": deliv, "deadline": "",
            "value_score": value, "fit_score": fit, "flags": flags, "summary": summary}
