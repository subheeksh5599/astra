"""The attestation the destination contract checks before it mints.

The transfer is not completable until the attestation is signed. Until then the
honest action is to wait and say so, which is what the deferral branch reports.

Two deployments answer on two paths. The earlier one answers on the version
one path and is attested after full finality; the later one answers on the
version two path, which carries the transfer's finality threshold and can be
attested once the source block is confirmed.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

UA = "Mozilla/5.0 (X11; Linux x86_64) Chrome/124"


#: States that mean the answer is not ready yet, not that there is no transfer.
RETRY_STATES = ("pending", "not_found")


class Attestation:
    def __init__(self, base: str, version: str = "v2", timeout: int = 45):
        self.base = base.rstrip("/")
        self.version = version
        self.timeout = timeout

    def _get(self, path: str) -> dict:
        req = urllib.request.Request(self.base + path, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            try:
                body = json.loads(raw)
            except Exception:  # noqa: BLE001
                body = {"raw": raw[:300]}
            return {**body, "_http": e.code}

    def _path(self, source_domain: int, tx_hash: str) -> str:
        if self.version == "v1":
            return f"/v1/messages/{source_domain}/{tx_hash}"
        return f"/v2/messages/{source_domain}?transactionHash={tx_hash}"

    def by_transaction(self, source_domain: int, tx_hash: str) -> dict:
        """Everything the attestation service knows about one source transaction."""
        body = self._get(self._path(source_domain, tx_hash))
        messages = body.get("messages") or []
        if not messages:
            return {"state": "not_found", "detail": body.get("error") or body,
                    "http": body.get("_http")}
        first = messages[0]
        attestation = first.get("attestation")
        if not attestation or attestation == "PENDING":
            return {"state": "pending", "status": first.get("status"),
                    "message": first.get("message"), "decoded": first.get("decodedMessage")}
        return {"state": "final", "status": first.get("status"), "message": first.get("message"),
                "attestation": attestation, "decoded": first.get("decodedMessage")}

    def await_final(self, source_domain: int, tx_hash: str, max_wait: int, interval: int = 20,
                    on_wait=None) -> dict:
        """Wait for an answer that is worth acting on.

        Two states mean "ask again", not "no": pending, and not_found. The service
        indexes a transfer a little after the source chain accepts it, so a transfer
        made thirty seconds ago answers not_found for a while -- and refusing on
        that would refuse precisely the transfer the caller just made. The state is
        still what gets returned if the budget runs out, so nothing is hidden: the
        rail just does not mistake "not yet" for "never".
        """
        deadline = time.time() + max_wait
        last: dict = {"state": "not_found"}
        attempts = 0
        while True:
            last = self.by_transaction(source_domain, tx_hash)
            attempts += 1
            if last["state"] not in RETRY_STATES:
                return last
            if on_wait:
                on_wait(last, attempts, max(0, int(deadline - time.time())))
            if time.time() >= deadline:
                return last
            time.sleep(interval)
