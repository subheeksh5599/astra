#!/usr/bin/env python3
"""Astra spike, stage 2: create the in-flight transfer, and get its attestation.

This moves real testnet value: it approves and burns USDC on Base Sepolia with
Ethereum Sepolia as the destination, and it deliberately does NOT complete the
transfer. That is the condition the rail has to detect and then finish.

Everything is signed by the payer wallet this machine holds the key for; nothing
here goes through KeeperHub, because this is the *paying* side of the story and
the rail is what comes after.

Usage:
    python3 scripts/spike_burn.py [amount-in-usdc]
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAST = os.path.expanduser("~/.foundry/bin/cast")

BASE_SEPOLIA = 84532
ETH_SEPOLIA_DOMAIN = 0  # Ethereum Sepolia's chain domain, read from the transmitter itself

RPC_BASE = os.environ.get("BASE_SEPOLIA_RPC", "https://sepolia.base.org")
MESSENGER = "0x9f3B8679c73C2Fef8b59B4f3444d4e156fb70AA5"
USDC_BASE = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
IRIS = "https://iris-api-sandbox.circle.com"


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


def cast(env: dict, sub: str, *args: str) -> str:
    out = subprocess.run([CAST, sub, "--rpc-url", RPC_BASE, *args],
                         capture_output=True, text=True, env={**os.environ, **env})
    if out.returncode != 0:
        raise SystemExit(f"cast failed: {sub} {' '.join(args[:3])}\n{out.stderr[:600]}")
    return out.stdout.strip()


def send(env: dict, to: str, sig: str, *params: str) -> dict:
    raw = subprocess.run([CAST, "send", "--rpc-url", RPC_BASE, "--private-key", env["BUYER_KEY"],
                          "--json", to, sig, *params],
                         capture_output=True, text=True, env={**os.environ, **env})
    if raw.returncode != 0:
        raise SystemExit(f"send failed: {sig}\n{raw.stderr[:600]}")
    out = json.loads(raw.stdout)
    return {"tx": out.get("transactionHash"), "status": out.get("status"), "gasUsed": out.get("gasUsed")}


def http(url: str) -> dict:
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
    env = load_env()
    amount = int(float(sys.argv[1] if len(sys.argv) > 1 else "1") * 1_000_000)
    payer = env["BUYER"]

    allowance = cast(env, "call", USDC_BASE, "allowance(address,address)(uint256)", payer, MESSENGER)
    allowance = int(allowance.split()[0])
    print(f"payer {payer} allowance to the messenger: {allowance}")

    if allowance < amount:
        appr = send(env, USDC_BASE, "approve(address,uint256)", MESSENGER, str(amount))
        print(f"approve  -> {appr['tx']}  status={appr['status']} gas={appr['gasUsed']}")

    recipient32 = "0x" + "0" * 24 + payer[2:].lower()
    burn = send(env, MESSENGER, "depositForBurn(uint256,uint32,bytes32,address)",
                str(amount), str(ETH_SEPOLIA_DOMAIN), recipient32, USDC_BASE)
    print(f"burn     -> {burn['tx']}  status={burn['status']} gas={burn['gasUsed']}")

    art = {"payer": payer, "amount": amount, "burnTx": burn["tx"], "destinationDomain": ETH_SEPOLIA_DOMAIN}
    print("\nfetching the message and attestation (source domain 6 = Base Sepolia)")
    body = http(f"{IRIS}/v1/messages/6/{burn['tx']}")
    msgs = body.get("messages") or []
    if not msgs:
        print("   no message yet:", json.dumps(body)[:300])
    else:
        m = msgs[0]
        art["message"] = m.get("message")
        art["attestation"] = m.get("attestation")
        art["attestationStatus"] = m.get("status")
        print(f"   status={m.get('status')} message={str(m.get('message'))[:40]}...")
        print(f"   attestation={'pending' if m.get('attestation') in (None, 'PENDING') else str(m.get('attestation'))[:40] + '...'}")

    out = os.path.join(ROOT, "artifacts", "spike-burn.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(art, fh, indent=2)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
