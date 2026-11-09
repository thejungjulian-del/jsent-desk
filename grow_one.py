"""Grow a single creator's watch-list. Run detached (systemd-run) by the web 'Find
creators' button so the expansion survives a gunicorn restart/deploy:

    python grow_one.py <creator_id>
"""

import os
import sys

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except Exception:
    pass

import db
import radar


def main():
    if len(sys.argv) < 2:
        print("usage: grow_one.py <creator_id>")
        return
    cid = int(sys.argv[1])
    db.init_db()
    r = radar.grow_for_creator(cid)        # expands the creator's niche watch-list
    print(f"[grow_one] creator {cid}: {r}")


if __name__ == "__main__":
    main()
