"""The dial session state machine.

This is the crux of a parallel dialer. Three to five lines ring at once; the
first human to pick up wins and everyone else gets hung up on. The only thing
that makes that safe is that "did anyone win yet?" and "I am the winner now"
happen as one indivisible step. Everything below hangs off `self._lock` for
exactly that reason -- Flask serves webhooks on multiple threads, so two leads
answering 200ms apart really can land in `claim_winner` at the same moment.

The second thing that makes it correct is the requeue. When a line gets hung up
mid-ring because someone else answered first, that person was never contacted.
Marking them done would silently eat ~2 numbers for every connect. On a list
you're required to work through, that's the difference between finishing and
only thinking you finished.
"""

import itertools
import queue
import threading
import time

_batch_counter = itertools.count(1)

# How many times a number can come back around before we stop chasing it.
MAX_ATTEMPTS = 3

# A line in one of these states never reached a person -- it goes back in line.
REQUEUE_STATES = ("dialing", "ringing")


class Leg:
    """One outbound line inside a batch."""

    __slots__ = ("call_sid", "lead", "state", "answered_by")

    def __init__(self, call_sid, lead):
        self.call_sid = call_sid
        self.lead = lead
        self.state = "dialing"  # dialing -> ringing -> answered|dead|machine
        self.answered_by = None

    def to_dict(self):
        return {
            "call_sid": self.call_sid,
            "state": self.state,
            "answered_by": self.answered_by,
            "lead": self.lead,
        }


class Batch:
    __slots__ = ("id", "legs", "winner_sid", "created_at", "closed")

    def __init__(self, batch_id, leads):
        self.id = batch_id
        # Keyed by lead id, not call sid: a lead can answer before the Twilio
        # REST response carrying its sid has even landed back here.
        self.legs = {lead["id"]: Leg(None, lead) for lead in leads}
        self.winner_sid = None
        self.created_at = time.time()
        self.closed = False

    def leg_by_sid(self, call_sid):
        for leg in self.legs.values():
            if leg.call_sid == call_sid:
                return leg
        return None

    def all_dead(self):
        return all(leg.state in ("dead", "machine") for leg in self.legs.values())


