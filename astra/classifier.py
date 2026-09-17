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

# The destinations this rail watches. A transfer to anywhere else is refused by
# name rather than attempted: an unwatched chain has no transmitter to call.
WATCHED_DOMAINS = (0, 6)

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


