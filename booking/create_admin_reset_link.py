#!/usr/bin/env python3
"""Create a private one-time password reset URL that expires in 15 minutes."""
import hashlib
import json
import secrets
import time
from pathlib import Path

TARGET = Path("/root/.secrets/owlsnest-admin-reset.json")

def main():
    token = secrets.token_urlsafe(32)
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.parent.chmod(0o700)
    TARGET.write_text(json.dumps({
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "expires": int(time.time()) + 15 * 60,
    }), encoding="utf-8")
    TARGET.chmod(0o600)
    print("https://owlsnestbar.com/booking/reset-password#" + token)

if __name__ == "__main__":
    main()
