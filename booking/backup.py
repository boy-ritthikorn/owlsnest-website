#!/usr/bin/env python3
"""Create a consistent SQLite backup and keep the latest 30 copies."""
import sqlite3
from datetime import datetime
from pathlib import Path

SOURCE = Path("/root/owlsnest-data/bookings.sqlite3")
DEST = Path("/root/owlsnest-data/backups")
KEEP = 30

def main():
    if not SOURCE.exists():
        return
    DEST.mkdir(parents=True, exist_ok=True)
    DEST.chmod(0o700)
    target = DEST / ("bookings-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".sqlite3")
    with sqlite3.connect(SOURCE) as source, sqlite3.connect(target) as backup:
        source.backup(backup)
    target.chmod(0o600)
    for old in sorted(DEST.glob("bookings-*.sqlite3"))[:-KEEP]:
        old.unlink()

if __name__ == "__main__":
    main()
