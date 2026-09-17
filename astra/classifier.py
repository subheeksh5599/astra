"""When money may move, and when it may not.

The decision is a pure function over facts observed from the protocol. It has no
network client and no clock, so every refusal is reproducible and testable.

The order matters. A transfer that has already been delivered must be refused
*before* anything is simulated, because simulating a delivery that has happened
is how a rail talks itself into spending gas on a mint that cannot occur. A
transfer whose attestation is not final must be deferred rather than refused:
nothing is wrong, it is simply early.
"""
from __future__ import annotations

# The protocol's own words for "this message has been received already". The
# destination contract raises this when a nonce is spent; the rail reads the
# refusal out of the destination's response rather than guessing.
ALREADY_DELIVERED_MARKERS = (
    "nonce already used",
    "already received",
    "used nonce",
    "message already received",
)

# The destinations this rail watches: every testnet the execution layer can
# deliver to and the protocol is deployed on. A transfer to anywhere else is
# refused by name rather than attempted, because an unwatched chain has no
# transmitter to call.
WATCHED_DOMAINS = (0, 2, 3, 6, 7)

COMPLETE = "complete"
DEFER = "defer"
REFUSE = "refuse"


def interpret_preflight(body: dict | None) -> dict:
    """Turn the execution layer's simulation response into a destination verdict.

    A simulation answers one question: would this call succeed against the
    destination as it stands? Everything else in the response is evidence that
    travels with the decision.
    """
    if not isinstance(body, dict):
        return {"ok": False, "reason": "simulation returned no readable body", "raw": body}
    would_revert = bool(body.get("wouldRevert"))
    success = bool(body.get("success"))
    text = " ".join(str(body.get(key, "")) for key in
                    ("error", "message", "reason", "revertReason", "raw", "result"))
    lowered = text.lower()
    already = any(marker in lowered for marker in ALREADY_DELIVERED_MARKERS)
    return {"ok": success and not would_revert, "already_delivered": already,
            "reason": text.strip()[:400] or None, "raw": body}


def decide(request: dict, observed: dict) -> dict:
    """Return {action, reason, detail} for one transfer.

    request:  source_domain, destination_domain, burn_tx, and optionally
              expect = {recipient, amount, burn_token}
    observed: attestation (state/message), message (parsed or None),
              preflight (as returned by interpret_preflight), executing_wallet
    """
    attestation = observed.get("attestation") or {}
    state = attestation.get("state")

    if state == "not_found":
        return {"action": REFUSE, "reason": "NOT_FOUND",
                "detail": "the source transaction carries no transfer: nothing to deliver"}
    if state == "pending":
        return {"action": DEFER, "reason": "ATTESTATION_PENDING",
                "detail": "the source chain has not finalised, so the attestation is not signed yet"}

    message = observed.get("message")
    if not message:
        return {"action": REFUSE, "reason": "UNREADABLE_MESSAGE",
                "detail": "the attestation is signed but the message could not be decoded"}

    disagreements = observed.get("disagreements") or []
    if disagreements:
        return {"action": REFUSE, "reason": "MESSAGE_INCONSISTENT",
                "detail": "our decode of the message and the attestation service's decode disagree",
                "evidence": disagreements}

    if (message["source_domain"] != request["source_domain"]
            or message["destination_domain"] != request["destination_domain"]):
        return {"action": REFUSE, "reason": "ROUTE_MISMATCH",
                "detail": (f"the message travels {message['source_domain']} -> "
                           f"{message['destination_domain']}, not "
                           f"{request['source_domain']} -> {request['destination_domain']}")}

    expect = request.get("expect") or {}
    for field, key in (("mint_recipient", "recipient"), ("burn_token", "burn_token")):
        want = expect.get(key)
        if want and str(want).lower() != str(message[field]).lower():
            return {"action": REFUSE, "reason": "RECIPIENT_MISMATCH" if key == "recipient"
                    else "TOKEN_MISMATCH",
                    "detail": f"requested {key} {want} does not match the message's {message[field]}"}
    if expect.get("amount") and int(expect["amount"]) != message["amount"]:
        return {"action": REFUSE, "reason": "AMOUNT_MISMATCH",
                "detail": f"requested {expect['amount']} does not match the message's {message['amount']}"}

    # Whether this rail can reach the destination at all precedes whether this
    # wallet may call it: an unreachable chain has no answer to the second
    # question, and a receipt that names the wrong fault teaches the wrong thing.
    if request.get("destination_domain") not in WATCHED_DOMAINS:
        return {"action": REFUSE, "reason": "UNSUPPORTED_DOMAIN",
                "detail": (f"the message travels to domain {request.get('destination_domain')}, "
                           "which this rail does not watch")}

    # Only a *recorded* absence is evidence. Callers that never looked must not
    # have their transfers refused on a fact nobody observed.
    if "transmitter_address" in observed and observed["transmitter_address"] is None:
        return {"action": REFUSE, "reason": "DEPLOYMENT_ABSENT",
                "detail": (f"this deployment is not on the destination chain for domain "
                           f"{request.get('destination_domain')}, so there is nothing to deliver "
                           "the message to")}

    caller = message.get("destination_caller")
    wallet = (observed.get("executing_wallet") or "").lower()
    if caller and message.get("has_destination_caller", True) and caller.lower() not in ("0x" + "0" * 40, wallet):
        return {"action": REFUSE, "reason": "CALLER_RESTRICTED",
                "detail": f"the message names {caller} as its only caller; this wallet may not deliver it"}

    check = observed.get("transmitter_check")
    if check and not check.get("matches"):
        return {"action": REFUSE, "reason": "WRONG_TRANSMITTER",
                "detail": (f"the contract {check.get('address')} reports domain "
                           f"{check.get('reported_domain')}, not {request['destination_domain']}")}

    record = observed.get("destination_record") or {}
    if record.get("used") is True:
        return {"action": REFUSE, "reason": "ALREADY_DELIVERED",
                "detail": "the destination's own record already holds this transfer; a second delivery is not possible",
                "evidence": f"the destination contract reports nonce {record.get('nonce')} as used"}

    preflight = observed.get("preflight") or {}
    if not preflight.get("ok"):
        if preflight.get("already_delivered") and record.get("used") is not False:
            return {"action": REFUSE, "reason": "ALREADY_DELIVERED",
                    "detail": "the destination has already received this transfer; a second delivery is not possible",
                    "evidence": preflight.get("reason")}
        return {"action": REFUSE, "reason": "PREFLIGHT_REVERT",
                "detail": "the destination would reject the delivery",
                "evidence": preflight.get("reason")}

    return {"action": COMPLETE, "reason": "READY",
            "detail": "attestation final, message matches the request, destination accepts the delivery"}
