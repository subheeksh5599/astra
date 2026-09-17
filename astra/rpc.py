"""A minimal JSON-RPC reader.

Reads only: the rail never signs anything itself, so there is no key handling
here. Writes go through the execution layer, on purpose.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

UA = "Mozilla/5.0 (X11; Linux x86_64) Chrome/124"


class Rpc:
    def __init__(self, url: str, timeout: int = 40):
        self.url = url
        self.timeout = timeout

    def call(self, method: str, params: list):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        req = urllib.request.Request(self.url, data=body, headers={
            "Content-Type": "application/json", "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{self.url} {method} -> HTTP {e.code}") from e
        if "error" in payload:
            raise RuntimeError(f"{method} -> {payload['error'].get('message')}")
        return payload.get("result")

    def block_number(self) -> int:
        return int(self.call("eth_blockNumber", []), 16)

    def get_logs(self, address: str, topics: list, from_block: int, to_block) -> list:
        return self.call("eth_getLogs", [{
            "address": address,
            "topics": topics,
            "fromBlock": hex(from_block),
            "toBlock": to_block if isinstance(to_block, str) else hex(to_block),
        }]) or []

    def call_contract(self, to: str, data: str) -> str:
        return self.call("eth_call", [{"to": to, "data": data}, "latest"])

    def blocks_per_second(self, behind: int = 2000) -> float:
        """How fast this chain produces blocks, measured rather than assumed.

        A window is a span of time, and chains disagree about how many blocks that
        is: the same 1,200 blocks is four hours on one chain and five minutes on
        another. Anything that reads two chains over "the same number of blocks" is
        reading two different amounts of history.
        """
        head = self.call("eth_getBlockByNumber", ["latest", False]) or {}
        try:
            head_number = int(head["number"], 16)
            head_time = int(head["timestamp"], 16)
        except (KeyError, TypeError, ValueError):
            return 0.0
        old_number = max(1, head_number - behind)
        old = self.call("eth_getBlockByNumber", [hex(old_number), False]) or {}
        try:
            seconds = head_time - int(old["timestamp"], 16)
            count = head_number - old_number
        except (KeyError, TypeError, ValueError):
            return 0.0
        return (count / seconds) if seconds > 0 and count > 0 else 0.0

    def transaction_receipt(self, tx_hash: str) -> dict:
        return self.call("eth_getTransactionReceipt", [tx_hash]) or {}

    def balance_of(self, token: str, holder: str) -> int:
        data = "0x70a08231" + "0" * 24 + holder[2:].lower()
        raw = self.call_contract(token, data)
        return int(raw, 16) if raw and raw != "0x" else 0
