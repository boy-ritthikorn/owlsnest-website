#!/usr/bin/env python3
"""Create an expiring, single-use admin password setup link."""

import os
import secrets
import time
from pathlib import Path


ENV_FILE = Path("/root/.secrets/owlsnest-messenger-assistant.env")
LINK_FILE = Path("/root/owlsnest-assistant-setup-link.txt")
BASE_URL = "https://newton-ritthikornkorjai.incomeinclick.in.th/owl-assistant/setup"


def main() -> None:
    token = secrets.token_urlsafe(32)
    expires = str(int(time.time()) + 3600)
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    values = {"SETUP_TOKEN": token, "SETUP_EXPIRES": expires}
    updated = []
    for line in lines:
        key = line.split("=", 1)[0] if "=" in line else ""
        if key in values:
            updated.append(f"{key}='{values.pop(key)}'")
        else:
            updated.append(line)
    updated.extend(f"{key}='{value}'" for key, value in values.items())
    temp = ENV_FILE.with_suffix(".tmp")
    temp.write_text("\n".join(updated) + "\n", encoding="utf-8")
    temp.chmod(0o600)
    os.replace(temp, ENV_FILE)
    LINK_FILE.write_text(f"{BASE_URL}?token={token}\n", encoding="utf-8")
    LINK_FILE.chmod(0o600)
    print("Created a one-time setup link valid for 60 minutes (value hidden)")


if __name__ == "__main__":
    main()
