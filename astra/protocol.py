"""The protocol's own wire format, parsed from the protocol's own bytes.

A cross-chain transfer is a message: a header naming the two domains and the
destination caller, and a body naming the token, the recipient and the amount.
Astra reads those fields out of the bytes rather than trusting a request, which
is what makes the mismatch refusals possible.

Two layouts are live at once, and the message length is what tells them apart.

Core layout, 272 bytes:

    version             4
    sourceDomain        4
    destinationDomain   4
    nonce              32
    sender             32
    recipient          32
    destinationCaller  32
    body:
      version           4
      burnToken        32
      mintRecipient    32
      amount           32
      messageSender    32

Extended layout, 376 bytes before any hook payload:

    the same header, then minFinalityThreshold 4 and finalityThresholdExecuted 4,
    then a body that carries maxFee, feeExecuted and expirationBlock after
    messageSender.

The offsets below are not guesses: they were read off transfers this rail
actually created, and the test suite pins them against those captured messages.
"""
from __future__ import annotations

HEADER = [("version", 4), ("source_domain", 4), ("destination_domain", 4), ("nonce", 32),
          ("sender", 32), ("recipient", 32), ("destination_caller", 32)]
HEADER_BYTES = sum(size for _, size in HEADER)
FINALITY = [("min_finality_threshold", 4), ("finality_threshold_executed", 4)]
BODY_CORE = [("version", 4), ("burn_token", 32), ("mint_recipient", 32), ("amount", 32),
             ("message_sender", 32)]
BODY_EXTENDED = [("version", 4), ("burn_token", 32), ("mint_recipient", 32), ("amount", 32),
                 ("message_sender", 32), ("max_fee", 32), ("fee_executed", 32),
                 ("expiration_block", 32)]
CORE_BYTES = HEADER_BYTES + sum(size for _, size in BODY_CORE)
EXTENDED_BYTES = HEADER_BYTES + sum(size for _, size in FINALITY) + sum(size for _, size in BODY_EXTENDED)

ZERO_ADDRESS = "0x" + "0" * 40


class BadMessage(ValueError):
    """The bytes are not a message this rail can reason about."""


def _read(raw: bytes, layout, offset: int = 0) -> tuple[dict, int]:
    out = {}
    for name, size in layout:
        if offset + size > len(raw):
            raise BadMessage(f"message ended inside field '{name}'")
        out[name] = raw[offset:offset + size]
        offset += size
    return out, offset


def _address(field: bytes) -> str:
    return "0x" + field[-20:].hex()


def parse(message_hex: str) -> dict:
    """Decode a message into the fields the rail decides on."""
    if not message_hex or not message_hex.startswith("0x"):
        raise BadMessage("message is not hex")
    raw = bytes.fromhex(message_hex[2:])
    if len(raw) < EXTENDED_BYTES:
        raise BadMessage(f"message is {len(raw)} bytes; this rail decodes the {EXTENDED_BYTES}-byte "
                         "layout (the later deployment). A shorter layout is read through the "
                         "attestation service's own decode instead of being guessed at.")

    head, offset = _read(raw, HEADER)
    compact = len(raw) < EXTENDED_BYTES
    layout = "compact" if compact else "extended"
    if compact:
        finality, body = {}, {}
        body_end = offset
    else:
        finality, offset = _read(raw, FINALITY, offset)
        body, body_end = _read(raw, BODY_EXTENDED, offset)

    parsed = {
        "layout": layout,
        "version": int.from_bytes(head["version"], "big"),
        "source_domain": int.from_bytes(head["source_domain"], "big"),
        "destination_domain": int.from_bytes(head["destination_domain"], "big"),
        "nonce": "0x" + head["nonce"].hex(),
        "sender": _address(head["sender"]),
        "recipient": _address(head["recipient"]),
        "destination_caller": _address(head["destination_caller"]),
    }
    if not compact:
        parsed.update({
            "body_version": int.from_bytes(body["version"], "big"),
            "burn_token": _address(body["burn_token"]),
            "mint_recipient": _address(body["mint_recipient"]),
            "amount": int.from_bytes(body["amount"], "big"),
            "message_sender": _address(body["message_sender"]),
        })
    if layout == "extended":
        parsed["min_finality_threshold"] = int.from_bytes(finality["min_finality_threshold"], "big")
        parsed["finality_threshold_executed"] = int.from_bytes(finality["finality_threshold_executed"], "big")
        parsed["max_fee"] = int.from_bytes(body["max_fee"], "big")
        parsed["fee_executed"] = int.from_bytes(body["fee_executed"], "big")
        parsed["expiration_block"] = int.from_bytes(body["expiration_block"], "big")
        parsed["hook_data"] = "0x" + raw[body_end:].hex() if len(raw) > body_end else "0x"
    return parsed


