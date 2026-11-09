"""Where brand-deal emails come from.

Each creator has a unique address — e.g. mina@jsentertainment.org — that they put
in their bio / media kit. Brands email it. Cloudflare Email Routing (free) catches
everything @<DEAL_DOMAIN> and forwards it to ONE inbox you own; the original "To:"
header is preserved, so we read that single inbox and route each message back to
the right creator by the address it was sent to.

Set IMAP creds in .env to go live; otherwise demo_messages() returns believable
sample brand emails so the Deals desk works out of the box.

  DEAL_DOMAIN        = jsentertainment.org      <- new addresses are minted here
  DEAL_DOMAINS       = jsentertainment.org,old-domain.com    <- all domains still accepted
  DEAL_IMAP_HOST     = imap.gmail.com
  DEAL_IMAP_USER     = the-inbox-you-forward-to@example.com
  DEAL_IMAP_PASSWORD = a Gmail "app password" (not your normal password)

DEAL_DOMAIN vs DEAL_DOMAINS: a creator who already handed out mina@old-domain.com keeps
receiving on it. The matcher below skips any message it can't tie to a creator, so a
domain missing from DEAL_DOMAINS means that brand's email is silently dropped — no
error, no bounce, it just never appears on the Deals desk. Keep retired domains listed
here for as long as their addresses might still be in circulation.
"""

import email
import imaplib
import os
import re
import time
from email.header import decode_header
from email.utils import parseaddr

DEAL_DOMAIN = os.environ.get("DEAL_DOMAIN", "jsentertainment.org")

# Every domain whose mail we still accept. The primary is always included, so setting
# only DEAL_DOMAIN keeps working; DEAL_DOMAINS adds retired ones alongside it.
DEAL_DOMAINS = [d.strip().lower()
                for d in (os.environ.get("DEAL_DOMAINS") or "").split(",") if d.strip()]
if DEAL_DOMAIN.lower() not in DEAL_DOMAINS:
    DEAL_DOMAINS.insert(0, DEAL_DOMAIN.lower())

# The trailing lookahead must reject a longer domain that merely STARTS with ours:
# \b would happily match "mina@jsentertainment.org.evil.com", letting a spoofed
# recipient route a stranger's email onto a creator's deal desk.
_RECIP_RE = re.compile(
    r"([A-Za-z0-9._%+-]+)@(" + "|".join(re.escape(d) for d in DEAL_DOMAINS) + r")(?![\w.-])",
    re.I)


def enabled():
    return all(os.environ.get(k, "").strip()
               for k in ("DEAL_IMAP_HOST", "DEAL_IMAP_USER", "DEAL_IMAP_PASSWORD"))


def address_for(slug):
    return f"{slug}@{DEAL_DOMAIN}"


# ---------- demo ----------

# Templates span the real spread a creator sees: a strong aligned paid deal, big
# money that's off-niche, gifting-only, an affiliate ask, and a flaggy one. {n} is
# the creator's niche, {w} its first word — so a fitness creator gets a protein brand.
_DEMO = [
    {"brand": "Forma Labs", "from": "partnerships@formalabs.example", "name": "Jenna Course",
     "subject": "Paid collab — {n}",
     "body": "Hi! We love your {n} content and would love to work together. "
             "We're offering $2,500 for one Reel + 3 stories featuring our product. "
             "Flexible on timing over the next 3 weeks, and you keep full creative control.\n\n"
             "Best,\nJenna\nPartnerships, Forma Labs"},
    {"brand": "Vantyr Watches", "from": "marcus@vantyr.example", "name": "Marcus Vela",
     "subject": "Brand partnership opportunity",
     "body": "We'd like to offer $4,000 for a single dedicated post. Our brand is "
             "luxury watches. Open to your ideas on how to present it to your audience.\n\n"
             "Thanks,\nMarcus"},
    {"brand": "Brightleaf", "from": "hello@brightleaf.example", "name": "Priya Anand",
     "subject": "Gifting collaboration",
     "body": "We'd love to send you our products for free in exchange for a post. "
             "No paid budget right now, but great exposure to our community!\n\n"
             "Cheers,\nPriya"},
    {"brand": "Kinetic", "from": "devon@kinetic.example", "name": "Devon Park",
     "subject": "Affiliate program for {w} creators",
     "body": "Join our affiliate program — earn 15% commission on every sale from your "
             "promo code. No upfront fee. Great fit for {n} audiences.\n\n"
             "Regards,\nDevon"},
    {"brand": "PulsePromo", "from": "deals@pulsepromo.example", "name": "PulsePromo Team",
     "subject": "URGENT: post needed in 24 hours",
     "body": "We need a post live within 24 hours. $150 plus we require perpetual usage "
             "rights to run your content as paid ads, and 6-month exclusivity in your category."},
]


