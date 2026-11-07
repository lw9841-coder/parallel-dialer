"""Configuration, loaded from the environment (.env is read at startup)."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv():
    """Minimal .env reader so we don't need python-dotenv."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv()


def _bool(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# --- Twilio -----------------------------------------------------------------
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")

# The number leads see on caller ID. Keep this ONE consistent local number.
CALLER_ID = os.environ.get("CALLER_ID", "")

# Your cell. The dialer calls you once at session start and you stay on the line.
AGENT_PHONE = os.environ.get("AGENT_PHONE", "")

# Public HTTPS base URL of this server (ngrok/cloudflared tunnel), no trailing slash.
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")

# --- Dialing behavior -------------------------------------------------------
LINES_PER_BATCH = _int("LINES_PER_BATCH", 3)

# Seconds to let a line ring before giving up. Keep this under the carrier's
# voicemail rollover (usually ~25s) if you'd rather not hit voicemail at all.
RING_TIMEOUT = _int("RING_TIMEOUT", 22)

# Answering machine detection. "Enable" resolves in ~3-4s (fast yank, greeting
# still playing). "DetectMessageEnd" waits for the beep -- more accurate for
# voicemail drop, but you sit through the whole greeting. See README.
MACHINE_DETECTION = os.environ.get("MACHINE_DETECTION", "Enable")
AMD_SPEECH_THRESHOLD = _int("AMD_SPEECH_THRESHOLD", 2400)
AMD_TIMEOUT = _int("AMD_TIMEOUT", 15)

# Pause before playing the voicemail drop, to clear the tail of the greeting.
VOICEMAIL_PAUSE = _int("VOICEMAIL_PAUSE", 3)

# Publicly reachable mp3/wav of your pre-recorded voicemail. Blank = no drop
# (machines just get hung up on and marked).
VOICEMAIL_AUDIO_URL = os.environ.get("VOICEMAIL_AUDIO_URL", "")

# Auto-fire the next batch once a batch dies with nobody answering.
AUTO_ADVANCE = _bool("AUTO_ADVANCE", True)

# Seconds of wrap-up after a call ends before the next batch fires on its own.
# 0 means wait for you to click a disposition.
WRAPUP_SECONDS = _int("WRAPUP_SECONDS", 0)

RECORD_CALLS = _bool("RECORD_CALLS", False)

# --- Contacts ---------------------------------------------------------------
# Default CSV to load when the UI doesn't give a path.
CONTACTS_CSV = os.environ.get("CONTACTS_CSV", str(ROOT / "sample_contacts.csv"))


# --- Server -----------------------------------------------------------------
PORT = _int("PORT", 5111)
VALIDATE_TWILIO_SIGNATURES = _bool("VALIDATE_TWILIO_SIGNATURES", True)


def missing_required():
    """Returns a list of config keys that must be set before dialing works."""
    required = {
        "TWILIO_ACCOUNT_SID": TWILIO_ACCOUNT_SID,
        "TWILIO_AUTH_TOKEN": TWILIO_AUTH_TOKEN,
        "CALLER_ID": CALLER_ID,
        "AGENT_PHONE": AGENT_PHONE,
        "PUBLIC_URL": PUBLIC_URL,
    }
    return [k for k, v in required.items() if not v]
