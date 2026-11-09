# JS Entertainment Desk

A trend desk I built for a creator agency. It keeps watch lists of chosen creators, reads the latest 30 posts from each watched account on every scan, and ranks the posts and reels that are beating their own account's usual numbers in each niche. From there it writes shoot-ready briefs, suggests when to post, drafts captions and hashtags, and finds new creators to watch through real collaborations and tags, with a vision pass that drops brands, photographers and off-niche accounts. It also runs a brand-deal inbox that scores incoming offers and drafts replies. The live version runs on a Hetzner server behind Caddy and Cloudflare, and the scraper it uses costs about $0.30 per 1,000 posts.

## Run it

```
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:8782. With no keys it runs in demo mode on mock data. Set `DEALS_DEMO=1` to fill the deal inbox with sample emails. To use real data, copy `.env.example` to `.env` and fill in the keys you have (Apify for Instagram and TikTok, the YouTube and Twitch APIs, Anthropic for briefs). `deploy/setup.sh` sets it up on a fresh Ubuntu server with systemd and Caddy.

## Status

Live for the agency. The admin side manages real creators' accounts, so this public copy ships with no database, watch lists, inbox mail or keys. Everything you see locally is demo data.

Portfolio: https://thejungjulian-del.github.io/portfolio/
