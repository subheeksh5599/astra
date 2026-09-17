"""Finding transfers that are still in flight.

A transfer exists on the source chain the moment the protocol emits its message.
Whether it is *finished* is a question about the destination chain. So the rail
looks at both ends and reports the pair, rather than keeping a list of its own
and hoping.

Discovery reads the protocol's own log stream on the source chain. It does not
trust an indexer's view of "pending" and it does not need one: the message is in
the log, and the destination's answer is one simulation away.
"""
from __future__ import annotations

from . import classifier, protocol
from .config import CHAIN_IDS, TRANSMITTERS, transmitter
from .keeperhub import KeeperHub
from .rail import RECEIVE_ABI
from .rpc import Rpc


def decode_message_sent(data: str) -> str | None:
    """Pull the message out of the log's ABI envelope.

    The protocol emits its message as a dynamic bytes argument, so the log data
    is an offset, a length, then the bytes. Reading the envelope is what stops a
    rail from announcing every unrelated payload on the address as a transfer.
    """
    if not data.startswith("0x") or len(data) < 130:
        return None
    try:
        offset = int(data[2:66], 16)
        length = int(data[66:130], 16)
    except ValueError:
        return None
    if offset != 32 or length == 0:
        return None
    start = 2 + offset * 2
    end = start + length * 2
    if end > len(data):
        return None
    return "0x" + data[start:end]


def scan(rpc: Rpc, source_domain: int, blocks: int, deployment: str = "v2") -> list:
    """Every message the protocol announced in the last `blocks` blocks."""
    address = TRANSMITTERS[deployment][source_domain]
    head = rpc.block_number()
    from_block = max(1, head - blocks)
    logs = rpc.get_logs(address, [], from_block, head)

    found = []
    for log in logs:
        announced = decode_message_sent(log.get("data") or "0x")
        if not announced:
            continue
        found.append({
            "source_domain": source_domain,
            "burn_tx": log.get("transactionHash"),
            "block": int(log.get("blockNumber", "0x0"), 16),
            "announced_message": announced,
            "deployment": deployment,
        })
    found.sort(key=lambda item: item["block"], reverse=True)
    return found


def state_of_transfer(kh: KeeperHub, attestation, env: dict, transfer: dict,
                      wallet: str | None = None) -> dict:
    """What the two chains say about one transfer right now.

    `finished` is read from the destination, never inferred: a delivery that has
    already happened is the one case where the destination refuses the call, and
    that refusal is the evidence.
    """
    att = attestation.by_transaction(transfer["source_domain"], transfer["burn_tx"])
    record = dict(transfer)
    record["attestation_state"] = att.get("state")
    record["attestation_status"] = att.get("status")
    record.pop("message", None)

    parsed = None
    if att.get("message"):
        try:
            parsed, disagreements = protocol.merge(protocol.parse(att["message"]), att.get("decoded"))
            record["disagreements"] = disagreements
        except protocol.BadMessage:
            parsed = protocol.from_service(att["decoded"]) if att.get("decoded") else None
            record["disagreements"] = []
    else:
        record["disagreements"] = []
    record["parsed"] = parsed

    preflight = None
    watched = parsed.get("destination_domain") in CHAIN_IDS if parsed else False
    if watched and att.get("state") == "final":
        destination = parsed["destination_domain"]
        status, body = kh.simulate(CHAIN_IDS[destination], transmitter(env, destination),
                                   "receiveMessage", RECEIVE_ABI,
                                   [att["message"], att["attestation"]])
        preflight = classifier.interpret_preflight(body)
        preflight["http"] = status

    request = {
        "source_domain": transfer["source_domain"],
        "destination_domain": (parsed or transfer).get("destination_domain"),
        "burn_tx": transfer["burn_tx"],
    }
    observed = {"attestation": att, "message": parsed, "preflight": preflight,
                "executing_wallet": wallet, "disagreements": record.get("disagreements") or []}
    record["decision"] = classifier.decide(request, observed)
    record["finished"] = bool(preflight and not preflight.get("ok")
                              and preflight.get("already_delivered"))
    return record
