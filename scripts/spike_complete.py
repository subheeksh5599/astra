#!/usr/bin/env python3
"""Astra spike, stage 3: finish the in-flight transfer through KeeperHub.

The transfer created by spike_burn.py is stuck: the USDC left Base Sepolia and
has not been minted on Ethereum Sepolia. This script does what the rail does:

  observe   -> read the source burn and the destination's own state
  defer     -> while the attestation is not final, wait and say so
  complete  -> hand the signed attestation to the destination contract, executed
               by KeeperHub (simulate first, then broadcast, then poll the hash)
  refuse    -> run the same completion a second time; the destination refuses it
               and the rail reports why, having moved nothing

Usage:
    python3 scripts/spike_complete.py [--max-wait 540]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KH_BASE = "https://app.keeperhub.com"
IRIS = "https://iris-api-sandbox.circle.com"

SOURCE_DOMAIN = 6          # Base Sepolia
ETH_SEPOLIA = 11155111
TRANSMITTER = "0x7865fAfC2db2093669d92c0F33AeEF291086BEFD"

ABI = json.dumps([
    {"inputs": [{"name": "message", "type": "bytes"}, {"name": "attestation", "type": "bytes"}],
     "name": "receiveMessage", "outputs": [{"name": "success", "type": "bool"}],
     "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "nonce", "type": "bytes32"}], "name": "usedNonces",
     "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
])


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


def kh(env: dict, method: str, path: str, body: dict | None = None, idem: str | None = None):
    headers = {"Authorization": f"Bearer {env['KH_API_KEY']}", "Content-Type": "application/json",
               "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/124"}
    if idem:
        headers["Idempotency-Key"] = idem
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(KH_BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:  # noqa: BLE001
            return e.code, {"raw": raw[:400]}


def get_json(url: str) -> dict:
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


def fetch_attestation(burn_tx: str, max_wait: int) -> dict:
    """Poll until the attestation is final. This is the deferral the rail reports."""
    deadline = time.time() + max_wait
    last = None
    while time.time() < deadline:
        body = get_json(f"{IRIS}/v1/messages/{SOURCE_DOMAIN}/{burn_tx}")
        msgs = body.get("messages") or []
        if msgs:
            m = msgs[0]
            status = m.get("status")
            att = m.get("attestation")
            if att and att != "PENDING":
                return {"state": "final", "message": m.get("message"), "attestation": att,
                        "status": status, "decoded": m.get("decodedMessage", {})}
            last = {"state": "deferred", "status": status}
            print(f"   deferred: attestation not final (status={status})")
        else:
            last = {"state": "deferred", "status": body.get("error", "not indexed yet")}
            print(f"   deferred: {body.get('error', body)}")
        time.sleep(20)
    return last or {"state": "deferred", "status": "timeout"}


def poll_status(env, exec_id: str, attempts: int = 6) -> dict:
    out = {}
    for _ in range(attempts):
        _s, body = kh(env, "GET", f"/api/execute/{exec_id}/status")
        out = body if isinstance(body, dict) else {}
        if out.get("transactionHash") or str(out.get("status")) in ("failed", "error", "reverted", "cancelled"):
            return out
        time.sleep(2)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-wait", type=int, default=540)
    args = ap.parse_args()
    env = load_env()

    art_path = os.path.join(ROOT, "artifacts", "spike-burn.json")
    art = json.load(open(art_path, encoding="utf-8"))
    burn_tx = art.get("burnTx")
    if not burn_tx:
        print("no burnTx in artifacts/spike-burn.json - run spike_burn.py first")
        return 2
    print(f"in-flight transfer: source domain {SOURCE_DOMAIN}, burn {burn_tx}")

    print("\n== observe: the destination's own state for this transfer")
    att = fetch_attestation(burn_tx, args.max_wait)
    art["attestation"] = att.get("attestation")
    art["attestationState"] = att.get("state")
    print(f"   attestation state: {att.get('state')}")
    if att.get("state") != "final":
        json.dump(art, open(art_path, "w", encoding="utf-8"), indent=2)
        print("   still in flight; nothing was executed")
        return 3

    body = {"contractAddress": TRANSMITTER, "chainId": ETH_SEPOLIA,
            "functionName": "receiveMessage",
            "functionArgs": json.dumps([att["message"], att["attestation"]]),
            "abi": ABI}

    print("\n== complete: simulate through KeeperHub")
    st, sim = kh(env, "POST", "/api/execute/contract-call", {**body, "simulate": True})
    print(f"   HTTP {st} {json.dumps(sim)[:400]}")
    art["simulate"] = sim

    if not (isinstance(sim, dict) and sim.get("success") and not sim.get("wouldRevert")):
        print("   the simulation did not clear; broadcast skipped")
        json.dump(art, open(art_path, "w", encoding="utf-8"), indent=2)
        return 4

    print("\n== complete: broadcast through KeeperHub")
    st2, sent = kh(env, "POST", "/api/execute/contract-call", body,
                   idem=f"astra-{burn_tx[-12:]}")
    print(f"   HTTP {st2} {json.dumps(sent)[:300]}")
    exec_id = (sent or {}).get("executionId") if isinstance(sent, dict) else None
    status = poll_status(env, exec_id) if exec_id else {}
    art["execution"] = {"id": exec_id, "status": status.get("status"),
                        "tx": status.get("transactionHash"), "link": status.get("transactionLink")}
    print(f"   execution {exec_id} status={status.get('status')} tx={status.get('transactionHash')}")

    print("\n== refuse: the same completion, a second time")
    st3, sim2 = kh(env, "POST", "/api/execute/contract-call", {**body, "simulate": True})
    art["refusal"] = {"http": st3, "body": sim2}
    print(f"   HTTP {st3} {json.dumps(sim2)[:400]}")

    json.dump(art, open(art_path, "w", encoding="utf-8"), indent=2)
    print(f"\nwrote {art_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
