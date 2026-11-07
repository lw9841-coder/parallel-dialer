#!/usr/bin/env python3
"""Pull session recordings off Twilio and transcribe them locally, for free.

    python scripts/transcribe.py --days 1

Twilio's own transcription is $0.05/min -- more than the calls themselves cost.
whisper.cpp on your Mac does the same job for nothing:

    brew install whisper-cpp
    curl -L -o ~/.whisper-base.en.bin \\
      https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin

Recordings are conference-length (the whole session), so transcripts come out
as one file per session, not per call.
"""

import argparse
import datetime
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from dialer import config  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "recordings"
MODEL = Path(os.path.expanduser("~/.whisper-base.en.bin"))


def whisper_bin():
    for name in ("whisper-cli", "whisper-cpp", "whisper"):
        found = shutil.which(name)
        if found:
            return found
    return None


def fetch(days):
    from twilio.rest import Client

    client = Client(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)
    after = datetime.datetime.utcnow() - datetime.timedelta(days=days)
    OUT.mkdir(exist_ok=True)

    saved = []
    for rec in client.recordings.list(date_created_after=after, limit=200):
        dest = OUT / f"{rec.sid}.mp3"
        if dest.exists():
            saved.append(dest)
            continue
        url = f"https://api.twilio.com/2010-04-01/Accounts/{rec.account_sid}/Recordings/{rec.sid}.mp3"
        resp = requests.get(
            url, auth=(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN), timeout=120
        )
        if resp.status_code >= 400:
            print(f"  skip {rec.sid}: HTTP {resp.status_code}")
            continue
        dest.write_bytes(resp.content)
        mins = (rec.duration or 0) / 60
        print(f"  saved {dest.name}  ({mins:.1f} min)")
        saved.append(dest)
    return saved


def transcribe(path, binary):
    txt = path.with_suffix(".txt")
    if txt.exists():
        return txt
    wav = path.with_suffix(".wav")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(path),
         "-ar", "16000", "-ac", "1", str(wav)],
        check=True,
    )
    subprocess.run(
        [binary, "-m", str(MODEL), "-f", str(wav), "-otxt", "-of", str(path.with_suffix(""))],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    wav.unlink(missing_ok=True)
    return txt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1, help="how far back to pull")
    ap.add_argument("--download-only", action="store_true")
    args = ap.parse_args()

    if not config.TWILIO_ACCOUNT_SID:
        sys.exit("Set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN in .env first.")

    print(f"Fetching recordings from the last {args.days} day(s)...")
    files = fetch(args.days)
    if not files:
        print("Nothing to do.")
        return
    if args.download_only:
        print(f"\n{len(files)} file(s) in {OUT}")
        return

    binary = whisper_bin()
    if not binary:
        sys.exit("\nwhisper.cpp not found. Install it:  brew install whisper-cpp")
    if not MODEL.exists():
        sys.exit(f"\nModel missing at {MODEL}. Download ggml-base.en.bin (see docstring).")
    if not shutil.which("ffmpeg"):
        sys.exit("\nffmpeg not found. Install it:  brew install ffmpeg")

    print(f"\nTranscribing with {binary}...")
    for f in files:
        try:
            print(f"  {transcribe(f, binary).name}")
        except subprocess.CalledProcessError as exc:
            print(f"  failed on {f.name}: {exc}")

    print(f"\nTranscripts in {OUT}")


if __name__ == "__main__":
    main()
