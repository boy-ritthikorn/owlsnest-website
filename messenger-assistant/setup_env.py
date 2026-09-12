#!/usr/bin/env python3
"""Create the runtime secret file without printing secret values."""

import secrets
import sys
from pathlib import Path


SOURCE = Path("/root/.secrets/meta_long_lived.env")
TARGET = Path("/root/.secrets/owlsnest-messenger-assistant.env")
CREDENTIAL_NOTE = Path("/root/owlsnest-assistant-admin.txt")


def read_env(path: Path) -> dict[str, str]:
    values = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def main() -> None:
    source = read_env(SOURCE)
    old = read_env(TARGET) if TARGET.exists() else {}
    admin_password = old.get("ADMIN_PASSWORD") or secrets.token_urlsafe(18)
    values = {
        "HOST": "127.0.0.1",
        "PORT": "5606",
        "ADMIN_USER": "owladmin",
        "ADMIN_PASSWORD": admin_password,
        "CSRF_TOKEN": old.get("CSRF_TOKEN") or secrets.token_urlsafe(32),
        "META_APP_ID": source["META_APP_ID"],
        "META_APP_SECRET": source["META_APP_SECRET"],
        "META_PAGE_ID": "1195935530265019",
        "META_PAGE_TOKEN": source["META_PAGE_TOKEN_OWLS_NEST_1195935530265019"],
        "META_VERIFY_TOKEN": (
            secrets.token_urlsafe(32)
            if "--rotate-verify" in sys.argv
            else old.get("META_VERIFY_TOKEN") or secrets.token_urlsafe(32)
        ),
        "OPENAI_API_KEY": old.get("OPENAI_API_KEY", ""),
        "OPENAI_MODEL": old.get("OPENAI_MODEL", "gpt-5.6-luna"),
    }
    TARGET.write_text("".join(f"{key}='{value}'\n" for key, value in values.items()), encoding="utf-8")
    TARGET.chmod(0o600)
    CREDENTIAL_NOTE.write_text(
        "OWL'S NEST Messenger Assistant\n"
        "URL: https://newton-ritthikornkorjai.incomeinclick.in.th/owl-assistant/\n"
        f"Username: owladmin\nPassword: {admin_password}\n",
        encoding="utf-8",
    )
    CREDENTIAL_NOTE.chmod(0o600)
    print(f"Created {TARGET} and admin credential note (values hidden)")


if __name__ == "__main__":
    main()
