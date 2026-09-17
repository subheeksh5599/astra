#!/usr/bin/env python3
"""Finish one in-flight transfer, or refuse it by name.

    python3 scripts/astra_complete.py --burn-tx 0x... --source 6 --destination 0
    python3 scripts/astra_complete.py --burn-tx 0x... --source 6 --destination 0 --wait 300

`--wait` adds the deferral branch: while the attestation is not signed the rail
waits and says so instead of marching into a delivery that cannot happen.

The run prints a receipt and writes it to artifacts/receipts/.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astra.config import load_env  # noqa: E402
from astra.rail import Rail  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--burn-tx", required=True, help="the source transaction that created the transfer")
    ap.add_argument("--source", type=int, default=6, help="source domain id (6 = Base Sepolia)")
    ap.add_argument("--destination", type=int, default=0, help="destination domain id (0 = Ethereum Sepolia)")
    ap.add_argument("--wait", type=int, default=0, help="defer up to this many seconds for the attestation")
    ap.add_argument("--expect-recipient", default=None)
    ap.add_argument("--expect-amount", type=int, default=None)
    args = ap.parse_args()

    request = {
        "source_domain": args.source,
        "destination_domain": args.destination,
        "burn_tx": args.burn_tx,
    }
    expect = {}
    if args.expect_recipient:
        expect["recipient"] = args.expect_recipient
    if args.expect_amount:
        expect["amount"] = args.expect_amount
    if expect:
        request["expect"] = expect

    rail = Rail(load_env())
    if args.wait:
        receipt = rail.run(request, max_wait=args.wait, interval=10,
                           on_wait=lambda att: print(f"   deferred: attestation {att.get('state')}"))
    else:
        receipt = rail.run(request)

    print(f"\ntransfer   {receipt['transfer_id']}")
    print(f"decision   {receipt['decision']['action'].upper()} / {receipt['decision']['reason']}")
    print(f"detail     {receipt['decision']['detail']}")
    if receipt.get("transaction_hash"):
        print(f"executed   {receipt['transaction_hash']}")
        print(f"           {receipt.get('transaction_link')}")
    path = Rail.write_receipt(receipt)
    print(f"receipt    {path}")
    print(json.dumps(receipt["decision"], indent=2))
    return 0 if receipt["decision"]["action"] != "refuse" else 5


if __name__ == "__main__":
    sys.exit(main())
