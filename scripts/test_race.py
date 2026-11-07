#!/usr/bin/env python3
"""State machine tests.

Two things have to hold on a required call list:
  1. Exactly one line can win a batch, no matter how they interleave.
  2. Nobody gets skipped. A line hung up mid-ring was never contacted and
     must come back around.

    python scripts/test_race.py
"""

import sys
import threading
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dialer.session import MAX_ATTEMPTS, Session  # noqa: E402

ROUNDS = 300


def leads(n):
    return [{"id": f"rec{i}", "phone": f"+1555000{i:04d}", "name": f"Lead {i}"}
            for i in range(n)]


def ready(n_leads, lines=3, on_outcome=None):
    s = Session(lines_per_batch=lines, on_outcome=on_outcome)
    s.start(leads(n_leads), "test-room")
    s.agent_connected()
    return s


# -- 1. the race ------------------------------------------------------------

def race_round(rnd):
    s = ready(3)
    batch_id, batch = s.open_batch()
    for i, lead in enumerate(batch):
        s.register_leg(batch_id, lead["id"], f"CA{rnd}-{i}")

    out, gate = [], threading.Barrier(len(batch))

    def answer(i, lead):
        gate.wait()  # all threads released at the same instant
        out.append(s.claim_winner(batch_id, lead["id"], f"CA{rnd}-{i}"))

    ts = [threading.Thread(target=answer, args=(i, l)) for i, l in enumerate(batch)]
    [t.start() for t in ts]
    [t.join() for t in ts]

    winners = [r for r in out if r[0]]
    assert len(winners) == 1, f"round {rnd}: {len(winners)} winners"
    assert s.state == "live" and s.active_leg is not None
    assert len(winners[0][1]) == len(batch) - 1
    # The two who lost were never reached -- they must be owed another call.
    assert len(s.requeue) == 2, f"round {rnd}: requeue={len(s.requeue)}"
    return len(winners[0][1])


# -- 2. nobody gets skipped -------------------------------------------------

def test_full_list_is_worked_through():
    """The real-world failure: every connect strands the other lines. Simulate
    150 numbers where every 4th batch gets a live answer, and assert all 150
    end up with an outcome."""
    logged = []
    s = ready(150, lines=3, on_outcome=lambda l, o, n, a: logged.append((l["id"], o)))

    batches = 0
    while True:
        bid, batch = s.open_batch()
        if not bid:
            break
        batches += 1
        for i, lead in enumerate(batch):
            s.register_leg(bid, lead["id"], f"CA{bid}-{i}")

        if batches % 4 == 0:            # someone picks up
            s.claim_winner(bid, batch[0]["id"], f"CA{bid}-0")
            s.leg_ended(f"CA{bid}-0", outcome="connected")
        else:                            # all three ring out
            for lead in batch:
                s.fail_leg(bid, lead["id"], "no-answer")
        s.close_batch()

    assert len(s.completed) == 150, f"only {len(s.completed)}/150 got an outcome"
    assert s.remaining() == 0, f"{s.remaining()} left unworked"
    assert len(logged) == 150, f"only {len(logged)}/150 written to results"
    assert not s.requeue
    return batches


def test_race_loser_is_requeued():
    s = ready(3)
    bid, batch = s.open_batch()
    for i, lead in enumerate(batch):
        s.register_leg(bid, lead["id"], f"CA{i}")
    s.claim_winner(bid, batch[0]["id"], "CA0")
    ids = {l["id"] for l in s.requeue}
    assert ids == {batch[1]["id"], batch[2]["id"]}, ids
    assert batch[1]["id"] not in s.completed


def test_busy_is_retried_no_answer_is_not():
    s = ready(6, lines=3)
    bid, batch = s.open_batch()
    for i, lead in enumerate(batch):
        s.register_leg(bid, lead["id"], f"CA{i}")
    s.fail_leg(bid, batch[0]["id"], "busy")
    s.fail_leg(bid, batch[1]["id"], "no-answer")
    assert [l["id"] for l in s.requeue] == [batch[0]["id"]]
    assert batch[1]["id"] in s.completed


def test_retries_are_capped():
    s = ready(1, lines=1)
    for _ in range(MAX_ATTEMPTS + 3):
        bid, batch = s.open_batch()
        if not bid:
            break
        s.register_leg(bid, batch[0]["id"], "CAx")
        s.fail_leg(bid, batch[0]["id"], "busy")
        s.close_batch()
    assert len(s.completed) == 1, "a permanently busy number must not loop forever"


def test_stale_batch_rejected():
    s = ready(6)
    old_id, batch = s.open_batch()
    s.register_leg(old_id, batch[0]["id"], "CAold")
    s.close_batch()
    new_id, _ = s.open_batch()
    won, _ = s.claim_winner(old_id, batch[0]["id"], "CAold")
    assert not won and new_id != old_id


def test_late_register_gets_killed_and_requeued():
    s = ready(3)
    bid, batch = s.open_batch()
    s.claim_winner(bid, batch[0]["id"], "CAwin")
    assert s.register_leg(bid, batch[1]["id"], "CAlate") is True
    assert any(l["id"] == batch[1]["id"] for l in s.requeue)


def test_voicemail_counts_as_reached():
    s = ready(3)
    bid, batch = s.open_batch()
    s.claim_winner(bid, batch[0]["id"], "CAwin")
    s.mark_machine("CAwin", "machine_start")
    assert s.state == "wrapup" and s.active_leg is None
    assert batch[0]["id"] in s.completed
    assert s.history[-1]["outcome"] == "voicemail"


def test_stop_requeues_ringing_lines():
    s = ready(9)
    bid, batch = s.open_batch()
    for i, lead in enumerate(batch):
        s.register_leg(bid, lead["id"], f"CA{i}")
    s.stop()
    assert len(s.requeue) == 3, "lines ringing at Ctrl-C were never contacted"


if __name__ == "__main__":
    kills = Counter(race_round(r) for r in range(ROUNDS))
    print(f"  {ROUNDS} concurrent-answer rounds: exactly 1 winner every time")
    print(f"  lines killed per win: {dict(kills)}")

    n = test_full_list_is_worked_through()
    print(f"  150-number list fully worked through ({n} batches, 0 skipped)")

    for fn in (test_race_loser_is_requeued,
               test_busy_is_retried_no_answer_is_not,
               test_retries_are_capped,
               test_stale_batch_rejected,
               test_late_register_gets_killed_and_requeued,
               test_voicemail_counts_as_reached,
               test_stop_requeues_ringing_lines):
        fn()
        print(f"  {fn.__name__.replace('test_', '').replace('_', ' ')}")

    print("\nAll good.")
