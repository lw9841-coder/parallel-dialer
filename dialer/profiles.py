"""Dialing profiles.

Two jobs, opposite settings:

  warm    Conversations are the point. Your own caller ID so they recognize
          you, fast AMD so a live human never hears dead air, fewer lines so
          two people almost never answer at once.

  blitz   Working through a long list. More lines, shorter ring, and it
          keeps moving on its own after each call.

Edit profiles.json to change these. Caller IDs must differ -- see check().
"""

import json
import threading

from . import config

DEFAULTS = {
    "warm": {
        "label": "Warm calls",
        "caller_id": "",           # your own cell, verified in Twilio
        "lines_per_batch": 3,
        "ring_timeout": 25,
        "machine_detection": "Enable",
        "voicemail_pause": 3,
        # Off by default. To turn it on: record the file into static/ and
        # set this to "/static/vm-warm.mp3".
        "voicemail_audio_url": "",
        # Off by default. Check your recording-consent laws before enabling.
        "record": False,
        "auto_advance": True,
        "wrapup_seconds": 0,       # wait for you to log it
    },
    "blitz": {
        "label": "List blitz",
        "caller_id": "",           # a Twilio number. NEVER your cell.
        "lines_per_batch": 5,
        "ring_timeout": 20,
        # No voicemail drop by default. That allows fast detection: ~3-4s to identify a machine and pull off it
        # instead of ~20s waiting for the beep. Machines get hung up on and
        # logged. Every dialed number still records as an attempt.
        "machine_detection": "Enable",
        "voicemail_pause": 1,
        "voicemail_audio_url": "",
        "record": False,
        "auto_advance": True,
        "wrapup_seconds": 4,       # keep moving without you
    },
}

PATH = config.ROOT / "profiles.json"

_lock = threading.Lock()
_active = "warm"


def _load():
    if PATH.exists():
        try:
            saved = json.loads(PATH.read_text())
        except (json.JSONDecodeError, OSError):
            return dict(DEFAULTS)
        merged = {}
        for name, base in DEFAULTS.items():
            merged[name] = {**base, **saved.get(name, {})}
        for name, extra in saved.items():
            if name not in merged:
                merged[name] = {**DEFAULTS["warm"], **extra}
        return merged
    return dict(DEFAULTS)


ALL = _load()


def write_defaults():
    """Create profiles.json from DEFAULTS if it isn't there yet."""
    if not PATH.exists():
        PATH.write_text(json.dumps(DEFAULTS, indent=2) + "\n")
        return True
    return False


def names():
    return list(ALL.keys())


def active_name():
    return _active


def set_active(name):
    global _active
    if name not in ALL:
        raise KeyError(name)
    with _lock:
        _active = name
    return ALL[name]


def active():
    return ALL[_active]


def get(key, fallback=None):
    """Profile value, falling back to the .env-level default."""
    value = active().get(key)
    if value in (None, ""):
        return fallback
    return value


def caller_id():
    return get("caller_id", config.CALLER_ID)


def check():
    """Config problems worth shouting about before a session starts.

    The big one: reusing your personal cell for the blitz profile. High volume
    into a low-answer list is exactly the pattern carriers score as spam. Burn
    a Twilio number doing that if you like -- but if you burn the number your
    contacts have saved in their phones, your relationship calls stop landing
    and you won't know why.
    """
    problems = []
    ids = {}
    for name, prof in ALL.items():
        cid = prof.get("caller_id") or config.CALLER_ID
        if not cid:
            problems.append(f"profile '{name}' has no caller_id")
        elif cid in ids:
            problems.append(
                f"profiles '{ids[cid]}' and '{name}' share caller ID {cid} -- "
                "give the blitz profile its own Twilio number"
            )
        else:
            ids[cid] = name

        # Beep detection only earns its latency if there's a message to drop.
        if (prof.get("machine_detection") == "DetectMessageEnd"
                and not (prof.get("voicemail_audio_url") or config.VOICEMAIL_AUDIO_URL)):
            problems.append(
                f"profile '{name}' waits for the voicemail beep but has no "
                "message to drop -- switch machine_detection to Enable and "
                "stop sitting through greetings"
            )

        # A drop URL pointing at a file we don't serve produces TwiML that
        # tries to <Play> a 404. Silent in testing, audible to the person on
        # the other end.
        url = prof.get("voicemail_audio_url") or ""
        if url.startswith("/") and not (config.ROOT / url.lstrip("/")).exists():
            problems.append(
                f"profile '{name}' drops {url} but that file isn't there -- "
                "record it into static/ or clear voicemail_audio_url"
            )
    return problems
