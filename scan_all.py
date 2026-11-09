"""Scan every creator once. Run by the launchd scan job (or by hand).

    python scan_all.py
"""

import os

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except Exception:
    pass

import db
import deals
import radar


def main():
    db.init_db()
    creators = db.list_creators()
    if not creators:
        print("no creators to scan")
        return
    # trends: scan each NICHE+gender ONCE (shared across every creator in that lane)
    for r in radar.scan_niches():
        print(f"[niche {r['niche']}/{r.get('gender') or 'any'}] videos={r.get('videos_seen')} "
              f"hits={r.get('hits')} trends={r.get('trends')}")
    # brand deals stay per-creator (each creator has their own inbox)
    for c in creators:
        try:
            d = deals.run_scan(c)
            print(f"[{c['name']}] deals: {d['new']} new ({d['source']})")
        except Exception as e:
            print(f"[{c['name']}] deals error: {e}")


if __name__ == "__main__":
    main()
