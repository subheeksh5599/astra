"""The transfer application's own guarantees, pinned.

These tests are about one question repeated: does the application ever state
something the chains did not? A decode that guesses, an amount no witness
supports, a validation that lets a doomed transfer through, or an execution that
fires without a fresh read are all the same failure, and each of them is pinned
here.
"""
import json

import pytest

from astra import source, transfers

#: A real depositForBurn from this deployment: 0.5 USDC, Base Sepolia -> Ethereum
#: Sepolia, recipient and caller both the paying wallet, fee cap 500, fast threshold.
REAL_CALLDATA = (
    "0x8e0250ee"
    "000000000000000000000000000000000000000000000000000000000007a120"  # amount 500000
    "0000000000000000000000000000000000000000000000000000000000000000"  # destination 0
    "0000000000000000000000003991d5267e013fb9d5f2fbb30b8f3d8ff97c1ad9"  # mint recipient
    "000000000000000000000000036cbd53842c5426634e7929541ec2318f3dcf7e"  # burn token
    "0000000000000000000000000000000000000000000000000000000000000000"  # caller: open
    "00000000000000000000000000000000000000000000000000000000000001f4"  # max fee 500
    "00000000000000000000000000000000000000000000000000000000000003e8"  # threshold 1000
)
TOKEN = "0x036cbd53842c5426634e7929541ec2318f3dcf7e"
PAYER = "0x3991d5267e013fb9d5f2fbb30b8f3d8ff97c1ad9"


class ScriptedRpc:
    """A chain that answers with fixed numbers, so validation can be tested offline."""

    def __init__(self, *args, **kwargs):
        pass

    def balance_of(self, token, holder):
        return 10_000_000

    def call(self, method, params):
        return hex(10 ** 18)

    def call_contract(self, to, data):
        return hex(10_000_000)

    def block_number(self):
        return 1


# ---------------------------------------------------------------- the witnesses

def test_calldata_decode_reads_the_wallet_instruction():
    """The instruction the user approved is read field by field, not inferred."""
    decoded = source.decode_burn_calldata(REAL_CALLDATA)
    assert decoded is not None
    assert decoded["amount"] == 500000
    assert decoded["amount_usdc"] == 0.5
    assert decoded["destination_domain"] == 0
    assert decoded["recipient"] == PAYER
    assert decoded["burn_token"] == TOKEN
    assert decoded["caller"] is None
    assert decoded["max_fee"] == 500
    assert decoded["min_finality_threshold"] == 1000
    assert "calldata" in decoded["witness"]


def test_calldata_decode_refuses_what_it_cannot_read():
    """Another function's calldata, or a truncated one, is not a transfer."""
    assert source.decode_burn_calldata("0x095ea7b3" + "00" * 64) is None
    assert source.decode_burn_calldata(REAL_CALLDATA[:40]) is None
    assert source.decode_burn_calldata("") is None


def test_witnesses_must_agree_on_the_amount():
    """Where two witnesses speak, agreement is the finding; disagreement is too."""
    same = source.agree({"witness": "calldata", "amount": 500000},
                        {"witness": "event", "amount": 500000},
                        {"witness": "burn", "amount": 500000})
    assert same["amount"] == 500000
    assert same["disagreements"] == []
    assert len(same["witnesses"]) == 3

    short = source.agree({"witness": "calldata", "amount": 500000},
                         {"witness": "event", "amount": 499000})
    assert short["amount"] is None
    assert [d["witness"] for d in short["disagreements"]] == ["event"]

    alone = source.agree({"witness": "calldata", "amount": 500000})
    assert alone["amount"] == 500000


def test_the_rails_own_decode_is_withheld_when_it_fails_its_check(monkeypatch):
    """A parse that contradicts the known domains or invents an amount is not evidence."""
    monkeypatch.setattr(source.protocol, "parse",
                        lambda message: {"source_domain": 9, "amount": 500000})
    assert source._try_rail_decode("0x00") is None

    monkeypatch.setattr(source.protocol, "parse",
                        lambda message: {"source_domain": 6, "amount": 10 ** 30})
    assert source._try_rail_decode("0x00") is None

    monkeypatch.setattr(source.protocol, "parse",
                        lambda message: {"source_domain": 6, "amount": 500000})
    assert source._try_rail_decode("0x00")["amount"] == 500000

    def broken(message):
        raise ValueError("not a message")

    monkeypatch.setattr(source.protocol, "parse", broken)
    assert source._try_rail_decode("0x00") is None


