"""The execution layer.

Astra does not hold a key and does not sign. Every state-changing call goes
through the execution API: simulate first, then broadcast, then poll until the
transaction hash appears, because a status read can lag the write.

The simulation is not a nicety here. It is the destination's own answer to "may
this transfer be delivered", returned before anything is spent, and it is where
the already-delivered refusal comes from.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

UA = "Mozilla/5.0 (X11; Linux x86_64) Chrome/124"
DEFAULT_BASE = "https://app.keeperhub.com"
TERMINAL_FAILURES = ("failed", "error", "reverted", "cancelled")


class KeeperHub:
    def __init__(self, key: str, base: str = DEFAULT_BASE):
        self.key = key
        self.base = base.rstrip("/")

    def request(self, method: str, path: str, body: dict | None = None, idem: str | None = None):
        headers = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json",
                   "User-Agent": UA}
        if idem:
            headers["Idempotency-Key"] = idem
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            try:
                return e.code, json.loads(raw)
            except Exception:  # noqa: BLE001
                return e.code, {"raw": raw[:400]}

    def wallet(self) -> str | None:
        _status, body = self.request("GET", "/api/integrations")
        if isinstance(body, list):
            for item in body:
                if item.get("type") == "web3" and item.get("address"):
                    return item["address"]
        return None

    def read(self, chain_id: int, address: str, function: str, abi: str, args: list | None = None):
        body = {"contractAddress": address, "chainId": chain_id, "functionName": function,
                "functionArgs": json.dumps(args or []), "abi": abi}
        return self.request("POST", "/api/execute/contract-call", body)

    def simulate(self, chain_id: int, address: str, function: str, abi: str, args: list):
        body = {"contractAddress": address, "chainId": chain_id, "functionName": function,
                "functionArgs": json.dumps(args), "abi": abi, "simulate": True}
        return self.request("POST", "/api/execute/contract-call", body)

    def execute(self, chain_id: int, address: str, function: str, abi: str, args: list,
                idem: str | None = None, poll_seconds: float = 2.0, attempts: int = 6):
        body = {"contractAddress": address, "chainId": chain_id, "functionName": function,
                "functionArgs": json.dumps(args), "abi": abi}
        status, sent = self.request("POST", "/api/execute/contract-call", body, idem=idem)
        record = {"http": status, "response": sent}
        execution_id = sent.get("executionId") if isinstance(sent, dict) else None
        if not execution_id:
            return record
        record["execution_id"] = execution_id
        for _ in range(attempts):
            _s, state = self.request("GET", f"/api/execute/{execution_id}/status")
            record["status"] = state if isinstance(state, dict) else {"raw": state}
            if record["status"].get("transactionHash"):
                break
            if str(record["status"].get("status")) in TERMINAL_FAILURES:
                break
            time.sleep(poll_seconds)
        return record

    def transaction_hash(self, record: dict) -> str | None:
        return (record.get("status") or {}).get("transactionHash")

    def transaction_link(self, record: dict) -> str | None:
        return (record.get("status") or {}).get("transactionLink")
