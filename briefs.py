"""Producer Feed + Format Teardown.

Turns a detected trend into a shoot-ready brief for a SPECIFIC creator, in
their niche. Uses Claude (Anthropic API) when ANTHROPIC_API_KEY is set; falls
back to a templated brief so it still works offline.

The brief bundles two things the user asked for:
  - Producer Feed: the concrete "make THIS" move, hook, shot list, caption.
  - Format Teardown: why the trend works + the structure to copy.
"""

import json
import os

import db
import scoring

MODEL = os.environ.get("JSENT_BRIEF_MODEL") or os.environ.get("TRENDY_BRIEF_MODEL", "claude-opus-4-8")

BRIEF_SCHEMA = {
    "type": "object",
    "properties": {
        "the_move": {"type": "string", "description": "What to copy from the example video, recast for THIS creator — 1-2 sentences. Reference what the example post does that makes it work."},
        "hooks": {
            "type": "array",
            "description": "3-5 scroll-stopping openers for the first 1-2 seconds. For models/aesthetic content the hook is mostly VISUAL — pose drop on the beat, outfit/look reveal, walk-in, transition.",
            "items": {
                "type": "object",
                "properties": {
                    "visual": {"type": "string", "description": "The visual move in the first 1-2s. Specific and shootable."},
                    "text": {"type": "string", "description": "Short on-screen text overlay, max ~6 words (can be empty if the visual carries it)."},
                },
                "required": ["visual", "text"], "additionalProperties": False,
            },
        },
        "format": {"type": "array", "items": {"type": "string"}, "description": "3-5 beats / shot list to recreate the example."},
        "sound": {"type": "string", "description": "The sound/audio to use and where to hit the beat (the reveal/pose). Name the actual sound if known."},
        "captions": {
            "type": "array",
            "description": "3-5 short, vibey, engagement-baiting captions in different tones (confident, playful, mysterious, a question).",
            "items": {
                "type": "object",
                "properties": {
                    "vibe": {"type": "string", "description": "One-word tone, e.g. confident / playful / mysterious / question."},
                    "text": {"type": "string", "description": "The caption, short, with 2-4 fitting hashtags."},
                },
                "required": ["vibe", "text"], "additionalProperties": False,
            },
        },
        "why_it_works": {"type": "string", "description": "Teardown: why the example/trend is performing right now."},
        "post_by": {"type": "string", "description": "Timing guidance, e.g. 'next 48h'."},
        "look": {"type": "string", "description": "One line on the color/filter/vibe to match the trend."},
        "edit": {"type": "array", "items": {"type": "string"},
                 "description": "Step-by-step CapCut-style edit recipe to make their footage MATCH the example: audio sync, cut pacing, on-screen text timing, transitions, speed."},
    },
    "required": ["the_move", "hooks", "format", "sound", "captions", "why_it_works", "post_by", "look", "edit"],
    "additionalProperties": False,
}


def _trend_meta(creator_id, unit_key):
    """Find the latest snapshot for this unit + the creator's overperforming
    examples that use it."""
    _, trends = db.latest_radar(creator_id)
    trend = next((t for t in trends if t["unit_key"] == unit_key), None)

    videos = db.latest_video_stats(creator_id)
    baselines, niche_median = scoring.account_baselines(videos)
    examples = []
    for v in videos:
        tp = (v.get("topic") or "").lower()
        is_photo = v.get("platform") == "instagram_photo"
        match = (unit_key == f"audio:{v.get('audio_id')}") or \
                (tp and not is_photo and unit_key == f"topic:{tp}") or \
                (tp and is_photo and unit_key == f"photo:{tp}") or \
                any(unit_key == f"hashtag:{h.lower()}" for h in (v.get("hashtags") or []))
        if match:
            v["_op"] = scoring.overperformance(v, baselines, niche_median)
            examples.append(v)
    examples.sort(key=lambda v: v["_op"], reverse=True)
    return trend, examples[:5]


def _post_by(trend):
    if not trend:
        return "next 48h"
    if trend["status"] == "saturated":
        return "skip — already saturated"
    if trend["account_count"] >= 5:
        return "next 24h — spreading fast"
    return "next 48-72h — still early"


