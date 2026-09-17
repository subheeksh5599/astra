"""The execution client, without an execution.

A broadcast answers with an execution id before the transaction hash exists, so
the client polls until the hash appears and stops on a terminal failure. These
tests drive that logic with a scripted transport, which is the only honest way
to test the states a live chain refuses to produce on demand.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astra.keeperhub import KeeperHub  # noqa: E402

ABI = '[{"name":"x","type":"function","inputs":[],"outputs":[],"stateMutability":"nonpayable"}]'


class Scripted:
    """A transport that answers from a script instead of a network."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, path, body=None, idem=None):
        self.calls.append({"method": method, "path": path, "body": body, "idem": idem})
        if not self.responses:
            raise AssertionError("the scripted transport ran out of answers")
        return self.responses.pop(0)


def client(responses):
    kh = KeeperHub("kh_test")
    kh.request = Scripted(responses)
    return kh


def test_the_hash_that_arrives_late_is_still_returned():
    kh = client([
        (202, {"executionId": "exec_1", "status": "completed"}),
        (200, {"status": "pending"}),
        (200, {"status": "completed", "transactionHash": "0xhash", "transactionLink": "https://x/0xhash"}),
    ])
    record = kh.execute(1, "0xcontract", "x", ABI, [], poll_seconds=0)
    assert record["execution_id"] == "exec_1"
    assert kh.transaction_hash(record) == "0xhash"
    assert kh.transaction_link(record) == "https://x/0xhash"
    assert len(kh.request.calls) == 3


def test_a_terminal_failure_stops_the_polling():
    kh = client([
        (202, {"executionId": "exec_2"}),
        (200, {"status": "reverted"}),
    ])
    record = kh.execute(1, "0xcontract", "x", ABI, [], poll_seconds=0)
    assert record["status"]["status"] == "reverted"
    assert kh.transaction_hash(record) is None
    assert len(kh.request.calls) == 2


def test_an_idempotency_key_travels_with_the_broadcast():
    kh = client([(202, {"executionId": "exec_3"}), (200, {"status": "completed", "transactionHash": "0x9"})])
    kh.execute(1, "0xcontract", "x", ABI, [], idem="astra-6-abc", poll_seconds=0)
    assert kh.request.calls[0]["idem"] == "astra-6-abc"


def test_a_broadcast_without_an_execution_id_is_reported_as_it_came_back():
    kh = client([(400, {"error": "invalid functionArgs"})])
    record = kh.execute(1, "0xcontract", "x", ABI, [])
    assert record["http"] == 400
    assert record.get("execution_id") is None


def test_the_wallet_is_read_out_of_the_integration_list():
    kh = client([(200, [{"type": "discord"}, {"type": "web3", "address": "0xwallet"}])])
    assert kh.wallet() == "0xwallet"


def test_a_simulation_never_broadcasts():
    kh = client([(200, {"success": True, "wouldRevert": False})])
    _status, body = kh.simulate(1, "0xcontract", "x", ABI, [])
    assert body["success"] is True
    assert all(call["body"].get("simulate") is True for call in kh.request.calls)
