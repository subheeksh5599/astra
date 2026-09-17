"""Every refusal has a name, and the order they are checked in is deliberate.

These tests exercise the decision function alone: no network, no clock, no
wallet. If a refusal can be reached in a test, it can be explained in a demo.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astra import classifier, protocol  # noqa: E402

FIXTURES = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "fixtures", "captured.json"), encoding="utf-8"))

WALLET = "0x1776D4D751d97c85845bF54e6CE364CEc62D4bBf"
RECIPIENT = "0x3991d5267e013fb9d5f2fbb30b8f3d8ff97c1ad9"
TOKEN = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
REQUEST = {"source_domain": 6, "destination_domain": 0, "burn_tx": "0xabc"}
MESSAGE = {
    "source_domain": 6,
    "destination_domain": 0,
    "destination_caller": protocol.ZERO_ADDRESS,
    "mint_recipient": RECIPIENT,
    "burn_token": TOKEN,
    "amount": 1_000_000,
}


def observed(**over):
    base = {
        "attestation": {"state": "final"},
        "message": dict(MESSAGE),
        "preflight": {"ok": True, "already_delivered": False, "reason": None},
        "executing_wallet": WALLET,
        "disagreements": [],
        "transmitter_check": {"address": "0xdead", "reported_domain": 0, "matches": True},
    }
    base.update(over)
    return base


def test_a_clean_transfer_completes():
    decision = classifier.decide(dict(REQUEST), observed())
    assert decision["action"] == classifier.COMPLETE
    assert decision["reason"] == "READY"


def test_a_pending_attestation_defers_instead_of_refusing():
    decision = classifier.decide(dict(REQUEST), observed(attestation={"state": "pending"}))
    assert decision["action"] == classifier.DEFER
    assert decision["reason"] == "ATTESTATION_PENDING"


def test_no_transfer_at_all_is_a_refusal():
    decision = classifier.decide(dict(REQUEST), observed(attestation={"state": "not_found"}))
    assert decision["reason"] == "NOT_FOUND"


def test_an_undecodable_message_is_a_refusal():
    decision = classifier.decide(dict(REQUEST), observed(message=None))
    assert decision["reason"] == "UNREADABLE_MESSAGE"


def test_decoders_that_disagree_stop_the_run():
    decision = classifier.decide(dict(REQUEST), observed(
        disagreements=[{"field": "amount", "ours": 1, "service": 2}]))
    assert decision["reason"] == "MESSAGE_INCONSISTENT"


def test_a_transfer_that_travels_elsewhere_is_refused():
    decision = classifier.decide(dict(REQUEST), observed(
        message={**MESSAGE, "destination_domain": 3}))
    assert decision["reason"] == "ROUTE_MISMATCH"


def test_the_requested_recipient_must_be_the_one_in_the_message():
    request = {**REQUEST, "expect": {"recipient": "0x" + "22" * 20}}
    assert classifier.decide(request, observed())["reason"] == "RECIPIENT_MISMATCH"


def test_the_requested_amount_must_match():
    request = {**REQUEST, "expect": {"amount": 7}}
    assert classifier.decide(request, observed())["reason"] == "AMOUNT_MISMATCH"


def test_the_requested_token_must_match():
    request = {**REQUEST, "expect": {"burn_token": "0x" + "33" * 20}}
    assert classifier.decide(request, observed())["reason"] == "TOKEN_MISMATCH"


def test_a_message_that_names_another_caller_is_not_ours_to_deliver():
    decision = classifier.decide(dict(REQUEST), observed(
        message={**MESSAGE, "destination_caller": "0x617b0e8d15c3a48ddf2ec70ab17bf7cc7dbf3046"}))
    assert decision["reason"] == "CALLER_RESTRICTED"
    assert "617b0e8d15c3a48ddf2ec70ab17bf7cc7dbf3046" in decision["detail"]


def test_a_message_naming_the_executing_wallet_is_ours_to_deliver():
    decision = classifier.decide(dict(REQUEST), observed(
        message={**MESSAGE, "destination_caller": WALLET}))
    assert decision["action"] == classifier.COMPLETE


def test_the_destination_contract_is_checked_before_it_is_called():
    decision = classifier.decide(dict(REQUEST), observed(
        transmitter_check={"address": "0xdead", "reported_domain": 5, "matches": False}))
    assert decision["reason"] == "WRONG_TRANSMITTER"


def test_an_unwatched_destination_is_named_rather_than_attempted():
    decision = classifier.decide({**REQUEST, "destination_domain": 26},
                                 observed(message={**MESSAGE, "destination_domain": 26}))
    assert decision["reason"] == "UNSUPPORTED_DOMAIN"


def test_an_already_delivered_transfer_is_refused_on_the_destinations_words():
    decision = classifier.decide(dict(REQUEST), observed(preflight={
        "ok": False, "already_delivered": True,
        "reason": "Error(Nonce already used)   Error(Nonce already used)"}))
    assert decision["reason"] == "ALREADY_DELIVERED"
    assert "Nonce already used" in decision["evidence"]


def test_any_other_destination_revert_keeps_its_own_reason():
    decision = classifier.decide(dict(REQUEST), observed(preflight={
        "ok": False, "already_delivered": False, "reason": "Error(InvalidAttestation)"}))
    assert decision["reason"] == "PREFLIGHT_REVERT"
    assert "InvalidAttestation" in decision["evidence"]


def test_an_already_delivered_transfer_is_not_reported_as_a_generic_revert():
    decision = classifier.decide(dict(REQUEST), observed(preflight={
        "ok": False, "already_delivered": True, "reason": "nonce already used"}))
    assert decision["reason"] == "ALREADY_DELIVERED"


def test_interpreting_a_simulation_response():
    assert classifier.interpret_preflight({"success": True, "wouldRevert": False})["ok"] is True
    reverted = classifier.interpret_preflight({"success": False, "wouldRevert": True,
                                               "error": "execution reverted: Nonce already used"})
    assert reverted["ok"] is False and reverted["already_delivered"] is True
    assert classifier.interpret_preflight(None)["ok"] is False


def test_a_message_with_no_caller_field_is_never_refused_for_a_caller():
    """The caller refusal belongs to the layout that has a caller.

    On the older deployment the burn takes no destination caller, so a transfer
    there can be delivered by anyone. Refusing it because `destination_caller` is
    unset would refuse a transfer the protocol explicitly allows, which is the
    kind of over-refusal that makes a rail useless.
    """
    import json as _json
    import os as _os
    from astra import protocol
    fixtures = _json.load(open(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                                             "fixtures", "captured.json"), encoding="utf-8"))
    message = protocol.parse(fixtures["open_transfer_v1"]["message"])
    decision = classifier.decide({"source_domain": 6, "destination_domain": 0},
                                 {"attestation": {"state": "complete"}, "message": message,
                                  "executing_wallet": "0x1111111111111111111111111111111111111111",
                                  "destination_record": {"used": False},
                                  "preflight": {"ok": True}})
    assert decision["action"] == classifier.COMPLETE
    assert decision["reason"] == "READY"


def test_a_chain_without_this_deployment_is_refused_by_name():
    """Five chains are watched, but the earlier deployment only exists on two.

    A transfer to a chain the deployment is not on has no transmitter to call.
    The rail must say that rather than attempt it, and the refusal must be its
    own reason so the receipt does not blame the message for the deployment.
    """
    from astra import protocol
    message = protocol.parse(FIXTURES["open_transfer_v2"]["message"])
    decision = classifier.decide(
        {"source_domain": 6, "destination_domain": 0},
        {"attestation": {"state": "complete"}, "message": message,
         "executing_wallet": "0x1111111111111111111111111111111111111111",
         "destination_record": {"used": False}, "preflight": {"ok": True},
         "transmitter_address": None})
    assert decision["action"] == classifier.REFUSE
    assert decision["reason"] == "DEPLOYMENT_ABSENT"


def test_an_unobserved_transmitter_is_not_treated_as_a_missing_one():
    """An absent observation is not evidence; only a recorded absence is."""
    from astra import protocol
    message = protocol.parse(FIXTURES["open_transfer_v2"]["message"])
    decision = classifier.decide(
        {"source_domain": 6, "destination_domain": 0},
        {"attestation": {"state": "complete"}, "message": message,
         "destination_record": {"used": False}, "preflight": {"ok": True}})
    assert decision["reason"] != "DEPLOYMENT_ABSENT"


def test_the_watched_domains_match_the_chains_the_deployment_is_on():
    """The list the classifier trusts and the map the rail calls must agree.

    A domain that is watched but has no contract is a refusal that can never
    succeed; a domain with a contract that is not watched is an available
    delivery that is silently refused. Both are bugs, so they fail here.
    """
    from astra import config
    env = {"ASTRA_DEPLOYMENT": "v2"}
    watched = set(classifier.WATCHED_DOMAINS)
    present = {domain for domain in config.CHAIN_IDS if config.supports(env, domain)}
    assert watched == present

    older = {"ASTRA_DEPLOYMENT": "v1"}
    assert config.supports(older, 0) and config.supports(older, 6)
    assert not config.supports(older, 2)
    assert config.transmitter(older, 2) is None


def test_every_watched_domain_has_a_token_and_a_chain_id():
    from astra import config
    for domain in classifier.WATCHED_DOMAINS:
        assert domain in config.CHAIN_IDS
        assert domain in config.USDC
        assert domain in config.DEFAULT_RPC
