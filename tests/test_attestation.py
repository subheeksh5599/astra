"""The wait, and what it is allowed to conclude.

A freshly made transfer is not in the attestation service's answer yet. Reading
that as "there is no such transfer" would refuse exactly the transfer the caller
just created, so the wait has to tell "not yet" from "never" -- and it still has to
return what it saw when the budget runs out.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astra.attestation import RETRY_STATES, Attestation  # noqa: E402


class Scripted(Attestation):
    """An attestation service that answers a fixed script."""

    def __init__(self, answers):
        super().__init__("http://scripted.invalid", "v2")
        self.answers = list(answers)
        self.seen = []

    def by_transaction(self, source_domain, tx_hash):
        self.seen.append(source_domain)
        return self.answers.pop(0) if self.answers else {"state": "final", "attestation": "0xdead"}


def test_a_transfer_that_is_not_indexed_yet_is_waited_for_not_refused():
    service = Scripted([{"state": "not_found"}, {"state": "not_found"}, {"state": "pending"},
                        {"state": "final", "attestation": "0xbeef"}])
    waits = []
    answer = service.await_final(6, "0x" + "ab" * 32, max_wait=30, interval=0,
                                 on_wait=lambda state, reads, left: waits.append((state["state"], reads, left)))
    assert answer["state"] == "final"
    assert [entry[0] for entry in waits] == ["not_found", "not_found", "pending"]
    assert [entry[1] for entry in waits] == [1, 2, 3]


def test_the_budget_running_out_reports_what_was_actually_seen():
    service = Scripted([{"state": "not_found"}] * 4)
    answer = service.await_final(6, "0x" + "ab" * 32, max_wait=0, interval=0)
    assert answer["state"] == "not_found"
    assert "not_found" in RETRY_STATES


def test_a_final_answer_stops_the_wait_immediately():
    service = Scripted([{"state": "final", "attestation": "0xbeef"}])
    answer = service.await_final(6, "0x" + "ab" * 32, max_wait=600, interval=0)
    assert answer["state"] == "final"
    assert len(service.seen) == 1
