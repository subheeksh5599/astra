#!/usr/bin/env python3
"""Astra spike, stage 1: read through KeeperHub, and look at the wallet.

No value moves here. It answers three questions:

  1. Does the KeeperHub direct-execution API answer a read on Ethereum Sepolia
     and on Base Sepolia with this key?
  2. What address does KeeperHub sign from, and what does it hold on both chains?
  3. What does the protocol's own state say (chain domain, and whether a given
     transfer has already been received)?

Usage:
    .venv/bin/python scripts/spike_read.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KH_BASE = "https://app.keeperhub.com"

BASE_SEPOLIA = 84532
ETH_SEPOLIA = 11155111

# The protocol's own contracts, on both testnets.
TRANSMITTER = "0x7865fAfC2db2093669d92c0F33AeEF291086BEFD"
MESSENGER = "0x9f3B8679c73C2Fef8b59B4f3444d4e156fb70AA5"
USDC = {
    BASE_SEPOLIA: "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
    ETH_SEPOLIA: "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238",
}
RPC = {
    BASE_SEPOLIA: "https://sepolia.base.org",
    ETH_SEPOLIA: "https://ethereum-sepolia-rpc.publicnode.com",
}

ABI_TRANSMITTER_VIEWS = json.dumps([
    {"inputs": [], "name": "localDomain", "outputs": [{"name": "", "type": "uint32"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "paused", "outputs": [{"name": "", "type": "bool"}],
     "stateMutability": "view", "type": "function"},
])

ABI_ERC20 = json.dumps([
    {"inputs": [{"name": "account", "type": "address"}], "name": "balanceOf",
     "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
])


def load_env() -> dict:
    """Astra reads its own .env, then lets the process environment override it."""
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


def kh(env: dict, method: str, path: str, body: dict | None = None):
    headers = {
        "Authorization": f"Bearer {env['KH_API_KEY']}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/124",
    }
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(KH_BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:  # noqa: BLE001
            return e.code, {"raw": raw[:400]}


def rpc(url: str, method: str, params: list):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/124"})
    with urllib.request.urlopen(req, timeout=40) as resp:
        return json.loads(resp.read().decode()).get("result")


def read_contract(env, chain_id, address, function, args="[]", abi=None):
    body = {"contractAddress": address, "chainId": chain_id, "functionName": function,
            "functionArgs": args, "abi": abi or ABI_TRANSMITTER_VIEWS}
    return kh(env, "POST", "/api/execute/contract-call", body)


def main() -> int:
    env = load_env()
    if not env.get("KH_API_KEY"):
        print("FAIL: KH_API_KEY is not set in the environment or in ./.env")
        return 2

    print("== 1. KeeperHub read on both chains (no value moved)")
    for chain_id, label in ((BASE_SEPOLIA, "Base Sepolia"), (ETH_SEPOLIA, "Ethereum Sepolia")):
        status, body = read_contract(env, chain_id, TRANSMITTER, "localDomain")
        print(f"   {label:18s} localDomain  -> HTTP {status} {json.dumps(body)[:160]}")

    print("\n== 2. the KeeperHub signing wallet, and what it holds")
    status, body = kh(env, "GET", "/api/integrations")
    wallet = None
    if isinstance(body, list):
        for item in body:
            if item.get("type") == "web3" and item.get("address"):
                wallet = item["address"]
                break
    print(f"   GET /api/integrations -> HTTP {status}, wallet={wallet}")
    if wallet:
        for chain_id in (BASE_SEPOLIA, ETH_SEPOLIA):
            native = rpc(RPC[chain_id], "eth_getBalance", [wallet, "latest"])
            data = "0x70a08231" + "0" * 24 + wallet[2:].lower()
            token = rpc(RPC[chain_id], "eth_call", [{"to": USDC[chain_id], "data": data}, "latest"])
            n = int(native, 16) / 1e18 if native and native != "0x" else 0.0
            t = int(token, 16) / 1e6 if token and token != "0x" else 0.0
            print(f"   chain {chain_id}: native={n:.6f} usdc={t:.6f}")

    print("\n== 3. the payer wallet we hold the key for")
    payer = env.get("BUYER", "?")
    for chain_id in (BASE_SEPOLIA, ETH_SEPOLIA):
        native = rpc(RPC[chain_id], "eth_getBalance", [payer, "latest"])
        data = "0x70a08231" + "0" * 24 + payer[2:].lower()
        token = rpc(RPC[chain_id], "eth_call", [{"to": USDC[chain_id], "data": data}, "latest"])
        n = int(native, 16) / 1e18 if native and native != "0x" else 0.0
        t = int(token, 16) / 1e6 if token and token != "0x" else 0.0
        print(f"   {payer} chain {chain_id}: native={n:.6f} usdc={t:.6f}")

    out = os.path.join(ROOT, "artifacts", "spike-read.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"wallet": wallet, "kh_base": KH_BASE}, fh, indent=2)
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
