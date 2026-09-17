"""The paying side: the one place anything is signed, and what it refuses.

Nothing here signs. Every case below is decided before a key is touched, which is
the point: a transfer that cannot be paid for should be refused by the rail's own
rules and not discovered by a wallet that has already been asked to approve.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astra import payer  # noqa: E402


def test_the_recipient_field_is_a_twenty_byte_address_inside_thirty_two_bytes():
    encoded = payer.encode_recipient("0x3991D5267e013fB9d5f2fbb30B8F3D8FF97C1AD9")
    assert len(encoded) == 66
    assert encoded[:26] == "0x" + "0" * 24
    assert encoded.lower().endswith("3991d5267e013fb9d5f2fbb30b8f3d8ff97c1ad9")


def test_no_caller_is_the_zero_word_and_a_caller_is_an_address():
    assert payer.encode_caller(None) == "0x" + "0" * 64
    assert payer.encode_caller("0x617B0e8d15c3A48dDF2EC70AB17bF7CC7DbF3046").lower().endswith(
        "617b0e8d15c3a48ddf2ec70ab17bf7cc7dbf3046")
    with pytest.raises(ValueError):
        payer.encode_caller("0xnotanaddress")


def test_a_chain_the_rail_cannot_pay_on_is_refused_before_anything_is_signed():
    with pytest.raises(ValueError):
        payer.open_transfer({}, 1.0, 5, 0)


def test_an_unwatched_destination_is_refused_before_anything_is_signed():
    with pytest.raises(ValueError):
        payer.open_transfer({}, 1.0, 6, 5)


def test_a_named_caller_on_the_deployment_whose_burn_takes_none_is_refused():
    with pytest.raises(ValueError):
        payer.open_transfer({}, 1.0, 6, 0, caller="0x" + "11" * 20, deployment="v1")


def test_an_amount_of_nothing_is_refused():
    with pytest.raises(ValueError):
        payer.open_transfer({}, 0, 6, 0)


def test_no_key_is_a_clear_refusal_rather_than_a_crash():
    with pytest.raises(payer.PayerUnavailable) as excinfo:
        payer.private_key({})
    assert "PAYER_KEY" in str(excinfo.value)
    assert payer.key_name({"PRIVATE_KEY": "0x1"}) == "PRIVATE_KEY"
    assert payer.key_name({"PAYER_KEY": "0x1", "PRIVATE_KEY": "0x2"}) == "PAYER_KEY"
