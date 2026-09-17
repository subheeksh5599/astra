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


def test_an_unknown_length_is_not_guessed_at():
    """A layout is read because it is known, never because it is plausible.

    Truncating a real message produces bytes that are neither layout; the decoder
    must say so instead of reading fields out of whatever happens to be there.
    """
    truncated = FIXTURES["open_transfer_v2"]["message"][:2 + 300 * 2]
    with pytest.raises(protocol.BadMessage):
        protocol.parse(truncated)
    with pytest.raises(protocol.BadMessage):
        protocol.parse("0x00")


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


def test_core_layout_reads_the_real_fields():
    """The older deployment's layout, pinned against two transfers it produced.

    The nonce, amount, token and recipient in these assertions are also the
    words its deposit event emitted on the source chain, so the offsets are
    confirmed by the protocol rather than inferred from its source.
    """
    for key, nonce in (("open_transfer_v1", 0x9D99), ("second_open_transfer_v1", 0x9D9A)):
        parsed = protocol.parse(FIXTURES[key]["message"])
        assert parsed["layout"] == "core"
        assert parsed["source_domain"] == 6
        assert parsed["destination_domain"] == 0
        assert int(parsed["nonce"], 16) == nonce
        assert parsed["burn_token"].lower() == "0x036cbd53842c5426634e7929541ec2318f3dcf7e"
        assert parsed["mint_recipient"].lower() == "0x3991d5267e013fb9d5f2fbb30b8f3d8ff97c1ad9"
        assert parsed["amount"] == 1_000_000
        assert parsed["message_sender"].lower() == parsed["mint_recipient"].lower()
        assert parsed["sender"].lower() == "0x9f3b8679c73c2fef8b59b4f3444d4e156fb70aa5"
        assert parsed["recipient"].lower() == parsed["sender"].lower()


def test_core_layout_admits_it_carries_no_caller():
    """A layout without a caller slot must not be read as "open by policy"."""
    parsed = protocol.parse(FIXTURES["open_transfer_v1"]["message"])
    assert parsed["destination_caller"] is None
    assert parsed["has_destination_caller"] is False
    assert protocol.is_open_caller(parsed) is True


def test_two_core_messages_differ_only_in_their_nonce():
    """Two transfers of the same shape move one byte range: the nonce.

    This is what makes the offsets in the core layout evidence: the same rail,
    the same arguments, a second burn, and nothing in the message shifts.
    """
    first = bytes.fromhex(FIXTURES["open_transfer_v1"]["message"][2:])
    second = bytes.fromhex(FIXTURES["second_open_transfer_v1"]["message"][2:])
    assert len(first) == len(second) == protocol.CORE_BYTES
    differing = [i for i in range(len(first)) if first[i] != second[i]]
    assert differing and all(12 <= i < 20 for i in differing)


def test_a_layout_that_carries_a_caller_says_so():
    parsed = protocol.parse(FIXTURES["restricted_transfer_v2"]["message"])
    assert parsed["has_destination_caller"] is True
    assert protocol.is_open_caller(parsed) is False


def test_a_full_width_recipient_is_not_truncated_to_an_evm_address():
    """A destination on a chain with 32-byte addresses is paid to 32 bytes.

    This rail decoded a real Solana-bound message by taking the last twenty bytes
    of the recipient, which is a different address: the money is not at risk --
    the destination contract verifies the message -- but the rail's own evidence
    was wrong, and a request checked against that value would have been checked
    against nobody. Twelve leading zero bytes are what make an EVM-shaped field;
    without them the value is full width and is read as it stands.
    """
    from astra import protocol
    evm = bytes.fromhex("00" * 12 + "cf05af9834e97f12959437ebf59a4b23f64dbcce")
    full = bytes.fromhex("9e5230cea3c0ab985e49219acf05af9834e97f12959437ebf59a4b23f64dbcce")
    assert protocol._address(evm) == "0xcf05af9834e97f12959437ebf59a4b23f64dbcce"
    assert protocol._address(full) == "0x" + full.hex()


def test_the_same_bytes_spelled_two_ways_are_not_a_disagreement():
    """One destination chain reports the same 32 bytes in its own notation.

    Two decoders of the same bytes can disagree about spelling without disagreeing
    about the transfer. Comparing that as text would report a conflict between two
    decoders that agree, and refuse a transfer nobody described incorrectly.
    """
    from astra import protocol
    left = protocol.as_bytes("0x9e5230cea3c0ab985e49219acf05af9834e97f12959437ebf59a4b23f64dbcce")
    right = protocol.as_bytes("Bf279qoPVYwhQMcuJpY5yr2bVCiPJePDBbBh13u3hsVF")
    assert left is not None and right is not None
    assert left == right


def test_merge_accepts_a_second_decode_that_only_differs_in_notation():
    from astra import protocol
    parsed = protocol.parse(FIXTURES["open_transfer_v2"]["message"])
    decoded = json.loads(json.dumps(FIXTURES["open_transfer_v2"]["decoded"]))
    decoded["decodedMessageBody"]["mintRecipient"] = \
        "0x00" + decoded["decodedMessageBody"]["mintRecipient"][2:]
    _fields, disagreements = protocol.merge(parsed, decoded)
    assert disagreements == []


def test_merge_still_reports_a_real_disagreement():
    from astra import protocol
    parsed = protocol.parse(FIXTURES["open_transfer_v2"]["message"])
    decoded = json.loads(json.dumps(FIXTURES["open_transfer_v2"]["decoded"]))
    decoded["decodedMessageBody"]["amount"] = "1"
    _fields, disagreements = protocol.merge(parsed, decoded)
    assert [item["field"] for item in disagreements] == ["amount"]
