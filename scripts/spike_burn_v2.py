#!/usr/bin/env python3
"""Astra spike, stage 2b: the same transfer, created against the protocol's
later deployment, which can be attested before full finality.

The earlier deployment waits for the source chain to finalise before an
attestation is signed, and on a testnet that can take tens of minutes. The later
deployment takes a fee cap and a finality threshold, so a transfer can be
attested as soon as the source block is confirmed. The rail does not care which
one it is looking at: it reads the message the same way.

Usage:
    python3 scripts/spike_burn_v2.py [amount-in-usdc] [--wait SECONDS]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAST = os.path.expanduser("~/.foundry/bin/cast")
RPC_BASE = os.environ.get("BASE_SEPOLIA_RPC", "https://sepolia.base.org")
IRIS = "https://iris-api-sandbox.circle.com"

MESSENGER_V2 = "0x8FE6B999Dc680CcFDD5Bf7EB0974218be2542DAA"
TRANSMITTER_V2 = "0xE737e5cEBEEBa77EFE34D4aa090756590b1CE275"
USDC_BASE = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
SOURCE_DOMAIN = 6
DESTINATION_DOMAIN = 0
FAST_THRESHOLD = 1000


def load_env() -> dict:
    env: dict = {}
    path = os.environ.get("ASTRA_ENV") or os.path.join(ROOT, ".env")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    for k, v in os.environ.items():
        if k.isupper():
            env[k] = v
    return env


def cast(env, sub, *args):
    out = subprocess.run([CAST, sub, "--rpc-url", RPC_BASE, *args], capture_output=True, text=True,
                         env={**os.environ, **env})
    if out.returncode != 0:
        raise SystemExit(f"cast {sub} failed: {out.stderr[:500]}")
    return out.stdout.strip()


def send(env, to, sig, *params):
    out = subprocess.run([CAST, "send", "--rpc-url", RPC_BASE, "--private-key", env["BUYER_KEY"],
                          "--json", to, sig, *params], capture_output=True, text=True,
                         env={**os.environ, **env})
    if out.returncode != 0:
        raise SystemExit(f"send {sig} failed: {out.stderr[:500]}")
    d = json.loads(out.stdout)
    return d.get("transactionHash"), d.get("status")


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 Astra"})
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return {"_http": e.code, **json.loads(raw)}
        except Exception:  # noqa: BLE001
            return {"_http": e.code, "raw": raw[:300]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("amount", nargs="?", default="1")
    ap.add_argument("--wait", type=int, default=600)
    args = ap.parse_args()
    env = load_env()
    amount = int(float(args.amount) * 1_000_000)
    payer = env["BUYER"]

    allowance = int(cast(env, "call", USDC_BASE, "allowance(address,address)(uint256)",
                         payer, MESSENGER_V2).split()[0])
    if allowance < amount:
        tx, status = send(env, USDC_BASE, "approve(address,uint256)", MESSENGER_V2, str(amount))
        print(f"approve v2 -> {tx} status={status}")

    recipient32 = "0x" + "0" * 24 + payer[2:].lower()
    max_fee = max(1, amount // 1000)
    tx, status = send(env, MESSENGER_V2,
                      "depositForBurn(uint256,uint32,bytes32,address,bytes32,uint256,uint32)",
                      str(amount), str(DESTINATION_DOMAIN), recipient32, USDC_BASE,
                      "0x" + "0" * 64, str(max_fee), str(FAST_THRESHOLD))
    print(f"burn v2    -> {tx} status={status} maxFee={max_fee} threshold={FAST_THRESHOLD}")

    art = {"payer": payer, "amount": amount, "burnTx": tx, "messenger": MESSENGER_V2,
           "transmitter": TRANSMITTER_V2, "maxFee": max_fee, "threshold": FAST_THRESHOLD}
    path = os.path.join(ROOT, "artifacts", "spike-burn-v2.json")

    deadline = time.time() + args.wait
    while time.time() < deadline:
        body = get(f"{IRIS}/v2/messages/{SOURCE_DOMAIN}?transactionHash={tx}")
        msgs = body.get("messages") or []
        if msgs:
            m = msgs[0]
            att = m.get("attestation")
            print(f"   v2 status={m.get('status')} attestation={str(att)[:24]}")
            art["message"] = m.get("message")
            art["attestation"] = att
            art["status"] = m.get("status")
            art["decoded"] = m.get("decodedMessage")
            if att and att != "PENDING":
                break
        else:
            print(f"   not indexed yet: {body.get('error', body)}")
        time.sleep(15)

    json.dump(art, open(path, "w", encoding="utf-8"), indent=2)
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
