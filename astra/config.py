"""Configuration: one place where Astra learns about the world.

Everything that identifies a deployment (keys, wallets, RPC endpoints) comes from
the environment. Only the protocol's own public constants carry defaults here:
its domain ids and the addresses its contracts are deployed at on the two
testnets this rail watches. All of them can be overridden from the environment.

Two deployments of the protocol are live on these testnets. The earlier one
waits for full finality before it signs an attestation; the later one accepts a
fee cap and a finality threshold, so a transfer can be attested as soon as the
source block is confirmed. The rail is written against the message, not against
either deployment, and it checks on chain that the contract it is about to call
is the one the message belongs to.
"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The protocol's domain ids. Part of the protocol, not of our deployment; the
# rail also reads the live value back from the contract before it calls it.
DOMAIN_NAMES = {
    0: "ethereum-sepolia",
    2: "optimism-sepolia",
    3: "arbitrum-sepolia",
    6: "base-sepolia",
    7: "polygon-amoy",
}
DOMAINS = {v: k for k, v in DOMAIN_NAMES.items()}

CHAIN_IDS = {
    0: 11155111,
    2: 11155420,
    3: 421614,
    6: 84532,
    7: 80002,
}

TRANSMITTERS = {
    "v1": {
        0: "0x7865fAfC2db2093669d92c0F33AeEF291086BEFD",
        6: "0x7865fAfC2db2093669d92c0F33AeEF291086BEFD",
    },
    "v2": {
        0: "0xE737e5cEBEEBa77EFE34D4aa090756590b1CE275",
        2: "0xE737e5cEBEEBa77EFE34D4aa090756590b1CE275",
        3: "0xE737e5cEBEEBa77EFE34D4aa090756590b1CE275",
        6: "0xE737e5cEBEEBa77EFE34D4aa090756590b1CE275",
        7: "0xE737e5cEBEEBa77EFE34D4aa090756590b1CE275",
    },
}
MESSENGERS = {
    "v1": {0: "0x9f3B8679c73C2Fef8b59B4f3444d4e156fb70AA5",
           6: "0x9f3B8679c73C2Fef8b59B4f3444d4e156fb70AA5"},
    "v2": {0: "0x8FE6B999Dc680CcFDD5Bf7EB0974218be2542DAA",
           2: "0x8FE6B999Dc680CcFDD5Bf7EB0974218be2542DAA",
           3: "0x8FE6B999Dc680CcFDD5Bf7EB0974218be2542DAA",
           6: "0x8FE6B999Dc680CcFDD5Bf7EB0974218be2542DAA",
           7: "0x8FE6B999Dc680CcFDD5Bf7EB0974218be2542DAA"},
}
USDC = {
    0: "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238",   # Ethereum Sepolia
    2: "0x5fd84259d66Cd46123540766Be93DFE6D43130D7",   # Optimism Sepolia
    3: "0x75faf114eafb1BDbe2F0316DF893fd58CE46AA4d",   # Arbitrum Sepolia
    6: "0x036CbD53842c5426634e7929541eC2318f3dCF7e",   # Base Sepolia
    7: "0x41E94Eb019C0762f9Bfcf9Fb1E58725BfB0e7582",   # Polygon Amoy
}

DEFAULT_DEPLOYMENT = "v2"

DEFAULT_RPC = {
    0: "https://ethereum-sepolia-rpc.publicnode.com",
    2: "https://optimism-sepolia-rpc.publicnode.com",
    3: "https://arbitrum-sepolia-rpc.publicnode.com",
    6: "https://sepolia.base.org",
    7: "https://polygon-amoy-bor-rpc.publicnode.com",
}

LOCAL_DOMAIN_SELECTOR = "0x8d3638f4"
LOCAL_DOMAIN_ABI = ("[{\"inputs\":[],\"name\":\"localDomain\",\"outputs\":"
                    "[{\"name\":\"\",\"type\":\"uint32\"}],\"stateMutability\":\"view\","
                    "\"type\":\"function\"}]")


def load_env() -> dict:
    """Read .env, then let the process environment win over it."""
    env: dict = {}
    path = os.environ.get("ASTRA_ENV") or os.path.join(ROOT, ".env")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip().strip('"').strip("'")
    for key, value in os.environ.items():
        if key.isupper():
            env[key] = value
    return env


def deployment(env: dict) -> str:
    name = env.get("ASTRA_DEPLOYMENT", DEFAULT_DEPLOYMENT)
    if name not in TRANSMITTERS:
        raise SystemExit(f"ASTRA_DEPLOYMENT={name} is not one of {sorted(TRANSMITTERS)}")
    return name


def transmitter(env: dict, domain: int):
    """The destination contract for one domain, or None where this deployment
    is not present. Only the later deployment reaches the added testnets, so a
    domain without it is answered with None rather than a raise: the rail then
    refuses the transfer by name instead of dying on a live log it cannot serve."""
    override = env.get(f"TRANSMITTER_DOMAIN_{domain}")
    if override:
        return override
    return TRANSMITTERS[deployment(env)].get(domain)


def supports(env: dict, domain: int) -> bool:
    """Is this deployment actually present on this chain?"""
    return transmitter(env, domain) is not None


def messenger(env: dict, domain: int):
    override = env.get(f"MESSENGER_DOMAIN_{domain}")
    if override:
        return override
    return MESSENGERS[deployment(env)].get(domain)


def rpc_url(env: dict, domain: int) -> str:
    """The endpoint for one domain, and only that domain.

    A single RPC_URL is a trap here: it silently makes every domain read the
    same chain, so a rail watching two chains reports one chain's transfers
    under both domains. Each domain names its own endpoint or takes the default.
    """
    return env.get(f"RPC_DOMAIN_{domain}") or DEFAULT_RPC[domain]


def keeperhub_key(env: dict) -> str:
    key = env.get("KH_API_KEY", "")
    if not key:
        raise SystemExit("KH_API_KEY is not set (environment or ./.env)")
    return key


def attestation_base(env: dict) -> str:
    return env.get("ATTESTATION_BASE", "https://iris-api-sandbox.circle.com")


def attestation_version(env: dict) -> str:
    return env.get("ATTESTATION_VERSION", deployment(env))
