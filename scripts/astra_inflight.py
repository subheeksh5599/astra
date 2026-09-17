#!/usr/bin/env python3
"""List the transfers that are in flight, and what the rail would do about each.

    python3 scripts/astra_inflight.py --blocks 12000 --limit 12
    python3 scripts/astra_inflight.py --blocks 500 --source 6 --json

Read-only: it exercises the same classifier the completing path uses, and moves
nothing. Each row says whether the transfer is deliverable, early, or finished.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astra import inflight  # noqa: E402
from astra.attestation import Attestation  # noqa: E402
from astra.config import (DOMAIN_NAMES, attestation_base, attestation_version,  # noqa: E402
                          deployment, load_env, rpc_url)
from astra.keeperhub import KeeperHub  # noqa: E402
from astra.rpc import Rpc  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", type=int, default=6)
    ap.add_argument("--blocks", type=int, default=9000,
                    help="how far back to read the source chain's log stream")
    ap.add_argument("--limit", type=int, default=12)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    env = load_env()
    deployment_name = deployment(env)
    rpc = Rpc(rpc_url(env, args.source))
    kh = KeeperHub(env["KH_API_KEY"])
    attestation = Attestation(attestation_base(env), attestation_version(env))
    wallet = kh.wallet()

    print(f"reading {args.blocks} blocks of {DOMAIN_NAMES.get(args.source, args.source)} "
          f"on the {deployment_name} deployment")
    transfers = inflight.scan(rpc, args.source, args.blocks, deployment=deployment_name)
    print(f"the protocol announced {len(transfers)} transfers")

    rows = []
    for transfer in transfers[: args.limit]:
        record = inflight.state_of_transfer(kh, attestation, {**env, "ASTRA_DEPLOYMENT": deployment_name},
                                            transfer, wallet=wallet)
        parsed = record.get("parsed") or {}
        row = {
            "burn_tx": record["burn_tx"],
            "transfer_id": f"{record['source_domain']}:{record.get('nonce')}",
            "amount_usdc": (parsed.get("amount") or record.get("amount") or 0) / 1e6,
            "destination": DOMAIN_NAMES.get(parsed.get("destination_domain"), parsed.get("destination_domain")),
            "recipient": parsed.get("mint_recipient") or record.get("mint_recipient"),
            "attestation": record["attestation_state"],
            "decision": record["decision"]["action"],
            "reason": record["decision"]["reason"],
            "finished": record["finished"],
        }
        rows.append(row)
        if not args.json:
            print(f"  {row['transfer_id'][:14]}.. {row['amount_usdc']:>8.6f} -> {row['destination']:<16} "
                  f"attestation={row['attestation']:<10} {row['decision']:<8} {row['reason']}"
                  f"{'  (already delivered)' if row['finished'] else ''}")

    if args.json:
        print(json.dumps(rows, indent=2))

    folder = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "artifacts")
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "inflight.json"), "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
