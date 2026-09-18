"""Reading the source side of a transfer from what the chain actually holds.

Four witnesses exist for a single burn, and they are not the same witness:

  the wallet's instruction     the transaction's calldata — what the user approved
  the protocol's own event     the TokenMessenger's `DepositForBurn`, which is the
                               protocol's record of the transfer it accepted
  the token's accounting       the minimter's `Burn` and the ERC-20 transfers —
                               what actually left the payer's balance
  the announcement             the MessageTransmitter's `MessageSent` — the bytes
                               the destination will be handed

A transfer application that shows one number where four exist cannot tell "the
wallet asked for 5" from "5 was burned" from "the destination will be handed a
message saying 5". This module reads all of them and reports them separately, and
where they disagree that disagreement is the finding.
"""
from __future__ import annotations

from . import protocol

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
DEPOSIT_FOR_BURN_TOPIC = "0x0c8c1cbdc5190613ebd485511d4e2812cfa45eecb79d845893331fedad5130a5"
BURN_TOPIC = "0xcc16f5dbb4873280815c1ee09dbd06736cffcc184412cf7a71a0fdb75d397ca5"
DEPOSIT_FOR_BURN_SELECTOR = "0x8e0250ee"


def _int(value, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value, 16) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        return default


def _words(data: str) -> list:
    if not isinstance(data, str) or not data.startswith("0x"):
        return []
    body = data[2:]
    return [body[i:i + 64] for i in range(0, len(body), 64)]


def _address_from(word: str | None) -> str | None:
    if not word or len(word) < 40 or set(word) == {"0"}:
        return None
    return "0x" + word[-40:]


def decode_burn_calldata(data: str) -> dict | None:
    """The burn the wallet signed, read from the transaction's own bytes."""
    if not isinstance(data, str) or not data.startswith(DEPOSIT_FOR_BURN_SELECTOR):
        return None
    words = _words("0x" + data[10:])
    if len(words) < 4:
        return None
    amount = _int("0x" + words[0])
    if not amount:
        return None
    return {
        "amount": amount,
        "amount_usdc": amount / 1e6,
        "destination_domain": _int("0x" + words[1]),
        "recipient": _address_from(words[2]),
        "burn_token": _address_from(words[3]),
        "caller": _address_from(words[4]) if len(words) > 4 else None,
        "max_fee": _int("0x" + words[5]) if len(words) > 5 else None,
        "min_finality_threshold": _int("0x" + words[6]) if len(words) > 6 else None,
        "witness": "the transaction's own calldata",
    }


def deposit_for_burn(logs: list, messenger: str | None = None) -> dict | None:
    """The protocol's own record of the transfer it accepted.

    Topics: the event signature, the token, the depositor, the finality threshold.
    Data: amount, mint recipient, destination domain, destination caller, max fee,
    then the hook data envelope.
    """
    for log in logs or []:
        topics = log.get("topics") or []
        if len(topics) < 4 or str(topics[0]).lower() != DEPOSIT_FOR_BURN_TOPIC:
            continue
        if messenger and (log.get("address") or "").lower() != messenger.lower():
            continue
        words = _words(log.get("data"))
        if len(words) < 6:
            continue
        amount = _int("0x" + words[0])
        return {
            "action": "depositForBurn",
            "amount": amount,
            "amount_usdc": amount / 1e6,
            "burn_token": _address_from(topics[1][2:] if topics[1].startswith("0x") else topics[1]),
            "depositor": _address_from(topics[2][2:] if topics[2].startswith("0x") else topics[2]),
            "min_finality_threshold": _int(topics[3]),
            "mint_recipient": _address_from(words[1]),
            "destination_domain": _int("0x" + words[2]),
            "destination_caller": _address_from(words[4]),
            "max_fee": _int("0x" + words[5]),
            "witness": "the TokenMessenger's own DepositForBurn event",
            "log": {"address": log.get("address"),
                    "block": _int(log.get("blockNumber")),
                    "index": _int(log.get("logIndex"))},
        }
    return None


def burn_event(logs: list, token: str | None = None) -> dict | None:
    """What the token itself says was burned, and from whose balance."""
    burned = None
    moved = None
    for log in logs or []:
        topics = log.get("topics") or []
        if not topics:
            continue
        if token and (log.get("address") or "").lower() != token.lower():
            continue
        if str(topics[0]).lower() == BURN_TOPIC and len(topics) >= 2:
            burned = {
                "amount": _int(log.get("data")),
                "burner": _address_from(topics[1][2:] if topics[1].startswith("0x") else topics[1]),
                "witness": "the token minter's own Burn event",
            }
        elif str(topics[0]).lower() == TRANSFER_TOPIC and len(topics) >= 3:
            to_addr = _address_from(topics[2][2:] if topics[2].startswith("0x") else topics[2])
            value = _int(log.get("data"))
            event = {"amount": value, "amount_usdc": value / 1e6,
                     "from": _address_from(topics[1][2:] if topics[1].startswith("0x") else topics[1]),
                     "to": to_addr, "burn": to_addr is None,
                     "witness": "the token's own Transfer event"}
            if event["burn"]:
                moved = event
    if burned and moved:
        return {**burned, "amount_usdc": burned["amount"] / 1e6, "burn_transfer": moved}
    return burned or moved


def message_sent(receipt: dict, transmitter_address: str | None) -> dict | None:
    """The bytes the destination will be handed, taken from the transmitter's event."""
    from .inflight import decode_message_sent

    for log in receipt.get("logs") or []:
        if (transmitter_address
                and (log.get("address") or "").lower() != transmitter_address.lower()):
            continue
        topics = [str(t).lower() for t in (log.get("topics") or [])]
        if protocol.MESSAGE_SENT_TOPIC.lower() not in topics:
            continue
        announced = decode_message_sent(log.get("data") or "0x")
        if not announced:
            continue
        return {
            "message": announced,
            "bytes": (len(announced) - 2) // 2,
            "witness": "the transmitter's own MessageSent event",
            "rail_decode": _try_rail_decode(announced),
        }
    return None


def _try_rail_decode(message_hex: str) -> dict | None:
    """This repository's own decoder, offered as a second opinion or not at all.

    The extended layout's offsets do not line up with every message the live
    deployment emits — the deposit event's own numbers are the check, and a parse
    that disagrees with them is not evidence of anything. When it fails that
    check, the fields come from the attestation service's decode instead, and the
    disagreement is reported rather than hidden.
    """
    try:
        parsed = protocol.parse(message_hex)
    except Exception:  # noqa: BLE001
        return None
    if parsed.get("source_domain") not in (0, 2, 3, 6, 7):
        return None
    amount = parsed.get("amount")
    if not isinstance(amount, int) or amount <= 0 or amount > 10 ** 12:
        return None
    return parsed


def agree(*candidates: dict | None) -> dict:
    """Where two witnesses speak, do they agree?

    Each candidate is a dict with an `amount`. The result names the amount that
    more than one of them supports, and lists the disagreements instead of
    picking a winner quietly.
    """
    spoken = [(c.get("witness"), c.get("amount")) for c in candidates if c and c.get("amount")]
    if not spoken:
        return {"amount": None, "witnesses": [], "disagreements": []}
    tally: dict = {}
    for _, amount in spoken:
        tally[amount] = tally.get(amount, 0) + 1
    agreed, votes = max(tally.items(), key=lambda item: item[1])
    return {
        "amount": agreed if votes > 1 or len(spoken) == 1 else None,
        "witnesses": [{"witness": w, "amount": a} for w, a in spoken],
        "disagreements": [{"witness": w, "amount": a} for w, a in spoken if a != agreed],
    }
