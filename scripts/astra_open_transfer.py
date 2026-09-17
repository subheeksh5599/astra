#!/usr/bin/env python3
"""Open a cross-chain transfer from a wallet we hold, and deliberately leave it
in flight.

This is the paying side. It approves and burns on the source chain, with an
optional destination caller, and stops there: the transfer is now in flight and
the rail's job begins. It supports the two deployments of the protocol, so a
transfer can be created either way and the same rail will read it.

    python3 scripts/astra_open_transfer.py --amount 1
    python3 scripts/astra_open_transfer.py --amount 1 --caller 0xSomeoneElse
    python3 scripts/astra_open_transfer.py --amount 1 --deployment v1

The signing key is read from the environment (BUYER_KEY) and never printed.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astra.config import DOMAIN_NAMES, ROOT, USDC, load_env, messenger, rpc_url  # noqa: E402

CAST = os.path.expanduser("~/.foundry/bin/cast")
FAST_THRESHOLD = 1000
STANDARD_THRESHOLD = 2000


def cast(env, url, sub, *args):
    out = subprocess.run([CAST, sub, "--rpc-url", url, *args], capture_output=True, text=True,
                         env={**os.environ, **env})
    if out.returncode != 0:
        raise SystemExit(f"cast {sub} failed: {out.stderr[:500]}")
    return out.stdout.strip()


def send(env, url, key_name, address, to, sig, *params):
    """Send with an explicit nonce.

    Public testnet endpoints answer a nonce query with a slightly stale value,
    which turns a second write in the same script into a replacement-priced
    failure. Reading the pending nonce and passing it explicitly avoids that.
    """
    last = ""
    for attempt in range(3):
        nonce = cast(env, url, "nonce", address, "--block", "pending").split()[0]
        out = subprocess.run([CAST, "send", "--rpc-url", url, "--private-key", env[key_name],
                              "--nonce", nonce, "--json", to, sig, *params],
                             capture_output=True, text=True, env={**os.environ, **env})
        if out.returncode == 0:
            payload = json.loads(out.stdout)
            return payload.get("transactionHash"), payload.get("status")
        last = out.stderr.strip()[:300]
        time.sleep(2 + attempt)
    raise SystemExit(f"send {sig} failed after 3 attempts: {last}")


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
    if args.deployment:
        env["ASTRA_DEPLOYMENT"] = args.deployment
    url = rpc_url(env, args.source)
    token = USDC[args.source]
    target = messenger(env, args.source)
    payer = env["BUYER"]
    amount = int(float(args.amount) * 1_000_000)
    caller32 = "0x" + "0" * 64 if not args.caller else "0x" + "0" * 24 + args.caller[2:].lower()
    threshold = args.threshold if args.threshold is not None else (
        FAST_THRESHOLD if args.deployment != "v1" else STANDARD_THRESHOLD)

    allowance = int(cast(env, url, "call", token, "allowance(address,address)(uint256)",
                         payer, target).split()[0])
    if allowance < amount:
        tx, status = send(env, url, "BUYER_KEY", payer, token, "approve(address,uint256)", target, str(amount))
        print(f"approve -> {tx} status={status}")

    recipient32 = "0x" + "0" * 24 + payer[2:].lower()
    if args.deployment == "v1":
        tx, status = send(env, url, "BUYER_KEY", payer, target,
                          "depositForBurn(uint256,uint32,bytes32,address)",
                          str(amount), str(args.destination), recipient32, token)
    else:
        max_fee = max(1, amount // 1000)
        tx, status = send(env, url, "BUYER_KEY", payer, target,
                          "depositForBurn(uint256,uint32,bytes32,address,bytes32,uint256,uint32)",
                          str(amount), str(args.destination), recipient32, token,
                          caller32, str(max_fee), str(threshold))
    print(f"burn    -> {tx} status={status}")
    print(f"          source {DOMAIN_NAMES[args.source]} -> {DOMAIN_NAMES[args.destination]}, "
          f"caller {'anyone' if not args.caller else args.caller}, threshold {threshold}")

    record = {"burnTx": tx, "source": args.source, "destination": args.destination,
              "caller": args.caller, "deployment": args.deployment or "v2",
              "amount": amount, "payer": payer, "maxFee": max(1, amount // 1000)}
    out = os.path.join(ROOT, "artifacts", "last-transfer.json")
    json.dump(record, open(out, "w", encoding="utf-8"), indent=2)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
