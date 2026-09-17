#!/usr/bin/env python3
"""Open a cross-chain transfer from a wallet this machine holds.

This is the paying side, and it stops as soon as the transfer is in flight: the
rail's job begins where this ends. It supports both live deployments of the
protocol, so a transfer can be created either way and the same rail will read it.

    python3 scripts/astra_open_transfer.py --amount 1
    python3 scripts/astra_open_transfer.py --amount 1 --caller 0xSomeoneElse
    python3 scripts/astra_open_transfer.py --amount 1 --deployment v1
    python3 scripts/astra_open_transfer.py --amount 1 --source 6 --destination 0

The signing key is read from the environment and never printed. The signing itself
lives in astra/payer.py, which the control surface uses as well, so a transfer made
from the page and a transfer made from here take the same path.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astra import payer  # noqa: E402
from astra.config import DOMAIN_NAMES, ROOT, load_env  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--amount", default="1", help="amount in whole tokens")
    ap.add_argument("--source", type=int, default=6)
    ap.add_argument("--destination", type=int, default=0)
    ap.add_argument("--caller", default=None, help="address allowed to complete; default anyone")
    ap.add_argument("--deployment", default=None, choices=["v1", "v2"])
    ap.add_argument("--threshold", type=int, default=None, help="fast=1000, standard=2000")
    args = ap.parse_args()

    env = load_env()
    deployment = args.deployment or env.get("ASTRA_DEPLOYMENT", "v2")
    if args.deployment:
        env["ASTRA_DEPLOYMENT"] = args.deployment

    try:
        transfer = payer.open_transfer(env, args.amount, args.source, args.destination,
                                       caller=args.caller, deployment=deployment,
                                       threshold=args.threshold,
                                       on_step=lambda message: print(f"  {message}"))
    except (payer.PayerUnavailable, ValueError, RuntimeError) as exc:
        print(f"cannot open the transfer: {exc}", file=sys.stderr)
        return 1

    if transfer.get("approve_tx"):
        print(f"approve -> {transfer['approve_tx']}")
    print(f"burn    -> {transfer['burn_tx']} status={transfer['burn_status']}")
    print(f"          {DOMAIN_NAMES[transfer['source_domain']]} -> "
          f"{DOMAIN_NAMES[transfer['destination_domain']]}, "
          f"caller {transfer['caller'] or 'anyone'}, threshold {transfer['threshold']}")

    out = os.path.join(ROOT, "artifacts", "last-transfer.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(transfer, fh, indent=2)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