def _example_links(examples):
    """The overperforming videos using this trend — what to watch & recreate."""
    out = []
    for e in examples:
        if e.get("url"):
            out.append({
                "url": e["url"], "author": e.get("author") or "",
                "views": int(e.get("views", 0)),
                "caption": (e.get("caption") or "")[:60],
            })
    return out[:4]


def generate(creator_id, unit_key, force=False):
    """Return a brief dict. Cached unless force=True. Example links are always
    attached fresh (data, not the cached text)."""
    trend, examples = _trend_meta(creator_id, unit_key)
    # AI "concept" trends carry their own example posts (matched by Claude, not by unit_key)
    links = (trend.get("examples") if trend and trend.get("unit_type") == "concept" else None) \
        or _example_links(examples)

    if not force:
        cached = db.get_brief(creator_id, unit_key)
        if cached:
            b = json.loads(cached["brief_json"])
            b["examples"] = links
            return b

    creator = db.get_creator(creator_id)
    label = trend["unit_label"] if trend else unit_key
    if _llm_enabled():
        brief = _llm_brief(creator, trend, label, examples)
    else:
        brief = _template_brief(creator, trend, label, examples)

    db.save_brief(creator_id, unit_key, json.dumps(brief))
    brief["examples"] = links
    return brief


# ---------- Claude ----------

def _llm_enabled():
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


