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
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from astra import inflight  # noqa: E402
from astra.attestation import Attestation  # noqa: E402
from astra.config import (CHAIN_IDS, DOMAIN_NAMES, MESSENGERS, USDC, attestation_base,  # noqa: E402
                          attestation_version, deployment, load_env, rpc_url)
from astra.keeperhub import KeeperHub  # noqa: E402
from astra.rail import Rail  # noqa: E402
from astra.rpc import Rpc  # noqa: E402

WEB = os.path.join(ROOT, "service", "web")

# Reading two chains is not instant, and the page asks for the same window on
# every load. One scan is shared for a short window rather than repeated per
# request; the age is reported with the rows so a stale answer is visible.
SCAN_TTL_SECONDS = 30
_SCAN_LOCK = threading.Lock()
_SCAN_CACHE: dict = {}


def public_config(env: dict) -> dict:
    """Everything the page needs, taken from the environment, never invented."""
    name = deployment(env)
    chains = {}
    for domain, chain_id in CHAIN_IDS.items():
        chains[str(domain)] = {
            "domain": domain,
            "chain_id": chain_id,
            "name": DOMAIN_NAMES.get(domain, str(domain)),
            "usdc": USDC[domain],
            "messenger": MESSENGERS[name][domain],
        }
    return {
        "deployment": name,
        "chains": chains,
        "watched_domains": sorted(CHAIN_IDS),
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

