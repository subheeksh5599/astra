"""The protocol's own wire format, parsed from the protocol's own bytes.

A cross-chain transfer is a message: a header naming the two domains and the
destination caller, and a body naming the token, the recipient and the amount.
Astra reads those fields out of the bytes rather than trusting a request, which
is what makes the mismatch refusals possible.

Two layouts are live at once. The message length tells them apart, and both were
derived from transfers this rail created and then cross-checked against the
messenger's own deposit event on the source chain, so the offsets are evidence
rather than an assumption about the protocol's source:

Core layout, 248 bytes (the older deployment):

    version             4
    sourceDomain        4
    destinationDomain   4
    nonce              20      (matches the nonce in the deposit event)
    sender             20   + 12 bytes of padding
    recipient          20   + 12 bytes of padding
    destinationCaller  20
    body:
      version           4
      burnToken        32   (right aligned)
      mintRecipient    32   (right aligned)
      amount           32
      messageSender    32   (right aligned)

Extended layout, 376 bytes before any hook payload (the later deployment):

    version 4, sourceDomain 4, destinationDomain 4, nonce 32, sender 32,
    recipient 32, destinationCaller 32, minFinalityThreshold 4,
    finalityThresholdExecuted 4, then a body of version 4, burnToken 32,
    mintRecipient 32, amount 32, messageSender 32, maxFee 32, feeExecuted 32,
    expirationBlock 32, then an optional hook payload.

The test suite pins both layouts against messages captured from real transfers.
"""
from __future__ import annotations

HEADER = [("version", 4), ("source_domain", 4), ("destination_domain", 4), ("nonce", 32),
          ("sender", 32), ("recipient", 32), ("destination_caller", 32)]
HEADER_BYTES = sum(size for _, size in HEADER)
FINALITY = [("min_finality_threshold", 4), ("finality_threshold_executed", 4)]
BODY_EXTENDED = [("version", 4), ("burn_token", 32), ("mint_recipient", 32), ("amount", 32),
                 ("message_sender", 32), ("max_fee", 32), ("fee_executed", 32),
                 ("expiration_block", 32)]
EXTENDED_BYTES = HEADER_BYTES + sum(size for _, size in FINALITY) + sum(size for _, size in BODY_EXTENDED)

# The older layout, as its own bytes lay it out.
CORE_BYTES = 248
CORE = {
    "version": (0, 4),
    "source_domain": (4, 4),
    "destination_domain": (8, 4),
    "nonce": (12, 20),
    "sender": (32, 20),
    "recipient": (64, 20),
    "destination_caller": (96, 20),
    "body_version": (116, 4),
    "burn_token": (132, 20),
    "mint_recipient": (164, 20),
    "amount": (184, 32),
    "message_sender": (228, 20),
}

ZERO_ADDRESS = "0x" + "0" * 40

# The protocol's announcement event, as the source chain emits it. Filtering on
# it is what makes discovery a read of the protocol's own stream rather than a
# read of everything an address ever said.
MESSAGE_SENT_TOPIC = "0x8c5261668696ce22758910d05bab8f186d6eb247ceac2af2e82c7dc17669b036"


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
    """An address-shaped field, rendered the way its own bytes say it should be.

    Twelve leading zero bytes mean the field holds an EVM-shaped address and the
    last twenty are the value. Anything else is a full-width key -- a destination
    on a chain whose addresses use all thirty-two bytes -- and taking the last
    twenty there would silently change what the message says about who gets paid.
    """
    if len(field) == 32 and field[:12] == b"\x00" * 12:
        return "0x" + field[12:].hex()
    return "0x" + field.hex()


def _core(raw: bytes) -> dict:
    """The older layout, whose padding is part of the layout rather than noise.

    The nonce sits right-aligned in the first eight bytes of its slot, followed by
    padding; the addresses are left-aligned in theirs. Reading them means slicing
    the value and dropping the padding, which is what the deposit event on the
    source chain confirms: this message's nonce, amount and recipient all match
    the event's own words.
    """
    def field(name: str) -> bytes:
        start, size = CORE[name]
        return raw[start:start + size]

    nonce_bytes = field("nonce").rstrip(b"\x00") or b"\x00"

    return {
        "layout": "core",
        "version": int.from_bytes(field("version"), "big"),
        "source_domain": int.from_bytes(field("source_domain"), "big"),
        "destination_domain": int.from_bytes(field("destination_domain"), "big"),
        "nonce": f"0x{int.from_bytes(nonce_bytes, 'big'):x}",
        "sender": "0x" + field("sender").hex(),
        "recipient": "0x" + field("recipient").hex(),
        # This deployment's burn takes no destination caller, and its messages
        # carry none: the slot is padding. Saying `None` here is the honest read,
        # because a zero address would claim the transfer is open by design.
        "destination_caller": None,
        "has_destination_caller": False,
        "body_version": int.from_bytes(field("body_version"), "big"),
        "burn_token": "0x" + field("burn_token").hex(),
        "mint_recipient": "0x" + field("mint_recipient").hex(),
        "amount": int.from_bytes(field("amount"), "big"),
        "message_sender": "0x" + field("message_sender").hex(),
    }


