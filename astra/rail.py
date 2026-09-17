"""The rail.

One transfer, one pass:

    observe    read the source transaction's message and the destination's verdict
    verify     check on chain that the contract about to be called is the one the
               message belongs to
    decide     complete / defer / refuse, by the pure classifier
    act        hand the signed attestation to the destination through the
               execution layer, then read the receipt back
    record     write a receipt: what was seen, what was decided, what moved

The rail never signs, never holds a key, and never treats its own bookkeeping as
the source of truth. The destination's answer is the answer.
"""
from __future__ import annotations

import json
import os
import time

from . import classifier, protocol
from .attestation import Attestation
from .config import (CHAIN_IDS, LOCAL_DOMAIN_ABI, attestation_base, attestation_version,
                     keeperhub_key, load_env, receipts_dir, rpc_url, transmitter)
from .keeperhub import KeeperHub
from .rpc import Rpc

USED_NONCES_ABI = json.dumps([
    {"inputs": [{"name": "nonce", "type": "bytes32"}], "name": "usedNonces",
     "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
])

RECEIVE_ABI = json.dumps([
    {"inputs": [{"name": "message", "type": "bytes"}, {"name": "attestation", "type": "bytes"}],
     "name": "receiveMessage", "outputs": [{"name": "success", "type": "bool"}],
     "stateMutability": "nonpayable", "type": "function"},
])


class Rail:
    def __init__(self, env: dict):
        self.env = env
        self.kh = KeeperHub(keeperhub_key(env))
        self.attestation = Attestation(attestation_base(env), attestation_version(env))
        self.rpcs = {domain: Rpc(rpc_url(env, domain)) for domain in CHAIN_IDS}
        self.wallet: str | None = None

    # -- observation ------------------------------------------------------
    def executing_wallet(self) -> str:
        if self.wallet is None:
            self.wallet = self.kh.wallet() or ""
        return self.wallet

    def observe(self, request: dict, max_wait: int = 0, interval: int = 15,
                on_wait=None) -> dict:
        source_domain = request["source_domain"]
        burn_tx = request["burn_tx"]
        if max_wait:
            att = self.attestation.await_final(source_domain, burn_tx, max_wait, interval,
                                               on_wait=on_wait)
        else:
            att = self.attestation.by_transaction(source_domain, burn_tx)
        observed: dict = {"attestation": att}
        if att.get("state") == "final":
            try:
                parsed = protocol.parse(att["message"])
                fields, disagreements = protocol.merge(parsed, att.get("decoded"))
                observed["message"] = fields
                observed["disagreements"] = disagreements
            except (protocol.BadMessage, ValueError) as exc:
                observed["message_error"] = str(exc)
                if att.get("decoded"):
                    observed["message"] = protocol.from_service(att["decoded"])
                    observed["disagreements"] = []
                else:
                    observed["message"] = None
        observed["executing_wallet"] = self.executing_wallet()
        return observed

    def verify_transmitter(self, domain: int) -> dict:
        """Is the contract we are about to call the one this domain's rail talks to?

        A message names its destination domain; a transmitter answers with the
        domain it serves. If those disagree, the call would either revert or, far
        worse, be aimed at a contract that is not part of this protocol. The rail
        reads the answer rather than assuming it.
        """
        address = transmitter(self.env, domain)
        if address is None:
            return {"address": None, "http": None, "reported_domain": None, "matches": False,
                    "detail": "this deployment is not present on the destination chain"}
        status, body = self.kh.read(CHAIN_IDS[domain], address, "localDomain", LOCAL_DOMAIN_ABI)
        reported = None
        if isinstance(body, dict):
            raw = body.get("result")
            if isinstance(raw, str):
                try:
                    reported = int(raw, 16) if raw.startswith("0x") else int(raw)
                except ValueError:
                    reported = None
        return {"address": address, "http": status, "reported_domain": reported,
                "matches": reported == domain}

    def destination_record(self, domain: int, message: dict) -> dict:
        """Ask the destination what it has done with this transfer.

        The contract keeps its own record of every transfer it has delivered, so
        the rail asks it rather than reading a revert string and hoping the text
        says what happened. When the answer cannot be read, the rail says that
        too instead of guessing in either direction.
        """
        nonce = message.get("nonce")
        address = transmitter(self.env, domain)
        if address is None:
            return {"nonce": nonce, "used": None,
                    "detail": "this deployment is not present on the destination chain"}
        if not nonce:
            return {"nonce": None, "used": None, "detail": "the message carries no nonce to ask about"}
        status, body = self.kh.read(CHAIN_IDS[domain], address, "usedNonces", USED_NONCES_ABI,
                                    [nonce])
        raw = body.get("result") if isinstance(body, dict) else None
        used = None
        if isinstance(raw, str) and raw not in ("", "0x"):
            try:
                used = int(raw, 16) != 0
            except ValueError:
                used = None
        return {"nonce": nonce, "used": used, "http": status,
                "detail": "read from the destination contract" if used is not None
                else "the destination did not answer for this nonce"}

    def preflight(self, destination_domain: int, attestation: dict):
        if transmitter(self.env, destination_domain) is None:
            return None
        status, body = self.kh.simulate(
            CHAIN_IDS[destination_domain], transmitter(self.env, destination_domain),
            "receiveMessage", RECEIVE_ABI,
            [attestation["message"], attestation["attestation"]])
        verdict = classifier.interpret_preflight(body)
        verdict["http"] = status
        return verdict

    # -- the pass ---------------------------------------------------------
    def run(self, request: dict, max_wait: int = 0, interval: int = 15, on_wait=None,
            dry_run: bool = False) -> dict:
        started = time.time()
        observed = self.observe(request, max_wait=max_wait, interval=interval, on_wait=on_wait)
        destination_domain = request["destination_domain"]

        observed["transmitter_address"] = transmitter(self.env, destination_domain)
        observed["transmitter_check"] = self.verify_transmitter(destination_domain)
        preflight = None
        if (observed["attestation"].get("state") == "final" and observed.get("message")
                and observed["transmitter_address"] is not None):
            observed["destination_record"] = self.destination_record(destination_domain,
                                                                     observed["message"])
            preflight = self.preflight(destination_domain, observed["attestation"])
        observed["preflight"] = preflight

        decision = classifier.decide(request, observed)
        if dry_run and decision["action"] == classifier.COMPLETE:
            decision = {"action": "complete", "reason": "READY_DRY_RUN",
                        "detail": "attestation final, message matches the request, destination accepts "
                                  "the delivery; nothing was broadcast because this is a read"}

        execution = None
        if decision["action"] == classifier.COMPLETE and not dry_run and observed["transmitter_address"]:
            message = observed["message"]
            execution = self.kh.execute(
                CHAIN_IDS[destination_domain], transmitter(self.env, destination_domain),
                "receiveMessage", RECEIVE_ABI,
                [observed["attestation"]["message"], observed["attestation"]["attestation"]],
                idem=f"astra-{message['source_domain']}-{message['nonce'][-12:]}")

        receipt = {
            "transfer_id": protocol.transfer_id(observed["message"]) if observed.get("message")
            else f"{request['source_domain']}:{request['burn_tx']}",
            "request": request,
            "observed": {
                "attestation_state": observed["attestation"].get("state"),
                "attestation_status": observed["attestation"].get("status"),
                "message": observed.get("message"),
                "message_error": observed.get("message_error"),
                "transmitter_address": observed["transmitter_address"],
                "transmitter_check": observed["transmitter_check"],
                "preflight": preflight,
                "destination_record": observed.get("destination_record"),
                "executing_wallet": observed.get("executing_wallet"),
            },
            "decision": decision,
            "dry_run": dry_run,
            "execution": execution,
            "transaction_hash": self.kh.transaction_hash(execution) if execution else None,
            "transaction_link": self.kh.transaction_link(execution) if execution else None,
            "seconds": round(time.time() - started, 1),
        }
        return receipt

    # -- receipts ---------------------------------------------------------
    @staticmethod
    def write_receipt(receipt: dict, env: dict | None = None) -> str:
        """Write one receipt per decision, never one file per transfer.

        A refusal is evidence too, and it must not overwrite the receipt of the
        delivery that made the refusal correct: the pair of them is the story.
        """
        folder = receipts_dir(env or load_env())
        os.makedirs(folder, exist_ok=True)
        name = str(receipt["transfer_id"]).replace(":", "-").replace("0x", "")[:20]
        action = str((receipt.get("decision") or {}).get("action", "decision"))
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        path = os.path.join(folder, f"{stamp}-{name}-{action}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(receipt, fh, indent=2)
        return path
