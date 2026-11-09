"""Scan a single creator. Run detached (systemd-run) by the web 'Scan now' button so
the scan survives a gunicorn restart/deploy:

    python scan_one.py <creator_id>
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
        print("usage: scan_one.py <creator_id>")
        return
    cid = int(sys.argv[1])
    db.init_db()
    r = radar.scan_for_creator(cid)        # scans the creator's shared niche hub(s)
    print(f"[scan_one] creator {cid}: {r}")


if __name__ == "__main__":
    main()