def _extended(raw: bytes) -> dict:
    head, offset = _read(raw, HEADER)
    finality, offset = _read(raw, FINALITY, offset)
    body, body_end = _read(raw, BODY_EXTENDED, offset)
    return {
        "layout": "extended",
        "version": int.from_bytes(head["version"], "big"),
        "source_domain": int.from_bytes(head["source_domain"], "big"),
        "destination_domain": int.from_bytes(head["destination_domain"], "big"),
        "nonce": "0x" + head["nonce"].hex(),
        "sender": _address(head["sender"]),
        "recipient": _address(head["recipient"]),
        "destination_caller": _address(head["destination_caller"]),
        "has_destination_caller": True,
        "body_version": int.from_bytes(body["version"], "big"),
        "burn_token": _address(body["burn_token"]),
        "mint_recipient": _address(body["mint_recipient"]),
        "amount": int.from_bytes(body["amount"], "big"),
        "message_sender": _address(body["message_sender"]),
        "min_finality_threshold": int.from_bytes(finality["min_finality_threshold"], "big"),
        "finality_threshold_executed": int.from_bytes(finality["finality_threshold_executed"], "big"),
        "max_fee": int.from_bytes(body["max_fee"], "big"),
        "fee_executed": int.from_bytes(body["fee_executed"], "big"),
        "expiration_block": int.from_bytes(body["expiration_block"], "big"),
        "hook_data": "0x" + raw[body_end:].hex() if len(raw) > body_end else "0x",
    }


def parse(message_hex: str) -> dict:
    """Decode a message into the fields the rail decides on."""
    if not message_hex or not message_hex.startswith("0x"):
        raise BadMessage("message is not hex")
    raw = bytes.fromhex(message_hex[2:])
    if len(raw) >= EXTENDED_BYTES:
        return _extended(raw)
    if len(raw) == CORE_BYTES:
        return _core(raw)
    raise BadMessage(f"message is {len(raw)} bytes, which is neither the {CORE_BYTES}-byte layout "
                     f"nor at least the {EXTENDED_BYTES}-byte one; it is read through the attestation "
                     "service's own decode instead of being guessed at")


COMPARED = ("source_domain", "destination_domain", "nonce", "destination_caller", "burn_token",
            "mint_recipient", "amount", "message_sender")

# Address-shaped fields are compared as bytes, never as text. Two decoders of the
# same bytes can disagree about spelling without disagreeing about the transfer:
# a destination on a chain with a different address alphabet reports the same 32
# bytes in its own notation, and calling that a disagreement would refuse a
# transfer nobody described incorrectly.
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def as_bytes(value) -> bytes | None:
    """One field's bytes, whatever notation the answer arrived in."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.startswith("0x"):
        try:
            return bytes.fromhex(text[2:].zfill(64) if len(text) <= 66 else text[2:])
        except ValueError:
            return None
    if set(text) <= set(_B58):
        number = 0
        for character in text:
            number = number * 58 + _B58.index(character)
        raw = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
        return b"\x00" * (len(text) - len(text.lstrip("1"))) + raw
    return None


def from_service(decoded: dict) -> dict:
    """Build the same field view out of the attestation service's own decode.

    Used only when our own decode cannot read the layout. The fields are then the
    service's, so there is nothing to cross-check them against; the rail records
    that provenance in the receipt rather than pretending otherwise, and the
    destination contract still verifies the message and the attestation against
    each other before any money moves.
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

    Both are reading the same bytes. When they disagree, the rail does not pick a
    winner and proceed; it stops, because a decision made on a disagreement is a
    decision about a transfer nobody has described correctly.

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
        if not same and isinstance(ours, str) and isinstance(theirs, str):
            # Addresses can be spelled differently on either side of a chain
            # boundary. Compare what the bytes say before reporting a conflict.
            left, right = as_bytes(ours), as_bytes(theirs)
            if left is not None and right is not None:
                same = left[-32:].lstrip(b"\x00") == right[-32:].lstrip(b"\x00")
                if same:
                    fields[key] = ours if key in ("nonce", "amount") else theirs
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
    """The same rendering rule as _address, applied to a value that arrived as text."""
    if not value:
        return None
    text = str(value)
    if text.startswith("0x") and len(text) == 66:
        raw = bytes.fromhex(text[2:])
        if raw[:12] == b"\x00" * 12:
            return "0x" + raw[12:].hex()
        return "0x" + raw.hex()
    return text.lower() if text.startswith("0x") else text


def transfer_id(parsed: dict) -> str:
    """The protocol's own identity for a transfer: its source domain and nonce.

    Deliberately not a local counter. A rail that keys on its own bookkeeping
    cannot tell that a transfer has already been delivered by someone else.
    """
    return f"{parsed['source_domain']}:{parsed['nonce']}"


def is_open_caller(parsed: dict) -> bool:
    """True when the protocol lets anyone complete the transfer.

    A layout with no caller field is open by construction: there is nothing in
    the message that could restrict who delivers it.
    """
    if not parsed.get("has_destination_caller", True):
        return True
    return str(parsed["destination_caller"]).lower() == ZERO_ADDRESS


def amount_usdc(parsed: dict, decimals: int = 6) -> float:
    return parsed["amount"] / (10 ** decimals)
