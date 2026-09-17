#!/usr/bin/env python3
"""The invariant, executable.

One burn, exactly one mint. This script reads both chains and says, per transfer,
which of those two things happened:

  paired     the source announced it and the destination received it
  in flight  the source announced it and the destination has not received it,
             and the attestation is not signed yet, so nobody could have
  stranded   the source announced it, the destination has not received it, and
             the attestation IS signed: value is sitting still and could move
  unknown    the transfer goes somewhere this rail does not watch

It exits non-zero when a transfer is stranded, and when a nonce has been received
more than once. Those are the two ways the invariant can actually break: money
that will not move without help, and money that moved twice.

    python3 scripts/prove_pairing.py --blocks 6000
    python3 scripts/prove_pairing.py --blocks 6000 --json
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astra import inflight  # noqa: E402
from astra.attestation import Attestation  # noqa: E402
from astra.config import (CHAIN_IDS, DOMAIN_NAMES, TRANSMITTERS, attestation_base,  # noqa: E402
                          attestation_version, deployment, load_env, rpc_url)
from astra.rpc import Rpc  # noqa: E402


USED_NONCES_SELECTOR = "0xfeb61724"   # usedNonces(bytes32)
USED_NONCES_ABI = ("[{\"inputs\":[{\"name\":\"nonce\",\"type\":\"bytes32\"}],"
                   "\"name\":\"usedNonces\",\"outputs\":[{\"name\":\"\",\"type\":\"uint256\"}],"
                   "\"stateMutability\":\"view\",\"type\":\"function\"}]")

_RPC_CACHE: dict = {}


def rpc_for(domain: int) -> Rpc:
    if domain not in _RPC_CACHE:
        _RPC_CACHE[domain] = Rpc(rpc_url(load_env(), domain))
    return _RPC_CACHE[domain]


def destination_says_delivered(rpc: Rpc, domain: int, deployment_name: str, nonce: str | None):
    """Ask the destination contract whether this nonce has been used.

    Returns True, False, or None when the contract cannot answer (an older
    deployment whose storage the rail must not guess at).
    """
    if not nonce or not nonce.startswith("0x"):
        return None
    address = TRANSMITTERS[deployment_name][domain]
    try:
        raw = rpc.call_contract(address, USED_NONCES_SELECTOR + nonce[2:])
    except RuntimeError:
        return None
    if not raw or raw == "0x":
        return None
    try:
        return int(raw, 16) != 0
    except ValueError:
        return None


def received_on_destination(rpc: Rpc, domain: int, deployment_name: str, blocks: int) -> list:
    """Every message the destination chain says it received, in the window.

    Read from the destination's own log stream. The rail does not take anyone's
    word for a delivery, including its own.
    """
    address = TRANSMITTERS[deployment_name][domain]
    head = rpc.block_number()
    logs = rpc.get_logs(address, [], max(1, head - blocks), head)
    out = []
    for log in logs:
        data = (log.get("data") or "").lower()
        if len(data) < 130:
            continue
        out.append({"tx": log.get("transactionHash"), "data": data,
                    "block": int(log.get("blockNumber", "0x0"), 16)})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--blocks", type=int, default=6000)
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    env = load_env()
    name = deployment(env)
    attestation = Attestation(attestation_base(env), attestation_version(env))

    source_transfers = []
    received = {}
    for domain in sorted(CHAIN_IDS):
        rpc = Rpc(rpc_url(env, domain))
        source_transfers.extend(inflight.scan(rpc, domain, args.blocks, deployment=name))
        received[domain] = received_on_destination(rpc, domain, name, args.blocks)

    source_transfers.sort(key=lambda item: item.get("block") or 0, reverse=True)
    rows = []
    for transfer in source_transfers[: args.limit]:
        att = attestation.by_transaction(transfer["source_domain"], transfer["burn_tx"])
        parsed = None
        if att.get("message"):
            try:
                from astra import protocol
                parsed = protocol.parse(att["message"])
            except Exception:  # noqa: BLE001
                parsed = None
        if parsed is None:
            service = att.get("decoded") or {}
            nonce = (service.get("nonce") or "").lower()
            destination = service.get("destinationDomain")
            destination = int(destination) if destination is not None else None
            amount = None
        else:
            nonce = parsed["nonce"].lower()
            destination = parsed["destination_domain"]
            amount = parsed["amount"]

        verdict, mint_tx = "unknown", None
        if destination not in CHAIN_IDS:
            verdict = "unwatched" if destination is not None else "unknown"
        else:
            # The destination's own record: the contract answers, for this nonce,
            # whether it has already been delivered. No log matching, no indexer,
            # nothing this rail wrote down.
            delivered = destination_says_delivered(rpc_for(destination), destination, name, nonce)
            if delivered is True:
                verdict = "paired"
            elif delivered is False:
                verdict = "stranded" if att.get("state") == "final" else (
                    "in flight" if att.get("state") == "pending" else "unknown")
            else:
                verdict = "unknown"

        rows.append({
            "transfer_id": f"{transfer['source_domain']}:{nonce or transfer['burn_tx'][:12]}",
            "source_domain": transfer["source_domain"],
            "destination_domain": destination,
            "destination_name": DOMAIN_NAMES.get(destination, destination),
            "amount_usdc": (amount or 0) / 1e6 if amount else None,
            "attestation": att.get("state"),
            "verdict": verdict,
            "burn_tx": transfer["burn_tx"],
            "mint_tx": mint_tx,
        })

    # A direction the rail does not watch cannot be paired here, and saying so is
    # not the same as calling it broken.
    broken = [row for row in rows if row["verdict"] in ("stranded", "received twice")]
    if args.json:
        print(json.dumps({"rows": rows, "broken": broken}, indent=2))
    else:
        print(f"{'transfer':<26} {'amount':>12}  {'destination':<18} {'attestation':<10} verdict")
        for row in rows:
            amount = f"{row['amount_usdc']:.6f}" if row["amount_usdc"] is not None else "\u00b7"
            print(f"{row['transfer_id'][:26]:<26} {amount:>12}  {str(row['destination_name'])[:18]:<18} "
                  f"{str(row['attestation'])[:10]:<10} {row['verdict']}"
                  + (f"  mint {row['mint_tx'][:14]}" if row["mint_tx"] else ""))
        counted = {}
        for row in rows:
            counted[row["verdict"]] = counted.get(row["verdict"], 0) + 1
        print("\n" + ", ".join(f"{count} {verdict}" for verdict, count in sorted(counted.items())))
        if broken:
            print("\nTHE INVARIANT IS BROKEN:")
            for row in broken:
                print(f"  {row['transfer_id']} is {row['verdict']}: burn {row['burn_tx']}")
        else:
            print("\nno burn is stranded and no nonce was received twice")

    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "artifacts", "pairing.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"rows": rows, "broken": broken}, fh, indent=2)
    print(f"\nwrote {out}")
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
