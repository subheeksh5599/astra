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

from astra import classifier, inflight, pairing, payer, transfers  # noqa: E402
from astra.attestation import Attestation  # noqa: E402
from astra.config import (CHAIN_IDS, DOMAIN_NAMES, MESSENGERS, USDC, attestation_base,  # noqa: E402
                          attestation_version, deployment, load_env, read_only, receipts_dir,
                          rpc_url, supports, transmitter, watch_dir, written_receipt_dirs)
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

#: How many transfers one instance will broadcast in a window. The rail is
#: permissionless by design, but an endpoint anyone can poke should not be able to
#: spend an unbounded amount of the executor's gas.
EXECUTE_LIMIT = 6
EXECUTE_WINDOW_SECONDS = 600
_EXECUTIONS: list = []
_EXECUTE_LOCK = threading.Lock()


def executions_recently() -> int:
    now = time.time()
    with _EXECUTE_LOCK:
        _EXECUTIONS[:] = [at for at in _EXECUTIONS if now - at < EXECUTE_WINDOW_SECONDS]
        return len(_EXECUTIONS)


def note_execution() -> None:
    with _EXECUTE_LOCK:
        _EXECUTIONS.append(time.time())
_SCAN_LOCK = threading.Lock()
_SCAN_CACHE: dict = {}


EXPLORERS = {
    0: "https://sepolia.etherscan.io",
    2: "https://sepolia-optimism.etherscan.io",
    3: "https://sepolia.arbiscan.io",
    6: "https://sepolia.basescan.org",
    7: "https://amoy.polygonscan.com",
}


