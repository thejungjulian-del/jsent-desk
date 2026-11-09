"""Brand-deal replies: Yes / No / Negotiate.

The overall wording is a fixed bot template. For a negotiation, the creator can
change a few fields (asking price, timeline, notes); everything else stays
templated. Replies are sent from the shared deals inbox (Gmail SMTP) as a proper
"Re:" thread, with Reply-To set to the creator's own address so the brand's reply
loops back into JS Entertainment.
"""

import os
import smtplib
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid

import db
from sources import inbox


def _label_name(creator):
    g = db.get_group(creator.get("group_id")) if creator.get("group_id") else None
    return g["name"] if g else "JS Entertainment"


def negotiate_defaults(deal):
    return {
        "asking_price": (deal.get("offer_amount") or "").strip() or "a paid rate we can discuss",
        "timeline": "2–3 weeks from signing",
        "note": "",
    }


def build(creator, deal, kind, fields=None):
    """Return {to, subject, body, fields}. fields is the (editable) negotiation
    values actually used, so the UI can prefill them."""
    name = creator["name"]
    label = _label_name(creator)
    greeting = (deal.get("contact_name") or "").strip() or "there"
    used = {}

    if kind == "yes":
        body = (f"Hi {greeting},\n\n"
                f"Thank you for reaching out — I'd love to partner with you on this. "
                f"The offer and deliverables look like a great fit, and I'm happy to move forward.\n\n"
                f"Could you share the next steps (agreement, timing, and any creative guidelines)? "
                f"I'll get started as soon as everything's confirmed.\n\n"
                f"Looking forward to working together,\n{name}\n{label}")
    elif kind == "no":
        body = (f"Hi {greeting},\n\n"
                f"Thanks so much for thinking of me for this. After a careful look, it isn't the "
                f"right fit for me right now, so I'll have to pass — but I'd love to stay in touch for "
                f"future collaborations that line up more closely.\n\n"
                f"Wishing you a great campaign,\n{name}\n{label}")
    else:  # negotiate
        used = {**negotiate_defaults(deal),
                **{k: v for k, v in (fields or {}).items() if (v or "").strip()}}
        note_line = f"\n- Additional notes: {used['note']}" if used["note"].strip() else ""
        body = (f"Hi {greeting},\n\n"
                f"Thank you for the offer — I'm genuinely interested in working with you. "
                f"Before I confirm, I'd like to propose a couple of adjustments:\n\n"
                f"- Rate: {used['asking_price']}\n"
                f"- Timeline: {used['timeline']}{note_line}\n\n"
                f"If that works on your side, I'm ready to move forward and can begin right away. "
                f"Happy to jump on a quick call if that's easier.\n\n"
                f"Best,\n{name}\n{label}")

    subject = deal.get("subject") or "Your collaboration offer"
    if not subject.lower().startswith("re:"):
        subject = "Re: " + subject
    return {"to": deal.get("from_addr") or "", "subject": subject, "body": body, "fields": used}


def _dispatch(creator, deal, subject, body):
    """Send one outbound email in a deal's thread (Gmail SMTP), threaded under the
    brand's latest message. Returns the body. Raises on misconfig/failure. The caller
    records the message in the thread."""
    user = os.environ.get("DEAL_IMAP_USER", "").strip()
    pw = os.environ.get("DEAL_IMAP_PASSWORD", "").replace(" ", "")
    if not user or not pw:
        raise RuntimeError("email sending is not configured")
    to = deal.get("from_addr")
    if not to:
        raise RuntimeError("no brand address to reply to")
    if not subject.lower().startswith("re:"):
        subject = "Re: " + subject
    slug = creator.get("deal_slug") or db.deal_slug(creator["id"])
    # Brands must only ever see the branded domain. The From address is the creator's own
    # deal address unless DEAL_FROM overrides it with one shared sending identity.
    # ⚠️ Gmail SILENTLY REWRITES From back to the authenticated account unless that exact
    # address is a verified "Send mail as" alias — so this reads as working while still
    # leaking the Gmail. Verify with a real send (check the received From header), never
    # by reading the code.
    from_addr = os.environ.get("DEAL_FROM", "").strip() or inbox.address_for(slug)
    m = MIMEText(body)
    m["Subject"] = subject
    m["From"] = f'{creator["name"]} ({_label_name(creator)}) <{from_addr}>'
    m["To"] = to
    m["Reply-To"] = inbox.address_for(slug)
    m["Date"] = formatdate()
    m["Message-ID"] = make_msgid(domain=inbox.DEAL_DOMAIN)
    parent = db.last_inbound_message_id(deal.get("id")) or deal.get("message_id")
    if parent and not str(parent).startswith("demo-"):
        m["In-Reply-To"] = parent
        m["References"] = parent
    _smtp_send(user, pw, to, m, from_addr)
    return body


def send(creator, deal, kind, fields=None):
    """Send a templated Yes/No/Negotiate reply. Returns the sent body."""
    drafted = build(creator, deal, kind, fields)
    return _dispatch(creator, deal, drafted["subject"], drafted["body"])


def send_message(creator, deal, body):
    """Send a free-form follow-up message in an existing deal thread. Returns the body."""
    body = (body or "").strip()
    if not body:
        raise RuntimeError("empty message")
    return _dispatch(creator, deal, deal.get("subject") or "Your collaboration offer", body)


def _smtp_send(user, pw, to_addr, msg, from_addr=None):
    host = os.environ.get("DEAL_SMTP_HOST", "smtp.gmail.com")
    # Separate credentials for a dedicated sending provider (Resend/SES/Workspace). Falls
    # back to the Gmail sink's credentials, which is how this ran before.
    login = os.environ.get("DEAL_SMTP_USER", "").strip() or user
    secret = os.environ.get("DEAL_SMTP_PASSWORD", "").replace(" ", "") or pw
    # Envelope sender. Two very different cases:
    #  - Dedicated provider (Resend/SES): the domain is authenticated, so the envelope
    #    should match the branded From or SPF/DMARC alignment fails and replies go to spam.
    #    It must NOT fall back to `login` there — Resend's login is the literal string
    #    "resend", which is not an address.
    #  - Plain Gmail: Gmail REJECTS an envelope it hasn't verified
    #    ("Client does not have permission to send as this sender"), so it must stay the
    #    authenticated account. Gmail rewrites the visible From to match anyway.
    if os.environ.get("DEAL_SMTP_USER", "").strip():
        envelope = (from_addr or "").strip() or login
    else:
        envelope = user or login
    s = smtplib.SMTP(host, int(os.environ.get("DEAL_SMTP_PORT", "587")), timeout=30)
    try:
        s.starttls()
        s.login(login, secret)
        s.sendmail(envelope, [to_addr], msg.as_string())
    finally:
        s.quit()
