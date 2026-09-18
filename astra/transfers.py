"""Transfers, as the application sees them.

The rail already knows how to move value: it observes a burn, decides whether the
destination may be handed the attestation, and executes through the execution
layer. What it did not have was a way for a person to *make* a transfer and then
keep watching it. This module is that, and nothing more:

    prepare   what the source chain needs before a wallet can be asked to sign
    register  read the source receipt, decode the protocol's own event, and keep
              the transfer under the identifier the protocol uses: domain + nonce
    state_of  what both chains say about it right now, as one state and the
              evidence that produced it
    store     records on disk, so a transfer survives a refresh and does not
              belong to whatever tab opened it

Nothing here signs, and nothing here trusts the client. Every field that matters
is reread from the chains or from the attestation service before it is acted on.
"""
from __future__ import annotations

import json
import os
import time

from . import classifier, payer, protocol, source
from .attestation import Attestation
from .config import (CHAIN_IDS, DOMAIN_NAMES, MESSENGERS, USDC, attestation_base,
                     attestation_version, deployment, rpc_url, supports, transmitter)
from typing import Optional  # noqa: F401  (the state bundle is a plain dict on purpose)
from .inflight import decode_message_sent
from .rpc import Rpc

#: The states a transfer can be in. Each one is derived from something the chains
#: or the attestation service actually said; none of them is a frontend invention.
STATES = (
    "CREATED",              # a record exists; the source transaction is not on chain yet
    "SOURCE_PENDING",       # broadcast, no receipt yet
    "SOURCE_CONFIRMED",     # the receipt is successful and carries the message
    "WAITING_FOR_FINALITY",  # the attestation service has not signed it yet
    "ATTESTATION_READY",    # signed, not yet checked against the destination
    "DELIVERY_READY",       # every guard passes; delivery is ours to attempt
    "DELIVERY_SUBMITTED",   # an execution attempt has a transaction hash
    "DELIVERED",            # the destination's own receipt and token event agree
    "REFUSED",              # the protocol says this must not be delivered
    "FAILED",               # the source transaction reverted
    "STRANDED",             # signed and deliverable, and nothing has moved it
)

TERMINAL = ("DELIVERED", "REFUSED", "FAILED", "STRANDED")

#: After this long, a transfer that could have been delivered and has not been is
#: reported as stranded rather than merely ready. The number is not a protocol
#: constant; it is the point at which "not yet" stops being a plausible reading.
STRANDED_SECONDS = 1800


# ---------------------------------------------------------------- storage

def _int(value, default: int = 0) -> int:
    """Chain fields can be missing or null; a cast that raises on either is a crash
    waiting for the one receipt that is shaped differently."""
    try:
        if value is None:
            return default
        return int(value, 16) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        return default


def store_dir(env: dict) -> str:
    """Where transfer records live.

    A hosted instance cannot write into the repository, so it keeps them where it
    is allowed to write and still serves them; a local run keeps them with the
    other evidence. Both are the same shape.
    """
    override = env.get("ASTRA_TRANSFER_DIR")
    if override:
        return override
    if str(env.get("ASTRA_READ_ONLY") or "").lower() in ("1", "true", "yes"):
        return os.path.join("/tmp", "astra-transfers")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "artifacts", "transfers")


def _path(env: dict, transfer_id: str) -> str:
    safe = "".join(c for c in transfer_id if c.isalnum() or c in "-_")
    return os.path.join(store_dir(env), f"{safe}.json")


def save(env: dict, record: dict) -> str:
    folder = store_dir(env)
    os.makedirs(folder, exist_ok=True)
    record = dict(record)
    record["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    path = _path(env, record["transfer_id"])
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2, sort_keys=True, default=str)
    return path


