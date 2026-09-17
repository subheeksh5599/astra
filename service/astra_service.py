#!/usr/bin/env python3
"""The rail, over HTTP.

Two kinds of caller use this. A person opens the page and finishes a transfer
that is stuck. An agent calls the same routes and gets the same receipts. Neither
one is handed a key: the completing side is executed by the execution layer, and
the paying side is signed in the caller's own wallet.

Nothing here is configured in the client. The page asks /api/config and gets the
chains, the token and the contract addresses the rail is running against.

Usage:
    .venv/bin/python service/astra_service.py --port 8099
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from astra import inflight  # noqa: E402
from astra.attestation import Attestation  # noqa: E402
from astra.config import (CHAIN_IDS, DOMAIN_NAMES, MESSENGERS, USDC, attestation_base,  # noqa: E402
                          attestation_version, deployment, load_env, rpc_url, supports,
                          transmitter)
from astra.keeperhub import KeeperHub  # noqa: E402
from astra.rail import Rail  # noqa: E402
from astra.rpc import Rpc  # noqa: E402

WEB = os.path.join(ROOT, "service", "web")

# Reading two chains is not instant, and the page asks for the same window on
# every load. One scan is shared for a short window rather than repeated per
# request; the age is reported with the rows so a stale answer is visible.
SCAN_TTL_SECONDS = 30

# How many transfers are classified per batch, and how many of those at once.
# The batch is what makes the pass finish in seconds instead of minutes; the
# bound is what keeps a public endpoint from being hammered by parallelism.
STATE_WINDOW = 8
STATE_WORKERS = 4
_SCAN_LOCK = threading.Lock()
_SCAN_CACHE: dict = {}


def public_config(env: dict) -> dict:
    """Everything the page needs, taken from the environment, never invented."""
    name = deployment(env)
    chains = {}
    for domain, chain_id in CHAIN_IDS.items():
        if not supports(env, domain):
            continue
        chains[str(domain)] = {
            "domain": domain,
            "chain_id": chain_id,
            "name": DOMAIN_NAMES.get(domain, str(domain)),
            "usdc": USDC[domain],
            "messenger": MESSENGERS[name][domain],
            "transmitter": transmitter(env, domain),
        }
    return {
        "deployment": name,
        "chains": chains,
        "watched_domains": sorted(chains),
        "attestation_base": attestation_base(env),
        "faucet": env.get("FAUCET_URL", ""),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "astra"

    def log_message(self, fmt, *args):  # quieter than the default
        if os.environ.get("ASTRA_HTTP_LOG"):
            super().log_message(fmt, *args)

    # -- helpers ----------------------------------------------------------
    def send_json(self, payload, status=200):
        body = json.dumps(payload, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, content_type):
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError:
            self.send_json({"error": "not found", "path": path}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode())
        except (ValueError, UnicodeDecodeError):
            return {}

    # -- routes -----------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        env = load_env()

        if path == "/api/health":
            return self.send_json({"ok": True, "deployment": deployment(env)})
        if path == "/api/config":
            return self.send_json(public_config(env))
        if path == "/api/inflight":
            return self.inflight(query, env)
        if path == "/api/inspect":
            return self.inspect(query, env)
        if path == "/api/receipts":
            return self.receipts()
        if path.startswith("/api/receipt/"):
            return self.receipt(path.rsplit("/", 1)[-1])
        return self.static(path)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path.rstrip("/") == "/api/complete":
            return self.complete(self.read_body())
        return self.send_json({"error": "unknown route", "path": parsed.path}, 404)

    # -- handlers ---------------------------------------------------------
    def inflight(self, query, env):
        blocks = int((query.get("blocks") or ["1200"])[0])
        limit = int((query.get("limit") or ["12"])[0])
        key = (blocks, limit)
        now = time.time()
        with _SCAN_LOCK:
            cached = _SCAN_CACHE.get(key)
            if cached and now - cached["at"] < SCAN_TTL_SECONDS:
                return self.send_json({"rows": cached["rows"], "blocks": blocks,
                                       "age_seconds": round(now - cached["at"], 1),
                                       "cached": True})
        try:
            rows = build_inflight(env, blocks, limit)
        except Exception as exc:  # noqa: BLE001
            return self.send_json({"error": str(exc)[:300]}, 502)
        with _SCAN_LOCK:
            _SCAN_CACHE[key] = {"rows": rows, "at": time.time()}
        return self.send_json({"rows": rows, "blocks": blocks, "age_seconds": 0.0, "cached": False})

    def inspect(self, query, env):
        """Read one transfer, decide, and broadcast nothing.

        The landing page needs an answer about a transfer without moving money,
        so the same pass runs with the broadcast suppressed. The receipt says so.
        """
        burn_tx = (query.get("burn_tx") or [""])[0]
        if not burn_tx.startswith("0x") or len(burn_tx) != 66:
            return self.send_json({"error": "burn_tx must be a 32-byte transaction hash"}, 400)
        request = {
            "source_domain": int((query.get("source_domain") or ["6"])[0]),
            "destination_domain": int((query.get("destination_domain") or ["0"])[0]),
            "burn_tx": burn_tx,
        }
        try:
            rail = Rail(env)
            receipt = rail.run(request, dry_run=True)
        except Exception as exc:  # noqa: BLE001
            return self.send_json({"error": str(exc)[:300]}, 502)
        return self.send_json(receipt)

    def receipts(self):
        folder = os.path.join(ROOT, "artifacts", "receipts")
        out = []
        if os.path.isdir(folder):
            for name in sorted(os.listdir(folder), reverse=True):
                if not name.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(folder, name), encoding="utf-8") as fh:
                        data = json.load(fh)
                except (OSError, ValueError):
                    continue
                out.append({
                    "name": name,
                    "transfer_id": data.get("transfer_id"),
                    "decision": data.get("decision"),
                    "transaction_hash": data.get("transaction_hash"),
                    "transaction_link": data.get("transaction_link"),
                })
        return self.send_json({"receipts": out})

    def receipt(self, name):
        if not name.replace("-", "").replace("_", "").isalnum():
            return self.send_json({"error": "bad receipt name"}, 400)
        path = os.path.join(ROOT, "artifacts", "receipts", f"{name}.json")
        if not os.path.exists(path):
            return self.send_json({"error": "no such receipt"}, 404)
        with open(path, encoding="utf-8") as fh:
            return self.send_json(json.load(fh))

    def complete(self, body):
        burn_tx = (body or {}).get("burn_tx")
        if not burn_tx or not isinstance(burn_tx, str) or not burn_tx.startswith("0x"):
            return self.send_json({"error": "burn_tx is required"}, 400)
        env = load_env()
        request = {
            "source_domain": int(body.get("source_domain", 6)),
            "destination_domain": int(body.get("destination_domain", 0)),
            "burn_tx": burn_tx,
        }
        wait = int(body.get("wait_seconds") or 0)
        try:
            rail = Rail(env)
            receipt = rail.run(request, max_wait=wait, interval=10) if wait else rail.run(request)
            rail.write_receipt(receipt)
        except Exception as exc:  # noqa: BLE001
            return self.send_json({"error": str(exc)[:300]}, 502)
        return self.send_json(receipt)

    def static(self, path):
        if path in ("/", "/index.html"):
            return self.send_file(os.path.join(WEB, "index.html"), "text/html; charset=utf-8")
        if path in ("/app", "/app.html"):
            return self.send_file(os.path.join(WEB, "app.html"), "text/html; charset=utf-8")
        candidate = os.path.normpath(os.path.join(WEB, path.lstrip("/")))
        if not candidate.startswith(WEB):
            return self.send_json({"error": "not found"}, 404)
        if os.path.isfile(candidate):
            ext = os.path.splitext(candidate)[1]
            types = {".css": "text/css", ".js": "application/javascript",
                     ".woff2": "font/woff2", ".svg": "image/svg+xml",
                     ".json": "application/json", ".html": "text/html; charset=utf-8"}
            return self.send_file(candidate, types.get(ext, "application/octet-stream"))
        return self.send_json({"error": "not found", "path": path}, 404)


def build_inflight(env: dict, blocks: int, limit: int) -> list:
    """The transfers in flight, newest first, across every watched chain.

    Candidates are collected from every source chain before any of them is
    classified, and merged by block: reading one chain to exhaustion would hide
    the other chains' transfers behind it, which is exactly the blind spot this
    rail exists to remove.

    The reads are independent, so they run together: five chains, each with its
    own endpoint, read in parallel, then the per-transfer work in bounded windows.
    A pass over five chains should cost about what a pass over one did, and the
    verdict for one transfer never depends on another transfer's timing.
    """
    name = deployment(env)
    kh = KeeperHub(env["KH_API_KEY"])
    attestation = Attestation(attestation_base(env), attestation_version(env))
    wallet = kh.wallet()

    per_domain = int(env.get("ASTRA_PER_DOMAIN", "10"))
    sources = [domain for domain in sorted(CHAIN_IDS) if supports(env, domain)]

    def scan_one(domain: int) -> list:
        try:
            # A cap per chain keeps one busy chain from filling the whole view:
            # the point of watching five is seeing five.
            return inflight.scan(Rpc(rpc_url(env, domain)), domain, blocks,
                                 deployment=name)[:per_domain]
        except Exception:  # noqa: BLE001 - one chain's endpoint must not blind the others
            return []

    with ThreadPoolExecutor(max_workers=max(1, len(sources))) as pool:
        found = list(pool.map(scan_one, sources))
    candidates = [transfer for per_chain in found for transfer in per_chain]
    candidates.sort(key=lambda item: item.get("block") or 0, reverse=True)

    # Most traffic on a live testnet goes to chains this rail does not serve. A
    # view that is 90% "not mine" hides the transfers it can actually finish, so
    # those are held back and shown up to a small cap: enough to see the boundary,
    # never enough to bury the work.
    reachable: list = []
    beyond: list = []
    max_beyond = int(env.get("ASTRA_MAX_BEYOND", "3"))
    # Every candidate costs an attestation read, so the pass is bounded: a view
    # that would spend a minute looking for a tenth row is not a view anyone
    # waits for, and the answer for the rows it did read is already true.
    max_examined = int(env.get("ASTRA_MAX_EXAMINED", "24"))
    index = 0
    while (index < len(candidates) and index < max_examined
           and (len(reachable) < limit or len(beyond) < max_beyond)):
        window = candidates[index:index + STATE_WINDOW]
        index += STATE_WINDOW

        def describe(transfer: dict) -> dict:
            return inflight.state_of_transfer(kh, attestation, {**env, "ASTRA_DEPLOYMENT": name},
                                              transfer, wallet=wallet)

        with ThreadPoolExecutor(max_workers=min(STATE_WORKERS, len(window))) as pool:
            records = list(pool.map(describe, window))

        for record in records:
            if record.get("attestation_state") == "not_found":
                continue  # a source log with no message behind it is not a transfer
            parsed = record.get("parsed") or {}
            destination = parsed.get("destination_domain")
            reason = (record.get("decision") or {}).get("reason")
            bucket = beyond if reason in ("UNSUPPORTED_DOMAIN", "DEPLOYMENT_ABSENT") else reachable
            if len(bucket) >= (max_beyond if bucket is beyond else limit):
                continue
            bucket.append({
                "burn_tx": record["burn_tx"],
                "source_domain": record["source_domain"],
                "destination_domain": destination,
                "destination_name": DOMAIN_NAMES.get(destination, str(destination) if destination is not None else None),
                "transfer_id": f"{record['source_domain']}:{parsed.get('nonce')}" if parsed.get("nonce")
                else f"{record['source_domain']}:{record['burn_tx'][:12]}",
                "nonce": parsed.get("nonce"),
                "mint_recipient": parsed.get("mint_recipient"),
                "amount": parsed.get("amount"),
                "amount_usdc": (parsed.get("amount") or 0) / 1e6,
                "destination_caller": parsed.get("destination_caller"),
                "attestation_state": record.get("attestation_state"),
                "decision": record.get("decision"),
                "finished": record.get("finished"),
                "block": record.get("block"),
            })
    return reachable + beyond


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8099")))
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    args = ap.parse_args()
    env = load_env()
    if not env.get("KH_API_KEY"):
        print("warning: KH_API_KEY is not set; /api/complete and discovery will fail")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"astra listening on http://{args.host}:{args.port} "
          f"(deployment {deployment(env)}, collecting on {sorted(CHAIN_IDS)})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopping")
    return 0


if __name__ == "__main__":
    sys.exit(main())
