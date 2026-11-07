#!/usr/bin/env python3
"""Parallel dialer server.

  python app.py

Ring LINES_PER_BATCH numbers at once, bridge the first human who picks up into
a conference you're already parked in, and hang up the rest.
"""

import json
import logging
import threading
import time
import uuid
from functools import wraps

from flask import Flask, Response, jsonify, request, send_from_directory
from twilio.request_validator import RequestValidator

from dialer import config, contacts, profiles, results, telephony
from dialer.session import Session

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-5s %(name)s: %(message)s"
)
log = logging.getLogger("dialer")

app = Flask(__name__, static_folder="static", static_url_path="/static")

session = Session(
    lines_per_batch=profiles.get("lines_per_batch", config.LINES_PER_BATCH),
    on_outcome=lambda lead, outcome, note, attempts: results.record(
        lead, outcome, note, attempts),
)
_advance_lock = threading.Lock()
_loaded_queue = []


# --------------------------------------------------------------------------
# Twilio webhook auth
# --------------------------------------------------------------------------

def twilio_webhook(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if config.VALIDATE_TWILIO_SIGNATURES:
            validator = RequestValidator(config.TWILIO_AUTH_TOKEN)
            # Reconstruct the URL exactly as we handed it to Twilio -- behind a
            # tunnel, request.url has the wrong scheme/host.
            url = config.PUBLIC_URL + request.full_path.rstrip("?")
            signature = request.headers.get("X-Twilio-Signature", "")
            if not validator.validate(url, request.form.to_dict(), signature):
                log.warning("rejected unsigned webhook: %s", request.path)
                return Response("invalid signature", status=403)
        return fn(*args, **kwargs)

    return wrapper


# --------------------------------------------------------------------------
# Batch engine
# --------------------------------------------------------------------------

def _fire_next_batch():
    """Open a batch and throw the lines out. Always call this off the webhook
    thread -- it makes one Twilio REST call per line."""
    with _advance_lock:
        session.close_batch()
        batch_id, leads = session.open_batch()

    if not batch_id:
        return

    log.info("batch %s: dialing %s", batch_id, ", ".join(l["phone"] for l in leads))
    for lead in leads:
        try:
            call = telephony.dial_lead(lead, batch_id, session.conference_name)
        except Exception as exc:
            log.error("dial failed for %s: %s", lead["phone"], exc)
            session.fail_leg(batch_id, lead["id"], "dial-error")
            continue
        if session.register_leg(batch_id, lead["id"], call.sid):
            # Race already lost while this REST call was in flight.
            telephony.hangup(call.sid)

    # Everything blew up on creation -- don't strand the session.
    if session.batch_is_spent():
        maybe_advance()


def maybe_advance(force=False):
    if not force and not profiles.get("auto_advance", config.AUTO_ADVANCE):
        return
    if session.state in ("live", "connecting", "stopped", "idle"):
        return
    if not force and not session.batch_is_spent():
        return
    threading.Thread(target=_fire_next_batch, daemon=True).start()


# --------------------------------------------------------------------------
# UI + API
# --------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.get("/api/state")
def api_state():
    snap = session.snapshot()
    snap["config"] = {
        "missing": config.missing_required(),
        "warnings": profiles.check(),
        "queue_loaded": len(_loaded_queue),
        "voicemail_ready": bool(telephony.voicemail_url()),
        "profile": profiles.active_name(),
        "profiles": {n: profiles.ALL[n]["label"] for n in profiles.names()},
        "caller_id": profiles.caller_id(),
    }
    return jsonify(snap)


@app.post("/api/profile")
def api_profile():
    body = request.get_json(silent=True) or {}
    name = body.get("name", "")
    if session.state not in ("idle", "stopped"):
        return jsonify({"error": "stop the session before switching profiles"}), 400
    try:
        prof = profiles.set_active(name)
    except KeyError:
        return jsonify({"error": f"no profile '{name}'"}), 400
    session.lines_per_batch = prof["lines_per_batch"]
    session.emit("state")
    return jsonify({"ok": True, "profile": name, "caller_id": profiles.caller_id()})


@app.get("/api/events")
def api_events():
    def stream():
        q = session.subscribe()
        try:
            yield f"data: {json.dumps(session.snapshot())}\n\n"
            while True:
                try:
                    payload = q.get(timeout=20)
                    yield f"data: {json.dumps(payload)}\n\n"
                except Exception:
                    yield ": keepalive\n\n"
        finally:
            session.unsubscribe(q)

    return Response(
        stream(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/queue/load")
def api_queue_load():
    global _loaded_queue
    body = request.get_json(silent=True) or {}
    path = body.get("path") or config.CONTACTS_CSV
    limit = body.get("limit") or None
    skip_done = body.get("skip_called", True)
    try:
        _loaded_queue = contacts.load_csv(path, limit=limit)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400

    skipped = 0
    if skip_done:
        done = results.already_called(days=7)
        if done:
            before = len(_loaded_queue)
            _loaded_queue = [l for l in _loaded_queue if l["phone"] not in done]
            skipped = before - len(_loaded_queue)

    return jsonify({
        "count": len(_loaded_queue),
        "skipped": skipped,
        "preview": _loaded_queue[:10],
    })


@app.post("/api/session/start")
def api_session_start():
    missing = config.missing_required()
    if missing:
        return jsonify({"error": "missing config: " + ", ".join(missing)}), 400
    if not _loaded_queue:
        return jsonify({"error": "load a queue first"}), 400
    if session.state not in ("idle", "stopped"):
        return jsonify({"error": f"session already {session.state}"}), 400

    room = "dialer-" + uuid.uuid4().hex[:10]
    results.start(profiles.active_name())
    session.start(_loaded_queue, room)
    try:
        call = telephony.call_agent(room)
    except Exception as exc:
        session.stop(reason=str(exc))
        return jsonify({"error": f"could not call you: {exc}"}), 400
    session.set_agent_call(call.sid)
    return jsonify({"ok": True, "room": room, "agent_call_sid": call.sid})


@app.post("/api/session/stop")
def api_session_stop():
    batch = session.stop()
    sids = []
    if batch:
        sids = [leg.call_sid for leg in batch.legs.values() if leg.call_sid]
    telephony.hangup_all(sids)
    telephony.hangup(session.agent_call_sid)
    session.reset()
    session.emit("state")
    return jsonify({"ok": True})


@app.post("/api/batch/next")
def api_batch_next():
    maybe_advance(force=True)
    return jsonify({"ok": True})


@app.post("/api/hangup")
def api_hangup():
    """Drop whoever you're talking to without ending the session."""
    leg = session.active_leg
    if leg and leg.call_sid:
        telephony.hangup(leg.call_sid)
    return jsonify({"ok": True})


@app.post("/api/voicemail")
def api_voicemail():
    """Manual 'this is voicemail' -- drop the message and move on."""
    leg = session.active_leg
    if not leg or not leg.call_sid:
        return jsonify({"error": "no live call"}), 400
    sid = leg.call_sid
    session.mark_machine(sid, "manual")
    if telephony.voicemail_url():
        telephony.redirect(sid, "/twilio/voicemail-drop")
    else:
        telephony.hangup(sid)
    maybe_advance(force=True)
    return jsonify({"ok": True})


@app.post("/api/disposition")
def api_disposition():
    body = request.get_json(silent=True) or {}
    outcome = body.get("outcome", "logged")
    note = body.get("note")

    # The outcome is written to the results CSV by session.on_outcome.
    session.disposition(outcome, note)
    maybe_advance(force=True)
    return jsonify({"ok": True})


@app.get("/api/results")
def api_results():
    path = results.path()
    return jsonify({
        "path": str(path) if path else None,
        "summary": results.summary(),
    })


@app.post("/api/lines")
def api_lines():
    body = request.get_json(silent=True) or {}
    n = max(1, min(5, int(body.get("n", 3))))
    session.lines_per_batch = n
    session.emit("state")
    return jsonify({"ok": True, "lines_per_batch": n})


# --------------------------------------------------------------------------
# Twilio webhooks
# --------------------------------------------------------------------------

@app.post("/twilio/agent-answer")
@twilio_webhook
def tw_agent_answer():
    room = request.args.get("room", "")
    session.agent_connected()
    maybe_advance(force=True)
    return Response(telephony.agent_twiml(room), mimetype="text/xml")


@app.post("/twilio/agent-status")
@twilio_webhook
def tw_agent_status():
    status = request.form.get("CallStatus")
    if status in ("completed", "failed", "busy", "no-answer", "canceled"):
        log.info("agent leg ended (%s) -- tearing down session", status)
        batch = session.stop(reason=None if status == "completed" else status)
        if batch:
            telephony.hangup_all([l.call_sid for l in batch.legs.values() if l.call_sid])
        session.reset()
        session.emit("state")
    return ("", 204)


@app.post("/twilio/lead-answer")
@twilio_webhook
def tw_lead_answer():
    """Someone picked up. Exactly one of these wins."""
    batch_id = request.args.get("batch", "")
    lead_id = request.args.get("lead", "")
    room = request.args.get("room", "")
    call_sid = request.form.get("CallSid", "")

    won, losers = session.claim_winner(batch_id, lead_id, call_sid)
    if not won:
        return Response(telephony.hangup_twiml(), mimetype="text/xml")

    telephony.hangup_all(losers)
    log.info("batch %s: bridged %s (killed %d)", batch_id, call_sid, len(losers))
    return Response(telephony.lead_twiml(room), mimetype="text/xml")


@app.post("/twilio/lead-status")
@twilio_webhook
def tw_lead_status():
    batch_id = request.args.get("batch", "")
    lead_id = request.args.get("lead", "")
    call_sid = request.form.get("CallSid", "")
    status = request.form.get("CallStatus")

    if status in ("busy", "no-answer", "failed", "canceled"):
        session.fail_leg(batch_id, lead_id, status)
        maybe_advance()
    elif status == "completed":
        was_live = session.leg_ended(call_sid, outcome="connected")
        wrapup = profiles.get("wrapup_seconds", config.WRAPUP_SECONDS) or 0
        if was_live and wrapup > 0:
            threading.Timer(wrapup, lambda: maybe_advance(force=True)).start()
        elif not was_live:
            maybe_advance()
    return ("", 204)


@app.post("/twilio/amd")
@twilio_webhook
def tw_amd():
    """Async answering-machine verdict, a few seconds after the bridge."""
    call_sid = request.form.get("CallSid", "")
    answered_by = request.form.get("AnsweredBy", "")

    if answered_by.startswith("machine") or answered_by == "fax":
        leg = session.mark_machine(call_sid, answered_by)
        if leg:
            log.info("voicemail on %s (%s)", leg.lead.get("name"), answered_by)
            if telephony.voicemail_url():
                telephony.redirect(call_sid, "/twilio/voicemail-drop")
            else:
                telephony.hangup(call_sid)
        # Don't wait for the drop to finish playing -- move on now.
        maybe_advance(force=True)
    return ("", 204)


@app.post("/twilio/voicemail-drop")
@twilio_webhook
def tw_voicemail_drop():
    return Response(telephony.voicemail_twiml(), mimetype="text/xml")


@app.post("/twilio/recording")
@twilio_webhook
def tw_recording():
    log.info("recording ready: %s", request.form.get("RecordingUrl"))
    return ("", 204)


# --------------------------------------------------------------------------

if __name__ == "__main__":
    missing = config.missing_required()
    if missing:
        print("\n  Not dial-ready yet. Missing in .env: " + ", ".join(missing))
        print("  The UI will still load so you can look around.\n")
    print(f"  Parallel dialer  ->  http://localhost:{config.PORT}")
    print(f"  Public URL       ->  {config.PUBLIC_URL or '(not set)'}\n")
    app.run(host="127.0.0.1", port=config.PORT, threaded=True, debug=False)
