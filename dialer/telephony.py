"""Twilio call origination, teardown, and TwiML."""

import logging
import threading

from twilio.rest import Client
from twilio.twiml.voice_response import Conference, Dial, VoiceResponse

from . import config, profiles

log = logging.getLogger("dialer.telephony")

_client = None


def client():
    global _client
    if _client is None:
        _client = Client(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)
    return _client


def _url(path, **params):
    from urllib.parse import urlencode

    base = f"{config.PUBLIC_URL}{path}"
    return f"{base}?{urlencode(params)}" if params else base


# --- outbound ---------------------------------------------------------------

def call_agent(conference_name):
    """Ring your phone once at session start. You stay parked in the conference
    for the whole session while batches come and go around you."""
    return client().calls.create(
        to=config.AGENT_PHONE,
        from_=profiles.caller_id(),
        url=_url("/twilio/agent-answer", room=conference_name),
        status_callback=_url("/twilio/agent-status"),
        status_callback_event=["completed"],
        timeout=30,
    )


def dial_lead(lead, batch_id, conference_name):
    """Fire one line in a batch.

    AsyncAmd is the important bit: the call bridges the instant it's answered
    and the machine/human verdict arrives separately a few seconds later. The
    synchronous alternative makes every live human sit through dead air while
    Twilio makes up its mind, which is how you tank your answer rate.
    """
    return client().calls.create(
        to=lead["phone"],
        from_=profiles.caller_id(),
        url=_url(
            "/twilio/lead-answer",
            batch=batch_id,
            lead=lead["id"],
            room=conference_name,
        ),
        status_callback=_url("/twilio/lead-status", batch=batch_id, lead=lead["id"]),
        status_callback_event=["initiated", "ringing", "answered", "completed"],
        timeout=profiles.get("ring_timeout", config.RING_TIMEOUT),
        machine_detection=profiles.get("machine_detection", config.MACHINE_DETECTION),
        machine_detection_speech_threshold=config.AMD_SPEECH_THRESHOLD,
        machine_detection_timeout=config.AMD_TIMEOUT,
        async_amd=True,
        async_amd_status_callback=_url("/twilio/amd", batch=batch_id, lead=lead["id"]),
        async_amd_status_callback_method="POST",
    )


# --- teardown ---------------------------------------------------------------

def hangup(call_sid):
    if not call_sid:
        return
    try:
        client().calls(call_sid).update(status="completed")
    except Exception as exc:  # already gone, or never connected
        log.debug("hangup %s: %s", call_sid, exc)


def hangup_all(call_sids):
    """Kill the losing lines. Runs off-thread so the winning leg's TwiML goes
    back to Twilio immediately -- a slow webhook response here is audible to
    the person who just picked up."""
    sids = [s for s in call_sids if s]
    if not sids:
        return
    for sid in sids:
        threading.Thread(target=hangup, args=(sid,), daemon=True).start()


def redirect(call_sid, path, **params):
    """Move a live call to different TwiML (used for the voicemail drop)."""
    try:
        client().calls(call_sid).update(url=_url(path, **params), method="POST")
        return True
    except Exception as exc:
        log.debug("redirect %s: %s", call_sid, exc)
        return False


# --- TwiML ------------------------------------------------------------------

def _conference(room, is_agent):
    conf = Conference(
        room,
        beep=False,
        start_conference_on_enter=is_agent,
        # The agent leaving ends the session. A lead hanging up must NOT tear
        # down the room, or you'd get dropped after every single call.
        end_conference_on_exit=is_agent,
        wait_url="",  # silence between batches instead of Twilio hold music
    )
    if is_agent and (profiles.active().get("record") or config.RECORD_CALLS):
        conf.record = "record-from-start"
        conf.recording_status_callback = _url("/twilio/recording")
    return conf


def agent_twiml(room):
    vr = VoiceResponse()
    vr.say("Dialer connected.", voice="Polly.Matthew")
    dial = Dial()
    dial.append(_conference(room, is_agent=True))
    vr.append(dial)
    return str(vr)


def lead_twiml(room):
    vr = VoiceResponse()
    dial = Dial()
    dial.append(_conference(room, is_agent=False))
    vr.append(dial)
    return str(vr)


def hangup_twiml():
    vr = VoiceResponse()
    vr.hangup()
    return str(vr)


def voicemail_url():
    """Where the drop audio lives.

    A leading "/" means it's served by this app through the tunnel -- drop the
    mp3 in static/ and you're done. Saves standing up S3 or a Twilio Asset just
    to host one file, and it survives the tunnel hostname changing every run.
    """
    url = profiles.get("voicemail_audio_url", config.VOICEMAIL_AUDIO_URL)
    if url and url.startswith("/"):
        return f"{config.PUBLIC_URL}{url}"
    return url


def voicemail_twiml():
    """Played to a line we've decided is voicemail. In blitz mode this IS the
    outreach, not a consolation prize."""
    vr = VoiceResponse()
    url = voicemail_url()
    if url:
        pause = profiles.get("voicemail_pause", config.VOICEMAIL_PAUSE)
        if pause:
            vr.pause(length=pause)
        vr.play(url)
    vr.hangup()
    return str(vr)