# ---------------------------------------------------------------- identity

def test_a_transfer_is_identified_by_its_chain_and_its_transaction():
    """The id is the burn, not a decode: it exists the moment the receipt does."""
    burn = "0xE7802AED2ba6dd05359f1ef8813d78c81265341edc200fca2034d6a1cb2e395c"
    one = transfers.transfer_id_of(6, burn)
    two = transfers.transfer_id_of(6, burn)
    assert one == two
    assert one == "6-e7802aed2ba6dd05359f1ef8813d78c81265341edc200fca2034d6a1cb2e395c"
    assert transfers.transfer_id_of(0, burn).startswith("0-")


# ---------------------------------------------------------------- the store

def test_the_store_round_trips_and_never_leaks_another_wallet(tmp_path, monkeypatch):
    """A wallet sees its own transfers; a missing one is missing, not empty."""
    monkeypatch.setenv("ASTRA_TRANSFER_DIR", str(tmp_path))
    env = {"ASTRA_TRANSFER_DIR": str(tmp_path)}
    mine = {"transfer_id": "6-aa", "owner": "0xABC", "state": "DELIVERED",
            "created_at": "2026-01-01T00:00:00Z"}
    theirs = {"transfer_id": "6-bb", "owner": "0xdef", "state": "REFUSED",
              "created_at": "2026-01-02T00:00:00Z"}
    transfers.save(env, mine)
    transfers.save(env, theirs)

    loaded = transfers.load(env, "6-aa")
    assert loaded and loaded["state"] == "DELIVERED"
    assert loaded["updated_at"]

    assert transfers.load(env, "6-nope") is None
    assert transfers.load(env, "../../etc/passwd") is None

    assert [r["transfer_id"] for r in transfers.list_records(env, owner="0xabc")] == ["6-aa"]
    assert {r["transfer_id"] for r in transfers.list_records(env)} >= {"6-aa", "6-bb"}
    assert json.load(open(tmp_path / "6-aa.json"))["owner"] == "0xABC"


# ---------------------------------------------------------------- validation

def test_prepare_refuses_an_unsupported_source_chain(monkeypatch):
    """A chain this rail has no token or messenger for is refused by name."""
    monkeypatch.setattr(transfers, "Rpc", ScriptedRpc)
    out = transfers.prepare({}, {"source_domain": 99, "destination_domain": 0,
                                 "amount_usdc": 1, "recipient": PAYER, "address": PAYER})
    assert out["ok"] is False
    assert out["errors"]
    assert any(c["check"] == "source chain" and not c["ok"] for c in out["checks"])


def test_prepare_refuses_a_destination_without_this_deployment(monkeypatch):
    """Watched is not deliverable: with no contract there, there is nothing to call."""
    monkeypatch.setattr(transfers, "Rpc", ScriptedRpc)
    monkeypatch.setattr(transfers, "transmitter", lambda env, domain: None)
    out = transfers.prepare({}, {"source_domain": 6, "destination_domain": 0,
                                 "amount_usdc": 1, "recipient": PAYER, "address": PAYER})
    assert out["ok"] is False
    check = next(c for c in out["checks"] if c["check"] == "destination chain")
    assert check["ok"] is False
    assert "deployment is not on it" in check["detail"] or "not on it" in check["detail"]


def test_prepare_refuses_a_recipient_that_is_not_an_address(monkeypatch):
    """A recipient is an address, and an empty field is named as missing."""
    monkeypatch.setattr(transfers, "Rpc", ScriptedRpc)
    out = transfers.prepare({}, {"source_domain": 6, "destination_domain": 0,
                                 "amount_usdc": 1, "recipient": "nope", "address": PAYER})
    check = next(c for c in out["checks"] if c["check"] == "recipient")
    assert check["ok"] is False

    empty = transfers.prepare({}, {"source_domain": 6, "destination_domain": 0,
                                   "amount_usdc": 1, "recipient": "", "address": PAYER})
    missing = next(c for c in empty["checks"] if c["check"] == "recipient")
    assert missing["ok"] is False
    assert "required" in missing["detail"]


