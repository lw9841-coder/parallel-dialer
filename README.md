# Parallel Dialer

Rings 3 numbers at once, connects you to the first person who picks up, and
hangs up the other two.

## Why

When you call down a list, most numbers don't answer. Dialing one at a time
means a lot of waiting on rings and voicemail. This tool rings a few numbers at
once, so you spend more of your time actually talking to people.

It's built for warm calls: people who know you or expect to hear from you.

## How it works

1. You start a session. The tool calls your phone once, and you stay on the
   line the whole time.
2. It rings the next 3 numbers on your list at the same time.
3. The first person to answer gets connected to you. The other two calls are
   hung up before anyone picks up.
4. Those two numbers go back in line, so nobody gets skipped.
5. You click an outcome (interested, follow up, and so on), and the next 3
   start ringing.

Every outcome is saved to a CSV file in `results/` the moment it happens, so a
dropped connection doesn't lose your progress.

```
  you ─────────────── call room (stays open all session)
                            ▲
   ── line 1 ringing ───────┤  first person to answer is connected
   ── line 2 ringing ──────    the others are hung up
   ── line 3 ringing ──────
```

## Setup

You need Python 3, a [Twilio](https://twilio.com) account with one voice
number, and [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)
so Twilio can reach your computer. `run.sh` is written for macOS.

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env`:

- `TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN` from your Twilio console
- `CALLER_ID`: the Twilio number your contacts will see
- `AGENT_PHONE`: your own phone

Then start it:

```bash
./run.sh
```

This opens the tunnel, saves its address in `.env`, and starts the app. Open
<http://localhost:5111>, load a contact list, click **Start session**, and
answer your phone.

## Your contact list

A CSV with these columns. Only `phone` is required.

```
name,phone,organization,status,notes
```

`sample_contacts.csv` shows the format with fake data. Put your own file path
in the box in the app, or set `CONTACTS_CSV` in `.env`. Test with your own
phone number first.

## Settings

Two profiles ship in `profiles.json`:

- **Warm calls**: 3 lines, waits for you to log each call before moving on.
- **List blitz**: 5 lines, shorter ring time, keeps moving on its own.

Give each profile its own caller ID. Calling a lot of numbers from the number
your contacts already know can get it flagged as spam.

Voicemail drop and call recording are off by default.

## Keyboard shortcuts

| Key | Action |
|-----|--------|
| `1`-`6` | Log the outcome (interested, follow up, not interested, wrong number, no answer, do not call) |
| `N` | Ring the next batch now |
| `V` | Mark as voicemail and move on |
| `H` | Hang up the current call, keep the session going |

## Tests

```bash
./venv/bin/python scripts/test_race.py      # only one line can ever win
./venv/bin/python scripts/test_webhooks.py  # Twilio callbacks are checked
```

## Calling laws

You are responsible for following the law where you and the people you call
live. That includes getting consent before you call, honoring do-not-call
lists and requests, and following call recording rules (some places require
everyone on the call to agree before you record). This tool does not check any
of that for you. If you're unsure, talk to a lawyer before you use it.

## Files

| File | What it does |
|---|---|
| `app.py` | Web server: the app's controls and Twilio callbacks |
| `dialer/session.py` | Decides who wins each batch and who goes back in line |
| `dialer/telephony.py` | Places and ends calls through Twilio |
| `dialer/contacts.py` | Reads your CSV and cleans up phone numbers |
| `dialer/results.py` | Saves every outcome to a CSV |
| `dialer/profiles.py` | The two calling profiles |
| `dialer/config.py` | Reads settings from `.env` |
| `scripts/transcribe.py` | Optional: downloads recordings and transcribes them on your computer |

## License

MIT
