"""The invariant, and the two ways it can break.

One burn, exactly one mint. A transfer is a pair: the source chain announcing a
message, and a destination chain receiving it. This module answers, per transfer,
which of those two happened, and it answers from the destinations themselves.

    paired          the destination's own receipt event names this transfer
    in flight       nothing has received it and the attestation is not signed, so
                    nobody could have
    stranded        the attestation IS signed and nothing has received it: value
                    is sitting still and could move
    unwatched       the transfer goes somewhere this rail does not watch, so the
                    rail says it cannot tell rather than calling it broken
    unknown         the destination answered nothing the rail can read

Delivery is read from the destination's receipt event, keyed by the source domain
and the nonce it emitted -- a key both live deployments publish, checked against
transfers this rail delivered itself. That is why the older deployment can be
paired here even though its storage does not answer the newer contract's replay
question: the event is the protocol's own statement, and it is the same statement
on both.

A second break is counted too: the destination naming the same nonce twice. That
is the other half of "exactly one mint", and it is a count, not a claim.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from . import inflight, protocol
from .attestation import Attestation
from .config import (CHAIN_IDS, DOMAIN_NAMES, USDC, attestation_base, attestation_version,
                     deployment, load_env, rpc_url, supports)
from .rpc import Rpc

# The destination's receipt event: source domain, the messenger that called it,
# then an offset to the dynamic argument. The nonce rides in the third topic.
RECEIVED_EVENT_TOPIC_INDEX = 2

# transfer(address,address,uint256), the token's own statement that value moved.
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

VERDICTS = ("paired", "in flight", "stranded", "unwatched", "unknown")


def transfer_key(source_domain: int, nonce) -> str:
    """One transfer's identity: its source domain and its nonce.

    The nonce is normalised to an integer because the two deployments publish it
    in different widths (a counter and a 32-byte word) while meaning the same
    number. Anything that is not a number is kept as it was written rather than
    forced into one.
    """
    if isinstance(nonce, int):
        return f"{source_domain}:{nonce}"
    text = str(nonce or "").strip()
    if text.startswith("0x"):
        try:
            return f"{source_domain}:{int(text, 16)}"
        except ValueError:
            return f"{source_domain}:{text.lower()}"
    return f"{source_domain}:{text}"


def receipts(rpc: Rpc, domain: int, deployment_name: str, blocks: int) -> dict:
    """What the destination chain says it has received, counted by transfer.

    Read from the destination's own log stream: no indexer, no bookkeeping of
    this rail's own, and nothing taken on trust. The count matters as much as the
    presence -- a nonce seen twice is the other way the invariant breaks.
    """
    from .config import TRANSMITTERS
    address = TRANSMITTERS[deployment_name].get(domain)
    if address is None:
        return {}
    head = rpc.block_number()
    logs = rpc.get_logs(address, [], max(1, head - blocks), head)
    counted: dict = {}
    for log in logs:
        topics = log.get("topics") or []
        if len(topics) <= RECEIVED_EVENT_TOPIC_INDEX:
            continue
        try:
            nonce = int(topics[RECEIVED_EVENT_TOPIC_INDEX], 16)
        except (TypeError, ValueError):
            continue
        data = (log.get("data") or "")[2:]
        try:
            source_domain = int(data[:64], 16) if len(data) >= 64 else None
        except ValueError:
            source_domain = None
        if source_domain is None:
            continue
        key = transfer_key(source_domain, nonce)
        counted.setdefault(key, []).append(log.get("transactionHash"))
    return counted


def minted_amount(rpc: Rpc, mint_tx: str, recipient: str, token: str) -> int | None:
    """How much value the destination actually minted, read from the token.

    The invariant is not only "a mint happened": it is "the value that arrived is
    the value that left, less the fee the protocol charged for it". The token's own
    transfer event is what says so, and it is the only thing that can.
    """
    if not mint_tx:
        return None
    receipt = rpc.transaction_receipt(mint_tx)
    if not receipt or receipt.get("status") not in ("0x1", 1):
        return None
    total = 0
    seen = False
    for log in receipt.get("logs") or []:
        if (log.get("address") or "").lower() != token.lower():
            continue
        topics = log.get("topics") or []
        if len(topics) < 3 or topics[0].lower() != TRANSFER_TOPIC:
            continue
        if "0x" + topics[2][-40:].lower() != "0x" + recipient[-40:].lower():
            continue
        try:
            total += int(log.get("data"), 16)
            seen = True
        except (TypeError, ValueError):
            continue
    return total if seen else None


def verdict(attestation_state, delivered, watched: bool) -> str:
    """The invariant, as one function over facts read from the two chains."""
    if not watched:
        return "unwatched"
    if delivered:
        return "paired"
    if attestation_state == "final":
        return "stranded"
    if attestation_state == "pending":
        return "in flight"
    return "unknown"


def collect(env: dict | None = None, blocks: int = 6000, limit: int = 20,
            rpcs: dict | None = None, attestation=None, progress=None,
            watched_only: bool = False) -> dict:
    """Walk every transfer the source chains announced, and pair each one.

    Returns {"rows": [...], "duplicates": [...], "domains": {...}} so a caller can
    print it, journal it, or fail a job on it.
    """
    env = env or load_env()
    name = deployment(env)
    attestation = attestation or Attestation(attestation_base(env), attestation_version(env))
    rpcs = rpcs or {}

    def rpc_for(domain: int) -> Rpc:
        if domain not in rpcs:
            rpcs[domain] = Rpc(rpc_url(env, domain))
        return rpcs[domain]

    domains = [domain for domain in sorted(CHAIN_IDS) if supports(env, domain)]
    if progress:
        progress(f"reading {len(domains)} chains: {', '.join(DOMAIN_NAMES.get(d, str(d)) for d in domains)}")

    def read_chain(domain: int):
        """One chain's announcements and its receipts. Independent of the others,
        so a slow endpoint costs its own chain and not the whole pass."""
        try:
            return domain, (inflight.scan(rpc_for(domain), domain, blocks, deployment=name),
                            receipts(rpc_for(domain), domain, name, blocks))
        except Exception:  # noqa: BLE001 - one endpoint must not blind the others
            return domain, ([], {})

    with ThreadPoolExecutor(max_workers=max(1, len(domains))) as pool:
        read = dict(pool.map(read_chain, domains))

    sources: list = []
    received: dict = {}
    for domain in domains:
        announced, counted = read.get(domain, ([], {}))
        sources.extend(announced)
        received[domain] = counted

    sources.sort(key=lambda item: item.get("block") or 0, reverse=True)

    def pair_one(transfer: dict) -> dict:
        att = attestation.by_transaction(transfer["source_domain"], transfer["burn_tx"])
        parsed = None
        if att.get("message"):
            try:
                parsed = protocol.parse(att["message"])
            except protocol.BadMessage:
                parsed = None
        if parsed is None and att.get("decoded"):
            parsed = protocol.from_service(att["decoded"])

        if parsed is None:
            service = att.get("decoded") or {}
            nonce = service.get("nonce")
            destination = service.get("destinationDomain")
            destination = int(destination) if destination is not None else None
            amount = None
        else:
            nonce = parsed.get("nonce")
            destination = parsed.get("destination_domain")
            amount = parsed.get("amount")

        watched = destination in CHAIN_IDS and supports(env, destination)
        seen = received.get(destination) or {}
        seen = seen.get(transfer_key(transfer["source_domain"], nonce)) or []
        state = att.get("state")

        # A delivery is a pair of numbers: what left, and what arrived. The dif-
        # ference has to be the protocol's own fee, which the message carries.
        fee = (parsed or {}).get("fee_executed")
        expected = (parsed or {}).get("amount")
        if expected is not None and fee is not None:
            expected -= fee
        minted = None
        if seen and expected is not None and destination is not None:
            minted = minted_amount(rpc_for(destination), seen[0], (parsed or {}).get("mint_recipient") or "",
                                   USDC.get(destination, ""))
        return {
            "transfer_id": transfer_key(transfer["source_domain"], nonce if nonce is not None
                                        else transfer["burn_tx"][:12]),
            "source_domain": transfer["source_domain"],
            "destination_domain": destination,
            "destination_name": DOMAIN_NAMES.get(destination, destination),
            "nonce": nonce,
            "amount_usdc": (amount / 1e6) if amount else None,
            "layout": (parsed or {}).get("layout"),
            "attestation": state,
            "verdict": verdict(state, seen, watched),
            "delivered_count": len(seen),
            "mint_txs": seen,
            "expected_amount": expected,
            "fee_executed": fee,
            "minted_amount": minted,
            "value_matches": (None if minted is None or expected is None else minted == expected),
            "burn_tx": transfer["burn_tx"],
        }

    # Each transfer is paired from its own two chains, so the pairs are assembled
    # in parallel windows and kept in the order they were read: the newest first,
    # whoever happens to answer first.
    rows: list = []
    wanted = sources[:limit]
    with ThreadPoolExecutor(max_workers=min(4, len(wanted) or 1)) as pool:
        for row in pool.map(pair_one, wanted):
            rows.append(row)

    if watched_only:
        # A transfer to a chain this rail cannot reach is honest output but it is
        # not work: every destination it *can* reach is what a pass is for.
        rows = [row for row in rows if row["verdict"] != "unwatched"]
    duplicates = [row for row in rows if row["delivered_count"] > 1]
    return {"rows": rows, "duplicates": duplicates,
            "domains": {str(domain): len(counted) for domain, counted in received.items()}}


def broken(result: dict) -> list:
    """The rows that show the invariant broken, however it broke.

    Money that can move and has not, money that moved twice, and value that
    arrived short of what left by more than the protocol said it would charge.
    """
    return ([row for row in result.get("rows", []) if row["verdict"] == "stranded"]
            + [row for row in result.get("rows", []) if row.get("value_matches") is False]
            + list(result.get("duplicates") or []))


def summarise(result: dict) -> str:
    counted: dict = {}
    for row in result.get("rows", []):
        counted[row["verdict"]] = counted.get(row["verdict"], 0) + 1
    return ", ".join(f"{count} {name}" for name, count in sorted(counted.items())) or "no transfers"
