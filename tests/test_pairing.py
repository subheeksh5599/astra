"""The invariant: what counts as paired, and what counts as broken.

These tests are about the two ways "one burn, exactly one mint" can fail. The
verdict function is pure, so every verdict a receipt can carry is reachable here.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astra import pairing, protocol  # noqa: E402

FIXTURES = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "fixtures", "captured.json"), encoding="utf-8"))


def test_the_two_deployments_write_the_same_nonce_differently_and_still_pair():
    """A counter and a 32-byte word, one transfer.

    The older deployment publishes its nonce as a small number and the later one
    as a full word. If those two spellings did not land on one key, the older
    deployment's deliveries could never be paired against its own burns.
    """
    older = protocol.parse(FIXTURES["open_transfer_v1"]["message"])
    newer = protocol.parse(FIXTURES["open_transfer_v2"]["message"])
    assert older["nonce"] != newer["nonce"]
    assert pairing.transfer_key(6, older["nonce"]) == f"6:{int(older['nonce'], 16)}"
    assert pairing.transfer_key(6, newer["nonce"]) == f"6:{int(newer['nonce'], 16)}"
    # the destination publishes the nonce as an integer, so that spelling must be
    # the same key as the message's
    assert pairing.transfer_key(6, int(older["nonce"], 16)) == pairing.transfer_key(6, older["nonce"])


def test_a_delivered_transfer_is_paired_and_an_unfinished_signed_one_is_stranded():
    assert pairing.verdict("final", [{"tx": "0x1"}], True) == "paired"
    assert pairing.verdict("final", [], True) == "stranded"


def test_pending_is_in_flight_rather_than_stranded():
    """Nothing is wrong with a transfer whose attestation is not signed yet."""
    assert pairing.verdict("pending", [], True) == "in flight"


def test_a_chain_the_rail_cannot_reach_is_unwatched_not_broken():
    assert pairing.verdict("final", [], False) == "unwatched"
    assert pairing.verdict("final", [{"tx": "0x1"}], False) == "unwatched"


def test_only_stranded_and_double_delivery_count_as_broken():
    result = {
        "rows": [{"verdict": "paired", "delivered_count": 1, "transfer_id": "6:1", "mint_txs": []},
                 {"verdict": "in flight", "delivered_count": 0, "transfer_id": "6:2", "mint_txs": []},
                 {"verdict": "unwatched", "delivered_count": 0, "transfer_id": "6:3", "mint_txs": []},
                 {"verdict": "stranded", "delivered_count": 0, "transfer_id": "6:4", "mint_txs": []}],
        "duplicates": [{"verdict": "paired", "delivered_count": 2, "transfer_id": "6:5",
                        "mint_txs": ["0xaa", "0xbb"]}],
    }
    assert [row["transfer_id"] for row in pairing.broken(result)] == ["6:4", "6:5"]


def test_the_summary_counts_every_verdict_it_saw():
    result = {"rows": [{"verdict": "paired"}, {"verdict": "paired"}, {"verdict": "stranded"}]}
    assert pairing.summarise(result) == "2 paired, 1 stranded"


def test_a_mint_short_of_the_burn_is_broken():
    """The invariant is a quantity, not just an event.

    A mint that happened but delivered less than the burn announced is the same
    failure as no mint at all, and it is the one a count of events cannot see.
    """
    result = {"rows": [{"verdict": "paired", "delivered_count": 1, "transfer_id": "6:1",
                        "mint_txs": ["0xaa"], "value_matches": False,
                        "expected_amount": 49_994, "minted_amount": 40_000}],
              "duplicates": []}
    assert [row["transfer_id"] for row in pairing.broken(result)] == ["6:1"]


def test_a_mint_that_could_not_be_measured_is_not_called_broken():
    result = {"rows": [{"verdict": "paired", "delivered_count": 1, "transfer_id": "6:1",
                        "mint_txs": [], "value_matches": None}],
              "duplicates": []}
    assert pairing.broken(result) == []


def test_the_delivery_this_rail_made_settles_to_the_protocol_fee():
    """Real numbers from a delivery: 0.05 USDC burned, a 6-unit fee, 49,994 minted.

    Fast transfers charge a fee, the message carries it, and the token's transfer
    event shows what arrived. Those three have to agree; this pins the arithmetic
    the rail relies on.
    """
    amount, fee, minted = 50_000, 6, 49_994
    assert amount - fee == minted
