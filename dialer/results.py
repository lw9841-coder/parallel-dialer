"""Crash-safe outcome log.

Every terminal outcome is appended to a CSV the instant it happens, flushed on
write. If the laptop sleeps, the tunnel dies, or you Ctrl-C at call 90 of 150,
what you already worked through is on disk -- and `already_called()` lets the
next run skip it instead of re-dialing people you just called.
"""

import csv
import datetime
import threading
from pathlib import Path

from . import config

DIR = config.ROOT / "results"
HEADER = ["timestamp", "name", "organization", "phone", "outcome", "attempts", "note", "record_id"]

# Outcomes that mean "we genuinely reached the end of the road with this number."
# Anything else is a number still owed a call.
TERMINAL = {
    "connected", "interested", "follow-up", "not-interested",
    "wrong-number", "no-answer", "do-not-call", "voicemail", "failed",
}

_lock = threading.Lock()
_path = None


def start(label=None):
    """Open a results file for this session. Returns its path."""
    global _path
    DIR.mkdir(exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M")
    name = f"{stamp}_{label}.csv" if label else f"{stamp}.csv"
    with _lock:
        _path = DIR / name
        if not _path.exists():
            with _path.open("w", newline="", encoding="utf-8") as fh:
                csv.writer(fh).writerow(HEADER)
    return _path


def path():
    return _path


def record(lead, outcome, note=None, attempts=1):
    """Append one outcome. Called for automatic outcomes (voicemail, no answer)
    as well as the ones you click, so the file is a complete record of the list
    rather than just the calls you remembered to log."""
    if _path is None:
        start()
    row = [
        datetime.datetime.now().isoformat(timespec="seconds"),
        lead.get("name", ""),
        lead.get("organization", ""),
        lead.get("phone", ""),
        outcome or "",
        attempts,
        (note or "").replace("\n", " "),
        lead.get("id", ""),
    ]
    with _lock:
        with _path.open("a", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow(row)
            fh.flush()
    return row


def already_called(days=7):
    """Phone numbers with a terminal outcome in recent results files.

    Feed this to the queue loader so a resumed list picks up where it stopped.
    """
    if not DIR.exists():
        return set()
    cutoff = datetime.date.today() - datetime.timedelta(days=days)
    done = set()
    for f in DIR.glob("*.csv"):
        try:
            stamp = datetime.datetime.strptime(f.name[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if stamp < cutoff:
            continue
        try:
            with f.open(newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    if row.get("outcome") in TERMINAL and row.get("phone"):
                        done.add(row["phone"])
        except OSError:
            continue
    return done


def summary(path_=None):
    """Counts by outcome, for the end-of-session readout."""
    target = Path(path_) if path_ else _path
    if not target or not target.exists():
        return {}
    counts = {}
    with target.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            key = row.get("outcome") or "?"
            counts[key] = counts.get(key, 0) + 1
    return counts
