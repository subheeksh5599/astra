"""The wire format, pinned against messages this project actually produced.

Every message in these tests was captured from a transfer this rail created on a
public testnet. If the decoder drifts, these fail, which is the point: the
refusal decisions downstream are only as good as the fields they read.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astra import inflight, protocol  # noqa: E402

FIXTURES = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "fixtures", "captured.json"), encoding="utf-8"))


def test_extended_layout_reads_the_real_fields():
    parsed = protocol.parse(FIXTURES["open_transfer_v2"]["message"])
    decoded = FIXTURES["open_transfer_v2"]["decoded"]
    assert parsed["layout"] == "extended"
    assert parsed["source_domain"] == 6
    assert parsed["destination_domain"] == 0
    assert parsed["nonce"] == decoded["nonce"]
    assert parsed["destination_caller"] == protocol.ZERO_ADDRESS
    assert parsed["burn_token"].lower() == decoded["decodedMessageBody"]["burnToken"].lower()
    assert parsed["mint_recipient"].lower() == decoded["decodedMessageBody"]["mintRecipient"].lower()
    assert parsed["amount"] == int(decoded["decodedMessageBody"]["amount"])
    assert parsed["fee_executed"] == int(decoded["decodedMessageBody"]["feeExecuted"])


def test_a_restricted_caller_is_read_off_the_message():
    parsed = protocol.parse(FIXTURES["restricted_transfer_v2"]["message"])
    assert parsed["destination_caller"].lower() == \
        FIXTURES["restricted_transfer_v2"]["decoded"]["destinationCaller"].lower()
    assert not protocol.is_open_caller(parsed)


def test_our_decode_and_the_service_decode_agree():
    parsed = protocol.parse(FIXTURES["open_transfer_v2"]["message"])
    fields, disagreements = protocol.merge(parsed, FIXTURES["open_transfer_v2"]["decoded"])
    assert disagreements == []
    assert fields["amount"] == 1_000_000


def test_a_disagreement_is_reported_not_swallowed():
    parsed = protocol.parse(FIXTURES["open_transfer_v2"]["message"])
    decoded = json.loads(json.dumps(FIXTURES["open_transfer_v2"]["decoded"]))
    decoded["decodedMessageBody"]["mintRecipient"] = "0x" + "11" * 20
    _fields, disagreements = protocol.merge(parsed, decoded)
    assert [d["field"] for d in disagreements] == ["mint_recipient"]


def test_the_shorter_layout_is_not_guessed_at():
    with pytest.raises(protocol.BadMessage):
        protocol.parse(FIXTURES["open_transfer_v1"]["message"])


def test_a_service_only_view_is_labelled_as_such():
    view = protocol.from_service(FIXTURES["open_transfer_v2"]["decoded"])
    assert view["provenance"] == "attestation-service"
    assert view["source_domain"] == 6
    assert view["destination_domain"] == 0


def test_the_log_envelope_yields_the_announced_message():
    announced = inflight.decode_message_sent(FIXTURES["message_sent_log_data"])
    assert announced and announced.startswith("0x")
    assert len(announced) == 2 + 376 * 2


def test_the_log_envelope_rejects_anything_else():
    assert inflight.decode_message_sent("0x") is None
    assert inflight.decode_message_sent("0x" + "00" * 40) is None
    assert inflight.decode_message_sent("0x" + "00" * 31 + "20" + "00" * 31 + "ff") is None


def test_transfer_identity_is_the_protocols_own_nonce():
    parsed = protocol.parse(FIXTURES["open_transfer_v2"]["message"])
    assert protocol.transfer_id(parsed) == f"6:{parsed['nonce']}"