class Session:
    """Holds the whole dialing session. One per server process."""

    def __init__(self, lines_per_batch=3, on_outcome=None):
        self._lock = threading.RLock()
        self._subscribers = []
        self._sub_lock = threading.Lock()

        # Called with (lead, outcome, note, attempts) for every terminal
        # outcome, automatic ones included. app.py wires this to results.record.
        self.on_outcome = on_outcome

        self.lines_per_batch = lines_per_batch
        self.reset()

    # -- lifecycle ----------------------------------------------------------

    def reset(self):
        with self._lock:
            self.state = "idle"
            self.agent_call_sid = None
            self.conference_name = None
            self.queue = []
            self.cursor = 0
            self.requeue = []        # leads hung up mid-ring, owed another try
            self.attempts = {}       # lead id -> times dialed
            self.completed = set()   # lead ids with a terminal outcome
            self.batch = None
            self.active_leg = None
            self.history = []
            self.error = None

    def start(self, leads, conference_name):
        with self._lock:
            self.reset()
            self.queue = list(leads)
            self.conference_name = conference_name
            self.state = "connecting"
        self.emit("state")

    def set_agent_call(self, call_sid):
        with self._lock:
            self.agent_call_sid = call_sid
        self.emit("state")

    def agent_connected(self):
        with self._lock:
            if self.state == "connecting":
                self.state = "ready"
        self.emit("state")

    def stop(self, reason=None):
        with self._lock:
            batch = self.batch
            # Lines still ringing when we pulled the plug were never reached.
            if batch:
                for leg in batch.legs.values():
                    if leg.state in REQUEUE_STATES:
                        self._requeue(leg.lead)
            self.state = "stopped"
            self.error = reason
            self.batch = None
            self.active_leg = None
        self.emit("state")
        return batch

    # -- queue --------------------------------------------------------------

    def _requeue(self, lead):
        """Put a never-contacted number back in line. Caller holds the lock."""
        if lead["id"] in self.completed:
            return
        if self.attempts.get(lead["id"], 0) >= MAX_ATTEMPTS:
            self._finish(lead, "failed", note="max attempts reached")
            return
        if any(l["id"] == lead["id"] for l in self.requeue):
            return
        self.requeue.append(lead)

    def _next_leads(self, n):
        """Requeued numbers first, then fresh ones. Caller holds the lock."""
        out = []
        while self.requeue and len(out) < n:
            lead = self.requeue.pop(0)
            if lead["id"] not in self.completed:
                out.append(lead)
        while self.cursor < len(self.queue) and len(out) < n:
            lead = self.queue[self.cursor]
            self.cursor += 1
            if lead["id"] not in self.completed:
                out.append(lead)
        return out

    def open_batch(self):
        """Reserve the next N numbers and open a batch. Returns (batch_id,
        leads) or (None, []) when the list is worked through."""
        with self._lock:
            if self.state not in ("ready", "wrapup"):
                return None, []
            if self.batch is not None and not self.batch.closed:
                return None, []

            leads = self._next_leads(self.lines_per_batch)
            if not leads:
                self.state = "ready"
                return None, []

            for lead in leads:
                self.attempts[lead["id"]] = self.attempts.get(lead["id"], 0) + 1

            batch_id = str(next(_batch_counter))
            self.batch = Batch(batch_id, leads)
            self.active_leg = None
            self.state = "dialing"
        self.emit("state")
        return batch_id, leads

    # -- legs ---------------------------------------------------------------

    def register_leg(self, batch_id, lead_id, call_sid):
        """Attach the Twilio call sid once the REST create returns. Returns True
        if this leg should be hung up -- the race was lost while we waited."""
        with self._lock:
            batch = self.batch
            if not batch or batch.id != batch_id:
                return True
            leg = batch.legs.get(lead_id)
            if not leg:
                return True
            leg.call_sid = call_sid
            if leg.state == "dialing":
                leg.state = "ringing"
            if batch.winner_sid and batch.winner_sid != call_sid:
                leg.state = "dead"
                self._requeue(leg.lead)
                return True
        self.emit("lines")
        return False

    def fail_leg(self, batch_id, lead_id, reason="failed"):
        """A line ended without reaching anyone."""
        with self._lock:
            batch = self.batch
            if not batch or batch.id != batch_id:
                return
            leg = batch.legs.get(lead_id)
            if not leg or leg.state in ("answered", "machine"):
                return
            leg.state = "dead"
            leg.answered_by = reason
            if reason == "busy":
                # They're on the phone. Not an answer, not a miss -- try later.
                self._requeue(leg.lead)
            elif reason == "no-answer":
                self._finish(leg.lead, "no-answer")
            elif reason in ("failed", "dial-error", "canceled"):
                self._requeue(leg.lead)
        self.emit("lines")

    def claim_winner(self, batch_id, lead_id, call_sid):
        """THE atomic step. Returns (won, losers) where `losers` are call sids
        to hang up. Only one caller in the process can get won=True per batch.

        Losing lines were ringing, not answered -- they go straight back in the
        queue so nobody gets skipped."""
        losers = []
        with self._lock:
            batch = self.batch
            if not batch or batch.id != batch_id or batch.closed:
                return False, []          # stale batch -- we already moved on
            if batch.winner_sid is not None:
                return False, []          # someone beat us to it

            leg = batch.legs.get(lead_id)
            if leg is None:
                return False, []
            leg.call_sid = call_sid
            leg.state = "answered"

            batch.winner_sid = call_sid
            batch.closed = True
            self.active_leg = leg
            self.state = "live"

            for other in batch.legs.values():
                if other is leg:
                    continue
                if other.state in REQUEUE_STATES:
                    if other.call_sid:
                        losers.append(other.call_sid)
                    self._requeue(other.lead)
                other.state = "dead"

        self.emit("state")
        return True, losers

    def mark_machine(self, call_sid, answered_by):
        """Async AMD says voicemail. In blitz mode the drop is the outreach, so
        this counts as reaching them."""
        with self._lock:
            batch = self.batch
            if not batch:
                return None
            leg = batch.leg_by_sid(call_sid)
            if leg is None:
                return None
            leg.answered_by = answered_by
            leg.state = "machine"
            self._finish(leg.lead, "voicemail")
            if batch.winner_sid == call_sid:
                batch.winner_sid = None
                self.active_leg = None
                self.state = "wrapup"
        self.emit("state")
        return leg

    def leg_ended(self, call_sid, outcome=None):
        """A lead's call completed. If it was the live one, go to wrap-up."""
        with self._lock:
            batch = self.batch
            if not batch:
                return False
            leg = batch.leg_by_sid(call_sid)
            if leg is None:
                return False
            was_live = batch.winner_sid == call_sid
            if leg.state != "machine":
                leg.state = "dead"
            if was_live:
                batch.winner_sid = None
                self.active_leg = None
                self.state = "wrapup"
                self._finish(leg.lead, outcome or "connected", pending=True)
        self.emit("state")
        return was_live

    def batch_is_spent(self):
        with self._lock:
            batch = self.batch
            if not batch:
                return self.state in ("ready", "wrapup")
            if self.state == "live":
                return False
            return batch.all_dead()

    def close_batch(self):
        with self._lock:
            if self.batch:
                self.batch.closed = True
            if self.state in ("dialing", "wrapup"):
                self.state = "ready"
        self.emit("state")

    # -- outcomes -----------------------------------------------------------

    def _finish(self, lead, outcome, note=None, pending=False):
        """Mark a number done and write it out. Caller holds the lock.

        `pending` means a connected call you can still re-label with a
        disposition -- it's recorded immediately anyway so a crash mid-session
        never loses the fact that the call happened."""
        if lead["id"] in self.completed:
            return
        self.completed.add(lead["id"])
        entry = {
            "lead": lead,
            "outcome": outcome,
            "note": note,
            "at": time.time(),
            "pending": pending,
        }
        self.history.append(entry)
        if self.on_outcome:
            try:
                self.on_outcome(lead, outcome, note, self.attempts.get(lead["id"], 1))
            except Exception:
                pass

    def disposition(self, outcome, note=None):
        """Your click after a live call. Relabels the pending entry."""
        with self._lock:
            lead = None
            if self.history and self.history[-1].get("pending"):
                entry = self.history[-1]
                entry.update(outcome=outcome, note=note, pending=False)
                lead = entry["lead"]
                if self.on_outcome:
                    try:
                        self.on_outcome(lead, outcome, note,
                                        self.attempts.get(lead["id"], 1))
                    except Exception:
                        pass
            elif self.active_leg:
                lead = self.active_leg.lead
                self._finish(lead, outcome, note)
            if self.state == "wrapup":
                self.state = "ready"
        self.emit("state")
        return lead

    def remaining(self):
        with self._lock:
            return self._remaining()

    def _remaining(self):
        return max(0, len(self.queue) - len(self.completed))

    # -- snapshot / events --------------------------------------------------

    def snapshot(self):
        with self._lock:
            lines = []
            if self.batch:
                lines = [leg.to_dict() for leg in self.batch.legs.values()]
            return {
                "state": self.state,
                "error": self.error,
                "lines": lines,
                "batch_id": self.batch.id if self.batch else None,
                "active": self.active_leg.to_dict() if self.active_leg else None,
                "remaining": self._remaining(),
                "total": len(self.queue),
                "dialed": len(self.completed),
                "retrying": len(self.requeue),
                "lines_per_batch": self.lines_per_batch,
                "history": self.history[-25:][::-1],
                "up_next": (self.requeue + self.queue[self.cursor:])[:6],
            }

    def subscribe(self):
        q = queue.Queue(maxsize=50)
        with self._sub_lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q):
        with self._sub_lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def emit(self, kind="state"):
        payload = self.snapshot()
        payload["event"] = kind
        with self._sub_lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(payload)
            except queue.Full:
                pass