COMPARED = ("source_domain", "destination_domain", "nonce", "destination_caller", "burn_token",
            "mint_recipient", "amount", "message_sender")


def from_service(decoded: dict) -> dict:
    """Build the same field view out of the attestation service's own decode.

    Used only when our own decode cannot read the layout. The fields are then
    the service's, so there is nothing to cross-check them against; the rail
    records that provenance in the receipt rather than pretending otherwise,
    and the destination contract still verifies the message and the attestation
    against each other before any money moves.
    """
    body = decoded.get("decodedMessageBody") or {}
    return {
        "layout": "service",
        "provenance": "attestation-service",
        "source_domain": _int(decoded.get("sourceDomain")),
        "destination_domain": _int(decoded.get("destinationDomain")),
        "nonce": (decoded.get("nonce") or "").lower() or None,
        "sender": _addr(decoded.get("sender")),
        "recipient": _addr(decoded.get("recipient")),
        "destination_caller": _addr(decoded.get("destinationCaller")),
        "burn_token": _addr(body.get("burnToken")),
        "mint_recipient": _addr(body.get("mintRecipient")),
        "amount": _int(body.get("amount")),
        "message_sender": _addr(body.get("messageSender")),
        "max_fee": _int(body.get("maxFee")),
        "fee_executed": _int(body.get("feeExecuted")),
    }


def merge(parsed: dict, decoded: dict | None) -> tuple[dict, list]:
    """Cross-check our decode against the attestation service's decode.

    Both are reading the same bytes. When they disagree, the rail does not pick
    a winner and proceed; it stops, because a decision made on a disagreement is
    a decision about a transfer nobody has described correctly.

    The chain still has the last word: the destination contract verifies the
    attestation against the message, so a transfer that gets this far cannot be
    delivered on the strength of a bad decode.
    """
    fields = dict(parsed)
    disagreements: list = []
    if not decoded:
        return fields, disagreements

    body = decoded.get("decodedMessageBody") or {}
    service = {
        "source_domain": _int(decoded.get("sourceDomain")),
        "destination_domain": _int(decoded.get("destinationDomain")),
        "nonce": (decoded.get("nonce") or "").lower() or None,
        "destination_caller": _addr(decoded.get("destinationCaller")),
        "burn_token": _addr(body.get("burnToken") or decoded.get("burnToken")),
        "mint_recipient": _addr(body.get("mintRecipient") or decoded.get("mintRecipient")),
        "amount": _int(body.get("amount") or decoded.get("amount")),
        "message_sender": _addr(body.get("messageSender") or decoded.get("messageSender")),
    }
    for key in COMPARED:
        theirs = service.get(key)
        if theirs in (None, ""):
            continue
        ours = fields.get(key)
        if ours is None:
            fields[key] = theirs
            continue
        if isinstance(ours, str) and isinstance(theirs, str):
            same = ours.lower() == theirs.lower()
        else:
            same = ours == theirs
        if not same:
            disagreements.append({"field": key, "ours": ours, "service": theirs})
    for key in ("max_fee", "fee_executed"):
        service_key = "maxFee" if key == "max_fee" else "feeExecuted"
        theirs = _int(body.get(service_key))
        if theirs is not None and key in fields and fields[key] != theirs:
            disagreements.append({"field": key, "ours": fields[key], "service": theirs})
    return fields, disagreements


def _int(value):
    if value is None or value == "":
        return None
    try:
        return int(value, 16) if isinstance(value, str) and value.startswith("0x") else int(value)
    except (TypeError, ValueError):
        return None


def _addr(value):
    if not value:
        return None
    text = str(value)
    if text.startswith("0x") and len(text) == 66:
        return "0x" + text[-40:]
    return text.lower() if text.startswith("0x") else text


def transfer_id(parsed: dict) -> str:
    """The protocol's own identity for a transfer: its source domain and nonce.

    Deliberately not a local counter. A rail that keys on its own bookkeeping
    cannot tell that a transfer has already been delivered by someone else.
    """
    return f"{parsed['source_domain']}:{parsed['nonce']}"


def is_open_caller(parsed: dict) -> bool:
    """True when the protocol lets anyone complete the transfer."""
    return str(parsed["destination_caller"]).lower() == ZERO_ADDRESS


def amount_usdc(parsed: dict, decimals: int = 6) -> float:
    return parsed["amount"] / (10 ** decimals)