def explorer_link(domain, tx_hash: str) -> str | None:
    base = EXPLORERS.get(domain)
    return f"{base}/tx/{tx_hash}" if base and tx_hash else None


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
            "explorer": EXPLORERS.get(domain, ""),
        }
    routes = []
    # chains is keyed by string domain for JSON; the config lookups are keyed by int
    domains = sorted(int(d) for d in chains)
    for source in domains:
        for destination in domains:
            if source == destination:
                continue
            routes.append({
                "source_domain": source,
                "destination_domain": destination,
                "ok": transmitter(env, destination) is not None,
                "note": ("" if transmitter(env, destination) is not None
                         else "this deployment is not on the destination chain"),
            })
    return {
        "deployment": name,
        "read_only": read_only(env),
        "chains": chains,
        "routes": routes,
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
        # A page that ships with its own client must never be served from a cache: a
        # stale app.js against a new API is a bug report about the wrong thing.
        self.send_header("Cache-Control", "no-store, must-revalidate")
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
        if path == "/api/payer":
            return self.payer_status(env)
        if path == "/api/pairing":
            return self.pairing(query)
        if path == "/api/receipts":
            return self.receipts()
        if path == "/api/balance":
            return self.balance(query, env)
        if path == "/api/transfers":
            return self.transfer_list(query, env)
        if path.startswith("/api/transfer/"):
            return self.transfer_get(path, env)
        if path.startswith("/api/receipt/"):
            return self.receipt(path.rsplit("/", 1)[-1])
        return self.static(path)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path.rstrip("/") == "/api/complete":
            return self.complete(self.read_body())
        if parsed.path.rstrip("/") == "/api/open":
            return self.open_transfer(self.read_body())
        if parsed.path.rstrip("/") == "/api/transfer/prepare":
            return self.transfer_prepare(self.read_body())
        if parsed.path.rstrip("/") == "/api/transfer/register":
            return self.transfer_register(self.read_body())
        if parsed.path.rstrip("/").endswith("/execute") and "/api/transfer/" in parsed.path:
            return self.transfer_execute(self.read_body())
        return self.send_json({"error": "unknown route", "path": parsed.path}, 404)

    # -- handlers ---------------------------------------------------------
    def inflight(self, query, env):
        seconds = int((query.get("seconds") or ["3600"])[0])
        blocks = int((query.get("blocks") or ["0"])[0])
        limit = int((query.get("limit") or ["12"])[0])
        key = (seconds, blocks, limit)
        now = time.time()
        with _SCAN_LOCK:
            cached = _SCAN_CACHE.get(key)
            if cached and now - cached["at"] < SCAN_TTL_SECONDS:
                return self.send_json({"rows": cached["rows"], "blocks": blocks,
                                       "age_seconds": round(now - cached["at"], 1),
                                       "cached": True})
        try:
            rows = build_inflight(env, blocks, limit, seconds=seconds)
        except Exception as exc:  # noqa: BLE001
            return self.send_json({"error": str(exc)[:300]}, 502)
        with _SCAN_LOCK:
            _SCAN_CACHE[key] = {"rows": rows, "at": time.time()}
        return self.send_json({"rows": rows, "blocks": blocks, "seconds": seconds,
                               "age_seconds": 0.0, "cached": False})

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

    def pairing(self, query):
        """The invariant over a window, and the passes that run it on a schedule.

        The page shows what the collection cannot: whether every burn in the window
        has exactly one mint, how much value actually arrived, and the journal of
        scheduled passes. Read-only; nothing here moves anything.
        """
        seconds = int((query.get("seconds") or ["3600"])[0])
        blocks = int((query.get("blocks") or ["0"])[0])
        limit = int((query.get("limit") or ["14"])[0])
        key = ("pairing", seconds, blocks, limit)
        now = time.time()
        with _SCAN_LOCK:
            cached = _SCAN_CACHE.get(key)
            if cached and now - cached["at"] < SCAN_TTL_SECONDS:
                return self.send_json({**cached["rows"], "cached": True,
                                       "age_seconds": round(now - cached["at"], 1)})
        try:
            result = pairing.collect(blocks=blocks, limit=limit, seconds=seconds)
        except Exception as exc:  # noqa: BLE001
            return self.send_json({"error": str(exc)[:300]}, 502)
        broken = pairing.broken(result)
        payload = {
            "blocks": blocks,
            "span_seconds": result.get("span_seconds"),
            "rates": result.get("rates"),
            "summary": pairing.summarise(result),
            "rows": result["rows"],
            "broken": broken,
            "domains": result["domains"],
            "passes": self.watch_journal(5),
            "cached": False,
            "age_seconds": 0.0,
        }
        with _SCAN_LOCK:
            _SCAN_CACHE[key] = {"rows": payload, "at": time.time()}
        return self.send_json(payload)

    @staticmethod
    def watch_journal(limit: int = 5) -> list:
        """The last few scheduled passes, as the watcher wrote them."""
        folder = watch_dir(load_env())
        out = []
        if not os.path.isdir(folder):
            return out
        for name in sorted(os.listdir(folder), reverse=True)[:limit]:
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(folder, name), encoding="utf-8") as fh:
                    entry = json.load(fh)
            except (OSError, ValueError):
                continue
            out.append({"at": entry.get("at"), "pass": entry.get("pass"),
                        "summary": entry.get("summary"), "window_blocks": entry.get("window_blocks"),
                        "broken": len(entry.get("broken") or []), "seconds": entry.get("seconds"),
                        "name": name})
        return out

    def payer_status(self, env):
        """Who can pay from this machine, and what that key holds where.

        Nothing signs here. The control surface asks this before offering to create
        a transfer, so a person is told where their value is instead of discovering
        it at signing time.
        """
        try:
            who = payer.address(env)
            rows = payer.balances(env)
        except payer.PayerUnavailable as exc:
            return self.send_json({"configured": False, "detail": str(exc)})
        except Exception as exc:  # noqa: BLE001
            return self.send_json({"configured": True, "detail": str(exc)[:300]}, 502)
        return self.send_json({"configured": True, "address": who, "chains": rows})

    def open_transfer(self, body):
        """Create a transfer from the key this machine holds, and optionally finish it.

        The burn is the payer's own transaction; the finishing half is the rail's
        and goes through the execution layer. Both are reported separately, because
        they are different transactions with different signers and a receipt that
        blurred them would be the most misleading thing this project could emit.
        """
        env = load_env()
        body = body or {}
        if read_only(env):
            return self.send_json({
                "error": "this instance holds no paying key: sign the burn in your own wallet, "
                         "or run the rail locally where a key is configured",
                "read_only": True,
            }, 403)
        steps: list = []

        def step(message: str) -> None:
            steps.append({"at": time.strftime("%H:%M:%SZ", time.gmtime()), "step": str(message)[:200]})

        try:
            amount = float(body.get("amount") or 0)
            source = int(body.get("source_domain", 6))
            destination = int(body.get("destination_domain", 0))
        except (TypeError, ValueError):
            return self.send_json({"error": "amount, source_domain and destination_domain must be numbers"}, 400)
        caller = (body.get("caller") or "").strip() or None
        deployment_name = deployment(env)

        try:
            transfer = payer.open_transfer(env, amount, source, destination, caller=caller,
                                           deployment=deployment_name, on_step=step)
        except (payer.PayerUnavailable, ValueError, RuntimeError) as exc:
            return self.send_json({"error": str(exc)[:300], "steps": steps}, 400)
        except Exception as exc:  # noqa: BLE001
            return self.send_json({"error": str(exc)[:300], "steps": steps}, 502)

        result = {
            "transfer": transfer,
            "steps": steps,
            "burn_link": explorer_link(transfer["source_domain"], transfer["burn_tx"]),
        }

        if body.get("finish"):
            wait = int(body.get("wait_seconds") or 900)
            step(f"waiting up to {wait}s for the source chain to finalise")
            try:
                rail = Rail(env)
                if executions_recently() >= EXECUTE_LIMIT:
                    raise RuntimeError(f"this instance has broadcast {EXECUTE_LIMIT} transfers in the "
                                       f"last {EXECUTE_WINDOW_SECONDS // 60} minutes")
                receipt = rail.run({"source_domain": source, "destination_domain": destination,
                                    "burn_tx": transfer["burn_tx"]},
                                   max_wait=wait, interval=10,
                                   on_wait=lambda state, reads, left: step(
                                       f"attestation {state.get('state')} after {reads} reads, {left}s left"))
                if (receipt.get("decision") or {}).get("action") == classifier.COMPLETE:
                    note_execution()
                rail.write_receipt(receipt, env)
                result["receipt"] = receipt
                result["mint_link"] = receipt.get("transaction_link")
                step(f"destination said: {(receipt.get('decision') or {}).get('reason')}")
            except Exception as exc:  # noqa: BLE001
                result["finish_error"] = str(exc)[:300]
                step(f"finishing failed: {str(exc)[:200]}")
        return self.send_json(result)

    # -- real transfers, the application's own boundary --------------------
    def balance(self, query, env):
        """Balances for whatever address asked, read from the chain it names.

        The page never shows a balance it computed itself, and this is the only
        place a balance comes from.
        """
        address = (query.get("address") or [""])[0].strip()
        if not address.startswith("0x") or len(address) != 42:
            return self.send_json({"error": "address must be a 20-byte address"}, 400)
        try:
            domain = int((query.get("domain") or ["0"])[0])
        except ValueError:
            return self.send_json({"error": "domain must be a number"}, 400)
        if domain not in CHAIN_IDS or not supports(env, domain):
            return self.send_json({"error": f"domain {domain} is not a chain this rail watches"}, 400)
        rpc = Rpc(rpc_url(env, domain))
        token = USDC[domain]
        messenger = MESSENGERS[deployment(env)][domain]
        out = {"address": address, "domain": domain, "chain_id": CHAIN_IDS[domain],
               "name": DOMAIN_NAMES.get(domain, str(domain)), "token": token,
               "messenger": messenger, "explorer": EXPLORERS.get(domain, "")}
        try:
            out["token_balance"] = rpc.balance_of(token, address)
            out["native_balance"] = int(rpc.call("eth_getBalance", [address, "latest"]), 16)
            raw = rpc.call_contract(token, "0xdd62ed3e" + "0" * 24 + address[2:].lower()
                                    + "0" * 24 + messenger[2:].lower())
            out["allowance"] = int(raw, 16) if raw and raw != "0x" else 0
            out["head"] = rpc.block_number()
        except Exception as exc:  # noqa: BLE001
            return self.send_json({**out, "error": f"the chain did not answer: {str(exc)[:200]}"}, 502)
        return self.send_json(out)

    def transfer_prepare(self, body):
        """Validate a transfer before a wallet is asked to sign anything."""
        try:
            prepared = transfers.prepare(load_env(), body or {})
        except Exception as exc:  # noqa: BLE001
            return self.send_json({"error": str(exc)[:300]}, 502)
        return self.send_json(prepared, 200 if prepared.get("ok") else 422)

    def transfer_register(self, body):
        """Keep a burn the user just made, under the protocol's own identifier."""
        env = load_env()
        try:
            registered = transfers.register(env, body or {})
        except ValueError as exc:
            return self.send_json({"error": str(exc)[:300]}, 400)
        except Exception as exc:  # noqa: BLE001
            return self.send_json({"error": str(exc)[:300]}, 502)
        tid = registered["transfer_id"]
        return self.send_json({**registered, "state": transfers.state_of(env, registered["record"])})

    def transfer_list(self, query, env):
        """This instance's transfers, or one wallet's, never anybody else's."""
        owner = (query.get("address") or [""])[0].strip() or None
        records = transfers.list_records(env, owner=owner)
        out = []
        for record in records[:100]:
            item = transfers.summary(record)
            item["source_link"] = explorer_link(item["source_domain"], item.get("source_tx"))
            item["destination_link"] = explorer_link(item["destination_domain"],
                                                     item.get("destination_tx"))
            out.append(item)
        return self.send_json({"transfers": out, "address": owner, "count": len(out)})

    def transfer_get(self, path, env):
        parts = [p for p in path.split("/") if p and p != "api"]
        if len(parts) < 2 or parts[0] != "transfer":
            return self.send_json({"error": "unknown route", "path": path}, 404)
        transfer_id = parts[1]
        want_evidence = len(parts) > 2 and parts[2] == "evidence"
        record = transfers.load(env, transfer_id)
        if not record:
            return self.send_json({"error": "no such transfer", "transfer_id": transfer_id}, 404)
        try:
            state = transfers.state_of(env, record)
        except Exception as exc:  # noqa: BLE001
            return self.send_json({"error": str(exc)[:300], "transfer_id": transfer_id}, 502)
        state["transfer_id"] = transfer_id
        state["links"] = {
            "source": explorer_link(int(record["source_domain"]), record.get("source_tx")),
            "destination": explorer_link(state.get("destination", {}).get("domain"),
                                         (state.get("delivered") or {}).get("transaction_hash")),
            "contract": (f"{EXPLORERS.get(state.get('destination', {}).get('domain'), '')}"
                         f"/address/{state.get('destination', {}).get('transmitter')}"
                         if state.get("destination", {}).get("transmitter") else None),
        }
        if want_evidence:
            return self.send_json(state)
        return self.send_json({k: v for k, v in state.items() if k != "record"})

    def transfer_execute(self, body):
        """Attempt the delivery, after rereading everything the guards depend on."""
        env = load_env()
        transfer_id = str((body or {}).get("transfer_id") or "").strip()
        record = transfers.load(env, transfer_id)
        if not record:
            return self.send_json({"error": "no such transfer", "transfer_id": transfer_id}, 404)
        if executions_recently() >= EXECUTE_LIMIT:
            return self.send_json({
                "error": f"this instance has broadcast {EXECUTE_LIMIT} transfers in the last "
                         f"{EXECUTE_WINDOW_SECONDS // 60} minutes",
                "state": transfers.state_of(env, record)["state"], "attempted": False}, 429)
        try:
            result = transfers.execute(env, record)
        except Exception as exc:  # noqa: BLE001
            return self.send_json({"error": str(exc)[:300], "transfer_id": transfer_id,
                                   "attempted": False}, 502)
        if result.get("attempted"):
            note_execution()
        return self.send_json({**result, "transfer_id": transfer_id,
                               "links": {"destination": (result.get("attempt") or {})
                                         .get("transaction_link")}})

    def receipts(self):
        """Every receipt this instance can see, newest first.

        A hosted instance is read-only, so it serves the receipts committed to the
        repository plus anything it managed to write where it is allowed to. Both
        are receipts; the page does not need to care which is which.
        """
        seen: dict = {}
        for folder in written_receipt_dirs(load_env()):
            if not os.path.isdir(folder):
                continue
            for name in os.listdir(folder):
                if not name.endswith(".json") or name in seen:
                    continue
                try:
                    with open(os.path.join(folder, name), encoding="utf-8") as fh:
                        data = json.load(fh)
                except (OSError, ValueError):
                    continue
                seen[name] = {
                    "name": name,
                    "transfer_id": data.get("transfer_id"),
                    "decision": data.get("decision"),
                    "transaction_hash": data.get("transaction_hash"),
                    "transaction_link": data.get("transaction_link"),
                }
        out = [seen[name] for name in sorted(seen, reverse=True)]
        return self.send_json({"receipts": out})

    def receipt(self, name):
        if not name.replace("-", "").replace("_", "").isalnum():
            return self.send_json({"error": "bad receipt name"}, 400)
        for folder in written_receipt_dirs(load_env()):
            path = os.path.join(folder, f"{name}.json")
            if os.path.exists(path):
                with open(path, encoding="utf-8") as fh:
                    return self.send_json(json.load(fh))
        return self.send_json({"error": "no such receipt"}, 404)

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
        if executions_recently() >= EXECUTE_LIMIT:
            return self.send_json({
                "error": f"this instance has broadcast {EXECUTE_LIMIT} transfers in the last "
                         f"{EXECUTE_WINDOW_SECONDS // 60} minutes; try again shortly",
            }, 429)
        try:
            rail = Rail(env)
            receipt = rail.run(request, max_wait=wait, interval=10) if wait else rail.run(request)
            if (receipt.get("decision") or {}).get("action") == classifier.COMPLETE:
                note_execution()
            rail.write_receipt(receipt, env)
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


def scan_window(env: dict, domain: int, blocks: int, seconds: int) -> tuple:
    """How far back to read one chain, and the rate that decision came from.

    A window given in seconds is the same span of history on every chain; one given
    in blocks is not, because a block is a different amount of time on each of them.
    The rate is measured from the chain itself, and the request is kept inside what
    a public endpoint will answer.
    """
    rpc = Rpc(rpc_url(env, domain))
    if not seconds:
        return rpc, blocks
    rate = rpc.blocks_per_second()
    if rate <= 0:
        return rpc, blocks or 1200
    return rpc, min(50_000, max(200, int(seconds * rate)))


def build_inflight(env: dict, blocks: int, limit: int, seconds: int = 0) -> list:
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
            rpc, window = scan_window(env, domain, blocks, seconds)
            # A cap per chain keeps one busy chain from filling the whole view:
            # the point of watching five is seeing five.
            return inflight.scan(rpc, domain, window, deployment=name)[:per_domain]
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