def demo_messages(creator):
    """Sample brand emails for a creator, with stable synthetic Message-IDs so
    re-checking doesn't pile up duplicates."""
    niche = creator.get("niche") or "your"
    word = (re.findall(r"[A-Za-z]+", niche) or [niche])[0]
    slug = creator.get("deal_slug") or "creator"
    out = []
    for i, t in enumerate(_DEMO):
        out.append({
            "creator_id": creator["id"],
            "message_id": f"demo-{creator['id']}-{i}",
            "from_addr": t["from"],
            "from_name": t["name"],
            "brand": t["brand"],
            "subject": t["subject"].format(n=niche, w=word),
            "body": t["body"].format(n=niche, w=word),
            "received_at": time.time() - i * 7200,
            "to_addr": address_for(slug),
        })
    return out


# ---------- live (IMAP) ----------

def _decode(s):
    if not s:
        return ""
    parts = decode_header(s)
    return "".join(p.decode(enc or "utf-8", "replace") if isinstance(p, bytes) else p
                   for p, enc in parts)


def _body(msg):
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition")):
                try:
                    return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
                except Exception:
                    continue
        return ""
    try:
        return msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", "replace")
    except Exception:
        return ""


def fetch_real(creators, limit=50):
    """Read unseen mail from the shared inbox and route each message to a creator
    by the slug it was addressed to (the part before @ / before any +)."""
    by_slug = {c["deal_slug"]: c for c in creators if c.get("deal_slug")}
    M = imaplib.IMAP4_SSL(os.environ["DEAL_IMAP_HOST"].strip())
    M.login(os.environ["DEAL_IMAP_USER"].strip(), os.environ["DEAL_IMAP_PASSWORD"].strip())
    M.select("INBOX")
    _, data = M.search(None, "ALL")
    ids = data[0].split()[-limit:]
    out = []
    for num in ids:
        _, raw = M.fetch(num, "(RFC822)")
        if not raw or not raw[0]:
            continue
        msg = email.message_from_bytes(raw[0][1])
        # which creator? look at To / Delivered-To / envelope recipients for any of our
        # accepted domains — X-Forwarded-To included, since Cloudflare Email Routing puts
        # the original recipient there and some forwards rewrite To: entirely.
        recips = " ".join(filter(None, [msg.get("To"), msg.get("Delivered-To"),
                                        msg.get("X-Forwarded-To"), msg.get("Cc")]))
        m = _RECIP_RE.search(recips)
        if not m:
            continue
        slug = m.group(1).split("+")[0].lower()
        creator = by_slug.get(slug)
        if not creator:
            continue
        from_name, from_addr = parseaddr(msg.get("From", ""))
        out.append({
            "creator_id": creator["id"],
            "message_id": msg.get("Message-ID") or f"{num.decode()}-{from_addr}",
            "in_reply_to": msg.get("In-Reply-To") or "",
            "references": msg.get("References") or "",
            "from_addr": from_addr,
            "from_name": _decode(from_name),
            "brand": _decode(from_name) or (from_addr.split("@")[-1] if from_addr else ""),
            "subject": _decode(msg.get("Subject")),
            "body": _body(msg)[:4000],
            "received_at": time.time(),
            "to_addr": m.group(0),
        })
    M.logout()
    return out