def _llm_brief(creator, trend, label, examples):
    import anthropic

    client = anthropic.Anthropic()
    ex_lines = "\n".join(
        f"- {e.get('author')}: {int(e.get('views',0)):,} views, "
        f"{e['_op']:.1f}x their baseline — \"{(e.get('caption') or '')[:80]}\""
        for e in examples
    ) or "(no example videos captured yet)"

    momentum = trend["momentum"] if trend else "rising"
    accounts = trend["account_count"] if trend else "several"
    gender = (creator.get("gender") or "").lower()
    who = {"man": "male model", "woman": "female model"}.get(gender, "model / aesthetic creator")
    top = examples[0] if examples else None
    hero = (f'@{top.get("author")} — {int(top.get("views",0)):,} views, "{(top.get("caption") or "")[:90]}"'
            if top else "(the top example post on our radar)")

    prompt = f"""You are the producer desk at a top creator agency that manages ATTRACTIVE \
MODELS and aesthetic creators on TikTok + Instagram Reels. This is visual-first "look at \
me" content — outfit reveals, GRWM, transitions, body/fitness aesthetic, modeling — NOT \
informational or educational video.

CREATOR: {creator['name']} — a {who} in the "{creator['niche']}" lane.

THE VIDEO TO RECREATE (the single best overperforming example): {hero}
DETECTED TREND: "{label}"
{('- What the trend actually is: ' + trend.get('description')) if trend and trend.get('description') else ''}
{('- Driving sound: "' + trend.get('sound') + '" — have them use THIS sound.') if trend and trend.get('sound') else ''}
- Momentum: {momentum}; working across {accounts} accounts in this lane.
- Other overperforming examples:
{ex_lines}

Write a brief that shows {creator['name']} how to RECREATE that video in their own lane. \
Be concrete and shootable today.
- 'the_move': what to copy from the example, recast for this creator.
- 'hooks': 3-5 VISUAL openers for the first 1-2s (pose drop on the beat, outfit/look reveal, \
walk-in, transition) each with a short on-screen text line.
- 'format': the shot list to recreate it.
- 'sound': the sound to use and where to hit the beat.
- 'captions': 3-5 short vibey engagement-baiting captions in different tones.
- 'why_it_works', 'look', and an 'edit' recipe (CapCut on a phone: audio sync, cut pacing, \
on-screen text timing, transitions, speed ramps, cover frame).

HARD RULE — keep everything PLATFORM-SAFE: suggestive and confident is fine, but nothing \
explicit, no nudity, no wording that would get the post suppressed or the account \
shadowbanned on TikTok/Instagram. Tasteful and aspirational, not crude."""

    resp = client.messages.create(
        model=MODEL,
        max_tokens=2500,
        thinking={"type": "adaptive"},
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": BRIEF_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )
    text = next(b.text for b in resp.content if b.type == "text")
    brief = json.loads(text)
    brief["source"] = "claude"
    return brief


# ---------- offline fallback ----------

def _template_brief(creator, trend, label, examples):
    niche = creator["niche"]
    gender = (creator.get("gender") or "").lower()
    aud = {"man": "men's", "woman": "women's"}.get(gender, "")
    lane = (f"{aud} {niche}" if aud else niche).strip()
    unit = trend["unit_type"] if trend else "audio"
    top = examples[0] if examples else None
    ref = (top.get("caption") or "").strip()[:80] if top else ""
    ref_line = (f'The post leading this is @{top.get("author")} — "{ref}". Recreate that beat-for-beat with your angle.'
                if top and ref else "Open one of the example posts below and mirror its structure.")

    kind_move = {
        "audio": f'Make a {lane} clip set to the "{label}" sound — match the cut to its beats.',
        "hashtag": f'Post a {lane} piece built for {label} — the angle that hashtag rewards right now.',
        "topic": f'Make a {lane} video on "{label}" — the topic over-indexing in your lane this week.',
        "photo": f'Shoot a {lane} "{label}" photo/carousel — the look that\'s landing right now.',
    }.get(unit, f'Make a {lane} post around "{label}".')

    on_beat = "as the sound drops" if unit == "audio" else "on the strongest frame"
    tag = niche.split(",")[0].strip().split()[0].lower() if niche else "fyp"
    return {
        "the_move": f"{kind_move} {ref_line}",
        "hooks": [
            {"visual": f"Start tight on a detail, then pull back to the full {lane} look {on_beat}.", "text": "wait for it"},
            {"visual": f"Outfit/look reveal: plain frame, then hard-cut to the styled {lane} look {on_beat}.", "text": "the glow up"},
            {"visual": f"Walk straight toward the camera and hit a pose {on_beat}.", "text": ""},
            {"visual": f"Quick 3-pose flip — switch pose on every beat of the {label} sound.", "text": "pick your favorite"},
        ],
        "format": [
            f"0-2s: hook frame — bold text + {'the sound drops' if unit=='audio' else 'the key visual'}",
            f"2-7s: the {lane} setup — the problem or promise",
            "7-15s: the turn — your specific take/result (this is what makes it yours)",
            "15-20s: payoff + a reason to rewatch or save",
        ],
        "sound": (f'Use the "{label}" sound and land your reveal/pose on its first big beat.'
                  if unit == "audio" else "Put a trending sound under it and land your reveal on the first beat."),
        "captions": [
            {"vibe": "confident", "text": f"knew the assignment 💁 #{tag} #fyp"},
            {"vibe": "playful", "text": f"warning: may cause a double take 👀 #{tag} #reels"},
            {"vibe": "mysterious", "text": f"some looks don't need a caption. #{tag} #aesthetic"},
            {"vibe": "question", "text": f"this fit or the last one? 👇 #{tag} #ootd"},
        ],
        "why_it_works": f"\"{label}\" is {trend['momentum'] if trend else 'rising'} and winning for "
                        f"{trend['account_count'] if trend else 'several'} different {lane} accounts right now"
                        + (f" — like @{top.get('author')} ({int(top.get('views',0)):,} views)." if top else "."),
        "post_by": _post_by(trend),
        "look": f"Match the {lane} examples below — same framing, pacing and color so it reads as part of the trend.",
        "edit": [
            (f'Drop in the "{label}" sound first and build the cut around its beats' if unit == "audio"
             else f'Lead with the strongest {lane} visual in the first frame'),
            "Hard-cut on the beat — keep clips short (~1-2s each)",
            "On-screen text at 0:00 with the hook, big and centered",
            "Quick zoom / whip transition at the turn or reveal",
            "Speed-ramp the payoff, then hold the last frame ~1s as the cover",
        ],
        "source": "template",
    }
