#!/usr/bin/env python3
"""The invariant, run once and reported.

One burn, exactly one mint. This reads every transfer the source chains announced
in the window, asks each destination chain what it received, and prints one line
per transfer. It exits non-zero when a transfer is stranded (value that can move
and has not) or when a destination named the same nonce twice (value that moved
twice), so it can be wired to a job whose failure is itself the evidence.

    python3 scripts/prove_pairing.py --blocks 6000
    python3 scripts/prove_pairing.py --blocks 6000 --json
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astra import pairing  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=3600, help="how far back to read, in time")
    ap.add_argument("--blocks", type=int, default=0, help="override the window in source blocks")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    result = pairing.collect(blocks=args.blocks, limit=args.limit, seconds=args.seconds,
                             progress=None if args.quiet else lambda line: print(line, file=sys.stderr))
    rows, broken = result["rows"], pairing.broken(result)

    if args.json:
        print(json.dumps({"rows": rows, "broken": broken, "domains": result["domains"]}, indent=2))
    else:
        print(f"{'transfer':<26} {'amount':>12}  {'destination':<18} {'attestation':<10} verdict")
        for row in rows:
            amount = f"{row['amount_usdc']:.6f}" if row["amount_usdc"] is not None else "\u00b7"
            value = ""
            if row.get("minted_amount") is not None:
                value = f"  minted {row['minted_amount'] / 1e6:.6f} of {row['expected_amount'] / 1e6:.6f}"
            print(f"{row['transfer_id'][:26]:<26} {amount:>12}  {str(row['destination_name'])[:18]:<18} "
                  f"{str(row['attestation'])[:10]:<10} {row['verdict']}"
                  + (f"  mint {row['mint_txs'][0][:14]}" if row.get("mint_txs") else "") + value)
        print("\n" + pairing.summarise(result))
        if broken:
            print("\nTHE INVARIANT IS BROKEN:")
            for row in broken:
                if row["delivered_count"] > 1:
                    print(f"  {row['transfer_id']} was received {row['delivered_count']} times: "
                          f"{', '.join(tx[:18] for tx in row['mint_txs'])}")
                else:
                    print(f"  {row['transfer_id']} is stranded: burn {row['burn_tx']}")
                    if row.get("mint_txs"):
                        print(f"    its mint is {row['mint_txs'][0]}")
        else:
            print("\nno burn is stranded and no nonce was received twice")

    out = os.path.join(ROOT, "artifacts", "pairing.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"rows": rows, "broken": broken, "domains": result["domains"]}, fh, indent=2)
    if not args.quiet:
        print(f"\nwrote {out}")
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
