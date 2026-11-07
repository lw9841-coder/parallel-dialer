#!/usr/bin/env python3
"""Exercise the Twilio webhook routes with real signatures.

The single most likely first-run failure is signature validation: we rebuild the
signed URL as PUBLIC_URL + request.full_path, and if that doesn't match what we
handed Twilio byte-for-byte, every callback 403s and the dialer looks dead. This
computes genuine Twilio signatures and drives the real Flask routes.

    python scripts/test_webhooks.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PUBLIC = "https://test-tunnel.trycloudflare.com"
TOKEN = "test_auth_token_0123456789abcdef"

os.environ.update({
    "TWILIO_ACCOUNT_SID": "ACtest0000000000000000000000000000",
    "TWILIO_AUTH_TOKEN": TOKEN,
    "CALLER_ID": "+1-555-555-0198",
    "AGENT_PHONE": "+1-555-555-0199",
    "PUBLIC_URL": PUBLIC,
    "VALIDATE_TWILIO_SIGNATURES": "true",
})

from twilio.request_validator import RequestValidator  # noqa: E402

import app as APP  # noqa: E402
from dialer import telephony  # noqa: E402

validator = RequestValidator(TOKEN)

# Never let a test reach the network.
telephony.hangup_all = lambda sids: None
telephony.hangup = lambda sid: None
telephony.redirect = lambda sid, path, **kw: True
telephony.dial_lead = lambda lead, batch, room: (_ for _ in ()).throw(
    RuntimeError("dial_lead must not run in tests"))

client = APP.app.test_client()
LEADS = [{"id": f"rec{i}", "phone": f"+1555555019{i}", "name": f"Sample Contact {i}"} for i in range(3)]


def post(path, params, sign=True, bad=False):
    """POST exactly as Twilio would, signing the full URL including query."""
    url = PUBLIC + path
    sig = validator.compute_signature(url, params)
    headers = {}
    if sign:
        headers["X-Twilio-Signature"] = "deadbeef" if bad else sig
    return client.post(path, data=params, headers=headers)


def fresh_session():
    APP.session.reset()
    APP.session.start(LEADS, "dialer-test")
    APP.session.agent_connected()
    bid, batch = APP.session.open_batch()
    for i, lead in enumerate(batch):
        APP.session.register_leg(bid, lead["id"], f"CA{i}")
    return bid, batch


def test_good_signature_accepted():
    fresh_session()
    r = post("/twilio/lead-answer?batch=1&lead=rec0&room=dialer-test",
             {"CallSid": "CA0", "CallStatus": "in-progress"})
    assert r.status_code == 200, f"signed webhook rejected: {r.status_code} {r.data[:200]}"


def test_bad_signature_rejected():
    fresh_session()
    r = post("/twilio/lead-answer?batch=1&lead=rec0&room=dialer-test",
             {"CallSid": "CA0"}, bad=True)
    assert r.status_code == 403, f"forged signature accepted: {r.status_code}"


def test_unsigned_rejected():
    fresh_session()
    r = post("/twilio/lead-answer?batch=1&lead=rec0&room=dialer-test",
             {"CallSid": "CA0"}, sign=False)
    assert r.status_code == 403, f"unsigned webhook accepted: {r.status_code}"


def test_winner_gets_conference_loser_gets_hangup():
    bid, batch = fresh_session()
    qs = f"?batch={bid}&lead={batch[0]['id']}&room=dialer-test"
    won = post(f"/twilio/lead-answer{qs}", {"CallSid": "CA0", "CallStatus": "in-progress"})
    body = won.data.decode()
    assert "<Conference" in body and "dialer-test" in body, f"winner not bridged: {body[:300]}"
    assert 'startConferenceOnEnter="false"' in body, "lead must not start the conference"
    assert 'endConferenceOnExit="false"' in body, "lead hangup must not kill your session"

    qs2 = f"?batch={bid}&lead={batch[1]['id']}&room=dialer-test"
    lost = post(f"/twilio/lead-answer{qs2}", {"CallSid": "CA1", "CallStatus": "in-progress"})
    lbody = lost.data.decode()
    assert "<Hangup" in lbody and "<Conference" not in lbody, f"loser bridged too: {lbody[:300]}"


def test_agent_twiml_holds_the_room():
    fresh_session()
    r = post("/twilio/agent-answer?room=dialer-test", {"CallSid": "CAagent"})
    body = r.data.decode()
    assert 'startConferenceOnEnter="true"' in body, "agent must open the room"
    assert 'endConferenceOnExit="true"' in body, "your hangup should end the session"


def test_amd_and_status_routes_accept_signed_posts():
    bid, batch = fresh_session()
    r = post(f"/twilio/amd?batch={bid}&lead={batch[0]['id']}",
             {"CallSid": "CA0", "AnsweredBy": "machine_start"})
    assert r.status_code in (200, 204), f"amd rejected: {r.status_code}"

    r = post(f"/twilio/lead-status?batch={bid}&lead={batch[1]['id']}",
             {"CallSid": "CA1", "CallStatus": "no-answer"})
    assert r.status_code in (200, 204), f"lead-status rejected: {r.status_code}"

    r = post("/twilio/agent-status", {"CallSid": "CAagent", "CallStatus": "completed"})
    assert r.status_code in (200, 204), f"agent-status rejected: {r.status_code}"


def test_no_voicemail_means_machines_hang_up():
    from dialer import profiles
    profiles.set_active("blitz")
    assert telephony.voicemail_url() == "", "blitz should have no voicemail audio"
    twiml = telephony.voicemail_twiml()
    assert "<Play" not in twiml and "<Hangup" in twiml, twiml


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  {fn.__name__.replace('test_', '').replace('_', ' ')}")
    print(f"\n{len(tests)} webhook tests passed.")