def test_prepare_refuses_an_amount_of_zero(monkeypatch):
    """Nothing is not a transfer."""
    monkeypatch.setattr(transfers, "Rpc", ScriptedRpc)
    out = transfers.prepare({}, {"source_domain": 6, "destination_domain": 0,
                                 "amount_usdc": 0, "recipient": PAYER, "address": PAYER})
    check = next(c for c in out["checks"] if c["check"] == "amount")
    assert check["ok"] is False
    assert out["burn"]["arguments"] is None


def test_prepare_refuses_a_caller_the_deployment_cannot_carry(monkeypatch):
    """The earlier deployment's burn takes no destination caller, so asking for one is refused."""
    monkeypatch.setattr(transfers, "Rpc", ScriptedRpc)
    monkeypatch.setattr(transfers, "deployment", lambda env: "v1")
    out = transfers.prepare({}, {"source_domain": 6, "destination_domain": 0,
                                 "amount_usdc": 1, "recipient": PAYER,
                                 "caller": PAYER, "address": PAYER})
    check = next(c for c in out["checks"] if c["check"] == "destination caller")
    assert check["ok"] is False


def test_prepare_returns_the_exact_burn_the_wallet_must_sign(monkeypatch):
    """When every check passes, the rail hands over the instruction, not a sketch of one."""
    monkeypatch.setattr(transfers, "Rpc", ScriptedRpc)
    out = transfers.prepare({}, {"source_domain": 6, "destination_domain": 0,
                                 "amount_usdc": 0.5, "recipient": PAYER, "address": PAYER})
    assert out["ok"] is True, out["errors"]
    assert out["burn"]["function"].startswith("depositForBurn(uint256,uint32,bytes32,address")
    assert out["burn"]["arguments"][0] == "500000"
    assert out["burn"]["arguments"][1] == "0"
    assert out["burn"]["arguments"][2].endswith(PAYER[2:])
    assert out["needs_approval"] is False  # the scripted chain already approved the amount


# ---------------------------------------------------------------- execution

def test_execute_does_nothing_without_a_fresh_go_ahead(monkeypatch):
    """A refusal is final: no broadcast, no attempt recorded, the reason is returned."""
    for state_name in ("REFUSED", "WAITING_FOR_FINALITY"):
        record = {"transfer_id": "6-aa", "source_domain": 6,
                  "source_tx": "0x" + "ab" * 32, "attempts": []}
        monkeypatch.setattr(transfers, "state_of", lambda env, rec, name=state_name: {
            "state": name,
            "detail": "the protocol says no",
            "decision": {"action": "refuse", "reason": "ALREADY_DELIVERED",
                         "detail": "the destination already holds this transfer"},
            "evidence": ["usedNonces answered true"],
        })
        out = transfers.execute({}, record)
        assert out["attempted"] is False
        assert out["reason"] == "ALREADY_DELIVERED"
        assert record["attempts"] == []


def test_execute_reports_the_reason_when_there_is_no_decision(monkeypatch):
    """A state read that never reached a decision still explains itself."""
    record = {"transfer_id": "6-bb", "source_domain": 6, "source_tx": "0x" + "cd" * 32,
              "attempts": []}
    monkeypatch.setattr(transfers, "state_of", lambda env, rec: {
        "state": "WAITING_FOR_FINALITY", "detail": "the attestation is not signed yet"})
    out = transfers.execute({}, record)
    assert out["attempted"] is False
    assert out["reason"] == "the attestation is not signed yet"
    assert record["attempts"] == []


# ---------------------------------------------------------------- the list view

def test_summary_carries_the_evidence_and_invents_no_state():
    """A record with no state is CREATED, and both transaction hashes travel with it."""
    summary = transfers.summary({"transfer_id": "6-aa", "source_domain": 6,
                                 "source_tx": "0xaa", "destination_tx": "0xbb",
                                 "amount": 500000, "minted": 499935, "owner": PAYER})
    assert summary["state"] == "CREATED"
    assert summary["source_tx"] == "0xaa"
    assert summary["destination_tx"] == "0xbb"
    assert summary["amount"] == 500000 and summary["minted"] == 499935
    assert summary["owner"] == PAYER

    stored = transfers.summary({"transfer_id": "6-bb", "state": "DELIVERED"})
    assert stored["state"] == "DELIVERED"