def load(env: dict, transfer_id: str) -> dict | None:
    path = _path(env, transfer_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def list_records(env: dict, owner: str | None = None) -> list:
    """Every transfer this instance knows, newest first, optionally one wallet's.

    A wallet only ever sees its own: `owner` is the address that signed the burn,
    recorded when the transfer was registered, and compared as an address.
    """
    folder = store_dir(env)
    if not os.path.isdir(folder):
        return []
    out = []
    for name in sorted(os.listdir(folder), reverse=True):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(folder, name), encoding="utf-8") as fh:
                record = json.load(fh)
        except (OSError, ValueError):
            continue
        if owner and (record.get("owner") or "").lower() != owner.lower():
            continue
        out.append(record)
    out.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return out


def find_by_burn(env: dict, source_domain: int, burn_tx: str) -> dict | None:
    for record in list_records(env):
        if (record.get("source_domain") == source_domain
                and (record.get("source_tx") or "").lower() == (burn_tx or "").lower()):
            return record
    return None


# ---------------------------------------------------------------- source side

BURN_V2 = ("depositForBurn(uint256,uint32,bytes32,address,bytes32,uint256,uint32)")
BURN_V1 = ("depositForBurn(uint256,uint32,bytes32,address)")


def burn_arguments(env: dict, amount: int, destination: int, recipient: str,
                   token: str, caller: str | None, threshold: int | None = None) -> list:
    """The arguments the wallet must send, in the order the protocol expects.

    Built here rather than in the browser so the caller restriction, the fee cap
    and the finality threshold are the rail's numbers and not the page's.
    """
    name = deployment(env)
    if name == "v1" and caller:
        raise ValueError("this deployment's burn takes no destination caller")
    if name == "v1":
        return [str(amount), str(destination), payer.encode_recipient(recipient), token]
    thresholds = {"v1": payer.STANDARD_THRESHOLD}
    limit = threshold or thresholds.get(name, payer.FAST_THRESHOLD)
    return [str(amount), str(destination), payer.encode_recipient(recipient), token,
            payer.encode_caller(caller), str(max(1, amount // payer.FEE_CAP_PER_THOUSAND)),
            str(limit)]


def signer_of(env: dict, source_domain: int, burn_tx: str) -> dict:
    """Who signed the source transaction, read from the chain's own view of it."""
    rpc = Rpc(rpc_url(env, source_domain))
    tx = rpc.call("eth_getTransactionByHash", [burn_tx]) or {}
    return {"from": (tx.get("from") or "").lower() or None, "to": (tx.get("to") or "").lower() or None}


def source_evidence(env: dict, source_domain: int, burn_tx: str) -> dict:
    """Everything the source chain holds about one burn, witness by witness.

    The identifier a transfer is stored under is the transfer itself — the chain
    it was burned on and the transaction that burned it — because that is known
    the moment the receipt exists. The protocol's own nonce is added to the record
    as soon as the attestation service or the destination reports it; it is never
    guessed at, and a transfer with no nonce is not a transfer with a made-up one.
    """
    name = deployment(env)
    address = transmitter(env, source_domain)
    token = USDC.get(source_domain)
    messenger = MESSENGERS[name].get(source_domain)
    rpc = Rpc(rpc_url(env, source_domain))
    out: dict = {
        "source_domain": source_domain,
        "source_chain": DOMAIN_NAMES.get(source_domain, str(source_domain)),
        "source_tx": burn_tx,
        "chain_id": CHAIN_IDS.get(source_domain),
        "token": token,
        "messenger": messenger,
        "transmitter": address,
        "deployment": name,
    }
    receipt = rpc.transaction_receipt(burn_tx)
    if not receipt:
        out.update({"status": "pending", "detail": "the source chain has no receipt for this transaction yet"})
        return out

    tx = rpc.call("eth_getTransactionByHash", [burn_tx]) or {}
    logs = receipt.get("logs") or []
    out["status"] = "success" if _int(receipt.get("status")) == 1 else "reverted"
    out["block"] = _int(receipt.get("blockNumber"))
    out["confirmations"] = max(0, rpc.block_number() - out["block"])
    out["from"] = (tx.get("from") or "").lower() or None
    out["to"] = (tx.get("to") or "").lower() or None
    out["logs"] = len(logs)
    if out["status"] != "success":
        out["detail"] = "the source transaction reverted; nothing was burned"
        return out

    calldata = source.decode_burn_calldata(tx.get("input") or "")
    event = source.deposit_for_burn(logs, messenger)
    burned = source.burn_event(logs, token)
    announced = source.message_sent(receipt, address)

    out["witnesses"] = {
        "calldata": calldata,
        "deposit_for_burn": event,
        "burn": burned,
        "message_sent": {k: v for k, v in (announced or {}).items() if k != "message"},
    }
    out["agreement"] = source.agree(calldata, event, burned)

    # The protocol's own event is the record the application reads its fields from:
    # it is what the protocol accepted, and it carries the recipient and the caller.
    fields = event or calldata
    if not fields:
        out["detail"] = ("the transaction succeeded but carries no burn this rail can "
                         "read; it is not a transfer")
        return out

    out["destination_domain"] = fields.get("destination_domain")
    out["destination_chain"] = DOMAIN_NAMES.get(fields.get("destination_domain"),
                                                str(fields.get("destination_domain")))
    out["amount"] = fields.get("amount")
    out["amount_usdc"] = (fields.get("amount") or 0) / 1e6
    out["recipient"] = fields.get("mint_recipient") or fields.get("recipient")
    out["caller"] = fields.get("destination_caller") or fields.get("caller")
    out["burn_token"] = fields.get("burn_token")
    out["max_fee"] = fields.get("max_fee")
    out["min_finality_threshold"] = fields.get("min_finality_threshold")
    out["message"] = (announced or {}).get("message")
    out["message_bytes"] = (announced or {}).get("bytes")
    out["message_witness"] = (announced or {}).get("witness")
    out["rail_decode"] = (announced or {}).get("rail_decode")
    out["detail"] = ("read from " + (event or calldata)["witness"]
                     + ("; the token's own burn event agrees on the amount"
                        if not out["agreement"]["disagreements"] else
                        "; the witnesses disagree on the amount"))
    return out


def transfer_id_of(source_domain: int, burn_tx: str) -> str:
    """The identifier the application stores a transfer under.

    The chain and the transaction: both are known the instant the burn exists, both
    are immutable, and neither is invented. The protocol's nonce is carried in the
    record as a field, added when a witness reports it.
    """
    return f"{source_domain}-{(burn_tx or '').removeprefix('0x').lower()}"


# ---------------------------------------------------------------- preparation

def prepare(env: dict, body: dict) -> dict:
    """Everything the wallet needs, and every reason it must not be asked yet.

    The page is not trusted for any of this: the routes come from the rail's
    configuration, the allowance and the balances come from the chains, and the
    destination contract is asked for the domain it serves before the burn is
    offered.
    """
    checks: list = []

    def check(name: str, ok: bool, detail: str) -> bool:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        return bool(ok)

    try:
        source = int(body.get("source_domain"))
        destination = int(body.get("destination_domain"))
    except (TypeError, ValueError):
        return {"ok": False, "errors": ["source_domain and destination_domain are required"],
                "checks": checks}
    try:
        amount_usdc = float(body.get("amount_usdc") or 0)
    except (TypeError, ValueError):
        amount_usdc = 0.0
    recipient = (body.get("recipient") or "").strip()
    caller = (body.get("caller") or "").strip() or None
    sender = (body.get("address") or "").strip()
    amount = int(round(amount_usdc * 1_000_000))

    ok_source = check("source chain", source in CHAIN_IDS and supports(env, source),
                      f"{DOMAIN_NAMES.get(source, source)} domain {source}")
    ok_destination = False
    detail = "not a chain this rail watches"
    if destination in CHAIN_IDS and supports(env, destination):
        address = transmitter(env, destination)
        if address is None:
            detail = (f"{DOMAIN_NAMES.get(destination, destination)} is watched, but this "
                      f"deployment is not on it")
        else:
            ok_destination = True
            detail = f"{DOMAIN_NAMES.get(destination, destination)} domain {destination}"
    ok_destination = check("destination chain", ok_destination, detail)

    ok_recipient = False
    try:
        payer.encode_recipient(recipient)
        ok_recipient = len(recipient) == 42 and recipient.startswith("0x")
        detail = recipient if ok_recipient else f"{recipient} is not an address"
    except Exception:  # noqa: BLE001
        detail = "a recipient address is required"
    check("recipient", ok_recipient, detail)

    ok_caller = True
    caller_detail = "anyone may deliver this transfer"
    try:
        payer.encode_caller(caller)
        if caller:
            caller_detail = f"only {caller} may deliver this transfer"
            if deployment(env) == "v1":
                ok_caller = False
                caller_detail = "this deployment's burn takes no destination caller"
    except ValueError as exc:
        ok_caller = False
        caller_detail = str(exc)
    check("destination caller", ok_caller, caller_detail)

    check("amount", amount > 0, f"{amount} units, {amount_usdc} USDC" if amount > 0
          else "an amount greater than zero is required")
    if amount > 0 and deployment(env) != "v1" and amount_usdc > 1000:
        check("fast threshold", False,
              "the fast threshold takes transfers up to 1,000 USDC per message")

    token = USDC.get(source)
    messenger = MESSENGERS[deployment(env)].get(source)
    if not token or not messenger:
        return {"ok": False, "errors": [f"this rail has no token or messenger for domain {source}"],
                "checks": checks + [{"check": "rail configuration", "ok": False,
                                     "detail": f"domain {source} has no token or messenger here"}]}
    allowance = None
    balance = None
    native = None
    if ok_source and sender:
        rpc = Rpc(rpc_url(env, source))
        try:
            balance = rpc.balance_of(token, sender)
            native = int(rpc.call("eth_getBalance", [sender, "latest"]), 16)
        except Exception as exc:  # noqa: BLE001
            check("wallet balances", False, f"could not read them: {str(exc)[:120]}")
        else:
            enough = balance >= amount if amount else False
            check("token balance", enough,
                  f"{balance / 1e6:.6f} USDC held, {amount / 1e6:.6f} needed")
            check("gas balance", native > 0, f"{native / 1e18:.6f} ETH for gas")
        try:
            raw = rpc.call_contract(token, "0xdd62ed3e" + "0" * 24 + sender[2:].lower()
                                    + "0" * 24 + messenger[2:].lower())
            allowance = int(raw, 16) if raw and raw != "0x" else 0
            check("allowance", allowance >= amount,
                  f"{allowance / 1e6:.6f} USDC approved to the messenger"
                  + ("" if allowance >= amount else "; an approval is needed first"))
        except Exception as exc:  # noqa: BLE001
            check("allowance", False, f"could not read it: {str(exc)[:120]}")
    elif ok_source:
        check("wallet balances", False, "no wallet address was given to check")

    arguments = None
    if all([ok_source, ok_destination, ok_recipient, ok_caller, amount > 0]):
        arguments = burn_arguments(env, amount, destination, recipient, token, caller)

    errors = [c["detail"] for c in checks if not c["ok"]]
    return {
        "ok": not errors,
        "errors": errors,
        "checks": checks,
        "deployment": deployment(env),
        "source": {"domain": source, "chain_id": CHAIN_IDS.get(source),
                   "name": DOMAIN_NAMES.get(source, str(source)),
                   "token": token, "messenger": messenger},
        "destination": {"domain": destination, "chain_id": CHAIN_IDS.get(destination),
                        "name": DOMAIN_NAMES.get(destination, str(destination)),
                        "transmitter": transmitter(env, destination)},
        "amount": amount,
        "amount_usdc": amount / 1e6 if amount else 0,
        "allowance": allowance,
        "balance": balance,
        "native": native,
        "needs_approval": bool(allowance is not None and amount and allowance < amount),
        "approve": {"token": token, "spender": messenger, "amount": str(amount),
                    "function": "approve(address,uint256)"},
        "burn": {"messenger": messenger,
                 "function": BURN_V1 if deployment(env) == "v1" else BURN_V2,
                 "arguments": arguments},
    }


# ---------------------------------------------------------------- state

def _attestation(env: dict) -> Attestation:
    return Attestation(attestation_base(env), attestation_version(env))


def state_of(env: dict, record: dict, rpc_for: dict | None = None) -> dict:
    """The transfer's state, and the evidence for it.

    Every branch below is a chain read or an attestation answer. When one of them
    cannot be read, the state says which and stays where it was: a rail that
    guesses is worse than a rail that waits.
    """
    source_domain = int(record["source_domain"])
    burn_tx = record["source_tx"]
    errors: list = []

    source = source_evidence(env, source_domain, burn_tx)
    out: dict = {"transfer_id": record.get("transfer_id"), "source": source,
                 "record": record, "errors": errors}

    if source.get("status") == "pending":
        out["state"] = "SOURCE_PENDING"
        out["detail"] = source.get("detail")
        out["evidence"] = [source.get("detail")]
        return _finish(out, record, env)

    if source.get("status") == "reverted":
        out["state"] = "FAILED"
        out["detail"] = source.get("detail")
        out["evidence"] = [source.get("detail")]
        return _finish(out, record, env)

    if not source.get("message"):
        out["state"] = "FAILED"
        out["detail"] = source.get("detail")
        out["evidence"] = [source.get("detail")]
        return _finish(out, record, env)

    destination_domain = source.get("destination_domain")
    if destination_domain is None:
        out["state"] = "FAILED"
        out["detail"] = "the message names no destination domain"
        out["evidence"] = ["a message with no destination domain cannot be delivered"]
        return _finish(out, record, env)
    out["destination"] = {
        "domain": destination_domain,
        "name": DOMAIN_NAMES.get(destination_domain, str(destination_domain)),
        "chain_id": CHAIN_IDS.get(destination_domain),
        "transmitter": transmitter(env, destination_domain),
    }

    # What the source announced, decoded from the message itself.
    # What the source said the transfer is. Read from a witness - the protocol's own
    # event, or the wallet's instruction - and never from a decode that has not been
    # corroborated. The nonce arrives below, from whoever reports it.
    out["message"] = {
        "nonce": record.get("nonce"),
        "source_domain": source_domain,
        "destination_domain": destination_domain,
        "amount": source.get("amount"),
        "amount_usdc": source.get("amount_usdc"),
        "recipient": source.get("recipient"),
        "mint_recipient": source.get("recipient"),
        "caller": source.get("caller"),
        "destination_caller": source.get("caller"),
        "burn_token": source.get("burn_token"),
        "max_fee": source.get("max_fee"),
        "witness": (source.get("witnesses") or {}).get("deposit_for_burn", {}).get("witness")
                   or (source.get("witnesses") or {}).get("calldata", {}).get("witness"),
    }

    attestation = _attestation(env).by_transaction(source_domain, burn_tx)
    out["attestation"] = {
        "state": attestation.get("state"),
        "status": attestation.get("status"),
        "signed": bool(attestation.get("attestation")),
        "decoded": attestation.get("decoded"),
        "detail": attestation.get("detail") if not attestation.get("message") else None,
    }
    # The nonce is the protocol's identifier for this transfer, and the only two
    # places it can be read from are the service's decode and the destination's own
    # record. This rail does not parse it out of bytes it cannot verify.
    service = protocol.from_service(attestation["decoded"]) if attestation.get("decoded") else None
    if service and service.get("nonce"):
        out["message"]["nonce"] = service["nonce"]
        out["message"]["nonce_witness"] = "the attestation service's decode of the message"
        for field in ("mint_recipient", "amount", "burn_token", "destination_caller"):
            if service.get(field) not in (None, "", 0):
                out["message"][field] = service[field]
        if service.get("amount"):
            out["message"]["amount_usdc"] = service["amount"] / 1e6
        if service.get("fee_executed") is not None:
            out["message"]["fee_executed"] = service["fee_executed"]
            out["message"]["fee_witness"] = "the protocol's own message, decoded by the attestation service"
        if service.get("max_fee") is not None:
            out["message"]["max_fee"] = service["max_fee"]
        out["message"]["destination_domain"] = service.get("destination_domain") \
            or out["message"]["destination_domain"]
    out["witnesses"] = {"message_sent": {"bytes": source.get("message_bytes"),
                                         "rail_decode": source.get("rail_decode")},
                        "service_decode": service}
    out["evidence"] = [source.get("detail")]

    if attestation.get("state") in ("pending", "not_found"):
        out["state"] = "WAITING_FOR_FINALITY"
        out["detail"] = ("the attestation service has not signed this transfer yet"
                         if attestation.get("state") == "pending"
                         else "the attestation service has not indexed this transfer yet")
        return _finish(out, record, env)

    out["evidence"].append(f"attestation signed, status {attestation.get('status')}")

    # Signed: the destination question can be asked. This is the same pass the
    # rail makes before it executes, so the page and the execution agree.
    recorded = [a for a in (record.get("attempts") or []) if a.get("transaction_hash")]
    if recorded:
        delivered = _delivered(env, out, recorded)
        if delivered:
            out["state"] = "DELIVERED"
            out["detail"] = "the destination's own receipt and token event agree"
            return _finish(out, record, env)
        last = recorded[-1]
        out["state"] = "DELIVERY_SUBMITTED"
        out["detail"] = f"delivery broadcast: {last['transaction_hash']}"
        out["execution"] = {"attempt": last}
        return _finish(out, record, env)

    from .rail import Rail  # imported here: the rail pulls in the execution client
    try:
        rail = Rail(env)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"the execution layer is unavailable: {str(exc)[:120]}")
        out["state"] = "ATTESTATION_READY"
        out["detail"] = "signed; the destination has not been asked"
        return _finish(out, record, env)

    transmitter_check = rail.verify_transmitter(destination_domain)
    out["destination"]["transmitter_check"] = transmitter_check
    if not transmitter_check.get("matches"):
        out["state"] = "REFUSED"
        out["decision"] = {"action": "refuse", "reason": "WRONG_TRANSMITTER",
                           "detail": "the contract on the destination answers with another domain",
                           "evidence": f"localDomain() answered {transmitter_check.get('reported_domain')}"}
        out["evidence"].append(out["decision"]["evidence"])
        return _finish(out, record, env)

    destination_record = rail.destination_record(destination_domain, out["message"])
    out["destination"]["record"] = destination_record
    if destination_record.get("used") is True:
        out["state"] = "REFUSED"
        out["decision"] = {"action": "refuse", "reason": "ALREADY_DELIVERED",
                           "detail": "the destination contract has already received this nonce",
                           "evidence": f"usedNonces({out['message']['nonce']}) answered true"}
        out["evidence"].append(out["decision"]["evidence"])
        return _finish(out, record, env)

    preflight = rail.preflight(destination_domain, attestation)
    out["destination"]["preflight"] = preflight
    observed = {"attestation": attestation, "message": out["message"], "preflight": preflight,
                "transmitter_address": transmitter(env, destination_domain),
                "transmitter_check": transmitter_check, "destination_record": destination_record,
                "executing_wallet": rail.executing_wallet()}
    decision = classifier.decide({"source_domain": source_domain,
                                  "destination_domain": destination_domain,
                                  "burn_tx": burn_tx}, observed)
    out["decision"] = decision
    action = decision.get("action")
    if decision.get("evidence"):
        out["evidence"].append(str(decision["evidence"])[:200])

    if action == classifier.COMPLETE:
        age = _age_seconds(env, source)
        out["execution"] = {"ready": True,
                            "guards": {"transmitter": transmitter_check.get("matches"),
                                       "nonce_unused": destination_record.get("used") is False,
                                       "preflight": (preflight or {}).get("ok"),
                                       "executor": rail.executing_wallet()}}
        if age is not None and age > STRANDED_SECONDS:
            out["state"] = "STRANDED"
            out["detail"] = (f"deliverable for {int(age // 60)} minutes and nothing has moved it")
        else:
            out["state"] = "DELIVERY_READY"
            out["detail"] = "every guard passes; delivery is ready to be attempted"
    elif action == classifier.REFUSE:
        out["state"] = "REFUSED"
        out["detail"] = decision.get("detail") or "the protocol refuses this delivery"
        out["execution"] = {"ready": False, "refusal": decision.get("reason")}
    else:
        out["state"] = "ATTESTATION_READY"
        out["detail"] = decision.get("detail") or "waiting on the protocol"
        out["execution"] = {"ready": False}
    return _finish(out, record, env)


def _age_seconds(env: dict, source: dict) -> float | None:
    block = source.get("block")
    if not block:
        return None
    try:
        rpc = Rpc(rpc_url(env, int(source["source_domain"])))
        head = rpc.call("eth_getBlockByNumber", ["latest", False]) or {}
        then = rpc.call("eth_getBlockByNumber", [hex(block), False]) or {}
        return max(0.0, int(head["timestamp"], 16) - int(then["timestamp"], 16))
    except Exception:  # noqa: BLE001
        return None


def _delivered(env: dict, out: dict, attempts: list) -> bool:
    """Is there a destination transaction whose receipt and token event agree?

    A hash from an execution call is not a delivery. The receipt has to be
    successful, and the token's own Transfer event has to show the recipient
    receiving what the message said - which is the same measurement the invariant
    makes, applied to this one transfer.
    """
    from . import pairing

    destination_domain = out["destination"]["domain"]
    rpc = Rpc(rpc_url(env, destination_domain))
    for attempt in reversed(attempts):
        mint_tx = attempt.get("transaction_hash")
        receipt = rpc.transaction_receipt(mint_tx)
        attempt["receipt_status"] = ("pending" if not receipt
                                     else ("success" if _int(receipt.get("status")) == 1
                                           else "reverted"))
        if not receipt:
            continue
        if attempt["receipt_status"] != "success":
            out.setdefault("errors", []).append(
                f"the destination transaction reverted: {mint_tx}")
            continue
        recipient = out["message"].get("recipient")
        token = USDC.get(destination_domain)
        minted = None
        if token and recipient:
            minted = pairing.minted_amount(rpc, mint_tx, recipient, token)
        attempt["block"] = _int(receipt.get("blockNumber"))
        attempt["minted"] = minted
        if minted is None:
            continue
        burned = int(out["message"].get("amount") or 0)
        # The fee is a fact the message carries; reading it is what stops a correct
        # mint from being reported as short by exactly the protocol's own fee.
        fee = out["message"].get("fee_executed")
        if fee is None:
            fee = _fee_of(attempt)
        fee = int(fee) if fee is not None else None
        expected_min = max(0, burned - fee) if fee is not None else burned
        attempt["burned"] = burned
        attempt["fee"] = fee
        attempt["expected_min"] = expected_min
        attempt["paired"] = minted >= expected_min and minted <= burned
        if attempt["paired"]:
            out["delivered"] = attempt
            return True
    return False


def _fee_of(attempt: dict) -> int | None:
    value = attempt.get("fee_executed")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _finish(out: dict, record: dict, env: dict) -> dict:
    """Persist what was just learned, so the next reader starts from it."""
    stored = dict(record)
    stored["state"] = out["state"]
    stored["detail"] = out.get("detail")
    if out.get("destination"):
        stored["destination_domain"] = out["destination"].get("domain")
    if out.get("message"):
        stored["nonce"] = out["message"].get("nonce")
        stored["amount"] = out["message"].get("amount")
        stored["amount_usdc"] = out["message"].get("amount_usdc")
        stored["recipient"] = out["message"].get("recipient")
        stored["caller"] = out["message"].get("caller")
    if out.get("delivered"):
        stored["destination_tx"] = out["delivered"].get("transaction_hash")
        stored["minted"] = out["delivered"].get("minted")
    try:
        save(env, stored)
    except OSError:
        pass
    out["record"] = stored
    return out


# ---------------------------------------------------------------- execution

def execute(env: dict, record: dict, on_step=None) -> dict:
    """Attempt delivery, and only report what the chains confirm.

    Called only after a fresh state read has said DELIVERY_READY or STRANDED.
    The duplicate guard is the read: if another executor delivered it in the
    meantime, the destination's nonce record says so and this refuses.
    """
    from .rail import Rail

    state = state_of(env, record)
    if state["state"] not in ("DELIVERY_READY", "STRANDED"):
        return {"state": state["state"], "attempted": False,
                "reason": (state.get("decision") or {}).get("reason") or state.get("detail"),
                "evidence": state.get("evidence"), "decision": state.get("decision")}

    rail = Rail(env)
    request = {"source_domain": int(record["source_domain"]),
               "destination_domain": int(state["destination"]["domain"]),
               "burn_tx": record["source_tx"]}
    receipt = rail.run(request, max_wait=60, interval=10, on_wait=on_step)
    rail.write_receipt(receipt, env)

    attempt = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "transaction_hash": receipt.get("transaction_hash"),
        "transaction_link": receipt.get("transaction_link"),
        "decision": receipt.get("decision"),
        "seconds": receipt.get("seconds"),
    }
    stored = dict(record)
    stored.setdefault("attempts", []).append(attempt)
    save(env, stored)

    after = state_of(env, stored)
    return {"state": after["state"], "attempted": True, "attempt": attempt,
            "decision": receipt.get("decision"), "receipt": receipt,
            "delivered": after.get("delivered"), "evidence": after.get("evidence")}


