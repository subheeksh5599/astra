"""The paying side: the one place this project signs anything.

The rail holds no key. Creating a transfer is the payer's own transaction, and it
belongs to whoever is paying. This module is that side, kept separate from the
rail on purpose: it signs, and the rail only ever reads and hands over an
attestation through the execution layer.

Two callers use it: the script a person runs, and the control surface, so that a
transfer can be created from the page by the key this machine is configured with
instead of requiring a browser wallet. Everything it needs comes from the
environment; nothing here prints a key or writes one down.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time

from .config import DOMAIN_NAMES, USDC, messenger, rpc_url, supports
from .rpc import Rpc

FAST_THRESHOLD = 1000
STANDARD_THRESHOLD = 2000
#: How much of the amount the payer is willing to pay the protocol in fees, in
#: parts per thousand. The message carries the cap; the protocol charges what it
#: charges and the difference is what the receipt measures.
FEE_CAP_PER_THOUSAND = 1000

KEY_NAMES = ("PAYER_KEY", "BUYER_KEY", "PRIVATE_KEY")


class PayerUnavailable(RuntimeError):
    """No key this machine may sign with, or no tool able to sign."""


def cast_binary() -> str:
    for candidate in (os.path.expanduser("~/.foundry/bin/cast"), shutil.which("cast")):
        if candidate and os.path.exists(candidate):
            return candidate
    raise PayerUnavailable("cast is not installed: the paying side signs with it and nothing else")


def key_name(env: dict) -> str | None:
    for name in KEY_NAMES:
        if env.get(name):
            return name
    return None


def private_key(env: dict) -> str:
    name = key_name(env)
    if not name:
        raise PayerUnavailable(f"no paying key in the environment (one of {', '.join(KEY_NAMES)})")
    return env[name]


def address(env: dict) -> str:
    """The address the configured key signs for, read from the key itself.

    Not from a separate variable: an address written down beside a key is a claim
    about the key, and the key is what actually signs.
    """
    cached = env.get("_payer_address")
    if cached:
        return cached
    out = subprocess.run([cast_binary(), "wallet", "address", "--private-key", private_key(env)],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise PayerUnavailable(f"could not read an address from the configured key: {out.stderr[:200]}")
    return out.stdout.strip()


def _run(args: list, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, env={**os.environ, **env})


def read(env: dict, url: str, to: str, signature: str, *params) -> str:
    out = _run([cast_binary(), "call", to, signature, *params, "--rpc-url", url], env)
    if out.returncode != 0:
        raise RuntimeError(f"call {signature} failed: {out.stderr.strip()[:200]}")
    return out.stdout.strip()


def send(env: dict, url: str, to: str, signature: str, *params) -> dict:
    """Sign and broadcast one call, with an explicit nonce.

    Public testnet endpoints answer a nonce query with a value that is already
    stale, which turns a second write in the same minute into a replacement-priced
    failure. Reading the pending nonce and passing it explicitly avoids that, and
    the retry exists because a public endpoint can lose a request without failing.
    """
    last = ""
    for attempt in range(3):
        nonce = _run([cast_binary(), "nonce", address(env), "--block", "pending",
                      "--rpc-url", url], env).stdout.split()
        if not nonce:
            last = "the endpoint did not answer a nonce query"
            time.sleep(2 + attempt)
            continue
        out = _run([cast_binary(), "send", "--rpc-url", url, "--private-key", private_key(env),
                    "--nonce", nonce[0], "--json", to, signature, *params], env)
        if out.returncode == 0:
            payload = json.loads(out.stdout)
            return {"transaction_hash": payload.get("transactionHash"), "status": payload.get("status"),
                    "nonce": nonce[0]}
        last = out.stderr.strip()[:300]
        time.sleep(2 + attempt)
    raise RuntimeError(f"send {signature} failed after 3 attempts: {last}")


def balances(env: dict, domains: list | None = None) -> list:
    """What the paying key holds on each chain this rail watches.

    A transfer can only be created where the payer holds both the token and the gas
    to move it, so the page asks this rather than letting someone fill a form and
    discover it at signing time.
    """
    domains = domains if domains is not None else [d for d in sorted(USDC) if supports(env, d)]
    who = address(env)
    rows = []
    for domain in domains:
        if not supports(env, domain):
            continue
        url = rpc_url(env, domain)
        row = {"domain": domain, "name": DOMAIN_NAMES.get(domain, str(domain)), "address": who}
        try:
            rpc = Rpc(url)
            row["usdc"] = rpc.balance_of(USDC[domain], who)
            row["native"] = int(rpc.call("eth_getBalance", [who, "latest"]), 16)
        except Exception as exc:  # noqa: BLE001 - an unreadable chain is reported, not fatal
            row["detail"] = f"could not read this chain: {str(exc)[:120]}"
        rows.append(row)
    return rows


def encode_recipient(address_hex: str) -> str:
    return "0x" + "0" * 24 + address_hex[2:].lower()


def encode_caller(caller: str | None) -> str:
    if not caller:
        return "0x" + "0" * 64
    text = caller.strip().lower()
    if not text.startswith("0x") or len(text) != 42:
        raise ValueError(f"{caller} is not an address")
    return "0x" + "0" * 24 + text[2:]


def open_transfer(env: dict, amount_usdc: float, source: int, destination: int,
                  caller: str | None = None, deployment: str = "v2",
                  threshold: int | None = None, on_step=None) -> dict:
    """Approve if needed, burn, and stop: the transfer is in flight by design.

    The burn is the payer's transaction. It leaves the rail a transfer it did not
    create and does not own, which is exactly the situation a rail has to be
    correct in.
    """
    def step(message: str) -> None:
        if on_step:
            on_step(message)

    if source not in USDC or not supports(env, source):
        raise ValueError(f"this rail cannot pay on domain {source}")
    if destination not in DOMAIN_NAMES or not supports(env, destination):
        raise ValueError(f"this rail cannot deliver to domain {destination}")
    if deployment == "v1" and caller:
        raise ValueError("this deployment's burn takes no destination caller")

    amount = int(round(float(amount_usdc) * 1_000_000))
    if amount <= 0:
        raise ValueError("amount must be greater than zero")

    url = rpc_url(env, source)
    token = USDC[source]
    target = messenger(env, source)
    payer = address(env)
    threshold = threshold or (FAST_THRESHOLD if deployment != "v1" else STANDARD_THRESHOLD)

    step(f"checking the allowance on chain {source}")
    allowance = int(read(env, url, token, "allowance(address,address)(uint256)", payer, target).split()[0])
    approve_tx = None
    if allowance < amount:
        step("approving the messenger to move the tokens")
        approve = send(env, url, token, "approve(address,uint256)", target, str(amount))
        approve_tx = approve["transaction_hash"]
        step(f"approval sent: {approve_tx}")

    recipient = encode_recipient(payer)
    step(f"burning {amount_usdc} USDC on {DOMAIN_NAMES.get(source, source)}")
    if deployment == "v1":
        burn = send(env, url, target, "depositForBurn(uint256,uint32,bytes32,address)",
                    str(amount), str(destination), recipient, token)
    else:
        burn = send(env, url, target,
                    "depositForBurn(uint256,uint32,bytes32,address,bytes32,uint256,uint32)",
                    str(amount), str(destination), recipient, token,
                    encode_caller(caller), str(max(1, amount // FEE_CAP_PER_THOUSAND)), str(threshold))
    step(f"burned: {burn['transaction_hash']}")

    return {
        "burn_tx": burn["transaction_hash"],
        "burn_status": burn["status"],
        "approve_tx": approve_tx,
        "source_domain": source,
        "destination_domain": destination,
        "amount": amount,
        "amount_usdc": amount / 1e6,
        "payer": payer,
        "recipient": payer,
        "caller": caller,
        "deployment": deployment,
        "threshold": threshold,
    }
