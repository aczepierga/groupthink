"""Secrets and constants. The partner key is read in-process only."""
import os
import subprocess

BASE_URL = "https://api-core.livingsocial.com"
USER_AGENT = "GroupThink/0.1 (Groupon employee contest entry)"
KEYCHAIN_SERVICE = "groupthink-ls-partner"


def _keychain_read() -> str:
    """Read the key from the macOS Keychain; stdout is captured, never printed."""
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def partner_key() -> str:
    key = os.environ.get("LS_PARTNER_KEY", "").strip() or _keychain_read()
    if not key:
        raise RuntimeError("partner key missing")
    return key