def register(env: dict, body: dict) -> dict:
    """Take ownership of a burn the user just made, and keep it.

    The client sends a transaction hash and, at most, a claim about who signed
    it. Everything else is read from the chain: if the receipt does not carry the
    protocol's message, there is no transfer to register.
    """
    source_domain = int(body.get("source_domain"))
    burn_tx = str(body.get("burn_tx") or "").strip()
    if not burn_tx.startswith("0x") or len(burn_tx) != 66:
        raise ValueError("burn_tx must be a 32-byte transaction hash")
    if source_domain not in CHAIN_IDS:
        raise ValueError(f"domain {source_domain} is not a chain this rail watches")

    source_data = source_evidence(env, source_domain, burn_tx)
    if not source_data.get("amount"):
        raise ValueError(source_data.get("detail") or "this transaction carries no transfer")

    signed = signer_of(env, source_domain, burn_tx)
    owner = (body.get("address") or signed.get("from") or "").strip().lower()
    transfer_id = transfer_id_of(source_domain, burn_tx)
    existing = load(env, transfer_id)
    record = existing or {
        "transfer_id": transfer_id,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "attempts": [],
    }
    record.update({
        "source_domain": source_domain,
        "source_tx": burn_tx,
        "destination_domain": source_data["destination_domain"],
        "amount": source_data["amount"],
        "amount_usdc": source_data["amount_usdc"],
        "recipient": source_data["recipient"],
        "caller": source_data["caller"],
        "burn_token": source_data.get("burn_token"),
        "max_fee": source_data.get("max_fee"),
        "min_finality_threshold": source_data.get("min_finality_threshold"),
        "message": source_data.get("message"),
        "owner": owner,
        "source_signer": signed.get("from"),
        "deployment": deployment(env),
        "witnesses": {k: v for k, v in (source_data.get("witnesses") or {}).items()},
        "agreement": source_data.get("agreement"),
    })
    save(env, record)
    return {"transfer_id": transfer_id, "record": record, "source": source_data,
            "reused": bool(existing)}


def summary(record: dict) -> dict:
    """The fields a list needs, without asking the chains anything."""
    return {
        "transfer_id": record.get("transfer_id"),
        "source_domain": record.get("source_domain"),
        "destination_domain": record.get("destination_domain"),
        "source_tx": record.get("source_tx"),
        "destination_tx": record.get("destination_tx"),
        "nonce": record.get("nonce"),
        "amount": record.get("amount"),
        "amount_usdc": record.get("amount_usdc"),
        "minted": record.get("minted"),
        "recipient": record.get("recipient"),
        "caller": record.get("caller"),
        "owner": record.get("owner"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "state": record.get("state") or "CREATED",
        "detail": record.get("detail"),
        "attempts": record.get("attempts") or [],
    }
