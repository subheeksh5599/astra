<div align="center">

# ASTRA

### One burn, exactly one mint.

A cross-chain transfer leaves one chain and arrives on another, unless it does not. ASTRA watches both
ends, finishes what is in flight through an execution layer that holds the keys, and refuses, by name,
what may not move.

[![Network](https://img.shields.io/badge/network-base%20sepolia%20%2B%20ethereum%20sepolia-0052FF?labelColor=0e1013)](https://sepolia.basescan.org)
[![Tests](https://img.shields.io/badge/tests-32%20passing-2ecc71?labelColor=0e1013)](tests/)
[![Invariant](https://img.shields.io/badge/invariant-executable-3f9d8f?labelColor=0e1013)](scripts/prove_pairing.py)
[![License](https://img.shields.io/badge/license-MIT-yellow?labelColor=0e1013)](LICENSE)

</div>

---

## Live status

Everything below was produced by this repository against public testnets. Links are to the chains
themselves, not to this project.

| What | Where | Transaction |
|---|---|---|
| A transfer opened, left in flight | Base Sepolia (84532) | [`0x6c7fa66b…65d108`](https://sepolia.basescan.org/tx/0x6c7fa66b54ba1608f144d72b403ce4a3d1d07bb4da82ed27c14f3dbfca65d108) |
| **That transfer finished, executed by the execution layer** | Ethereum Sepolia (11155111) | [`0x596e00b4…aa64a45`](https://sepolia.etherscan.io/tx/0x596e00b4b14c99a95572bc53a2e8b48ac10aff48f4e4c5e62f6dda855aa64a45) |
| A second transfer opened against the older deployment | Base Sepolia | [`0x1e15e0f1…93bcd3`](https://sepolia.basescan.org/tx/0x1e15e0f18046f3db2f8f4cd9bfd43f72b3f569b6948748c54697898a5293bcd3) |
| **It finished too, through the same rail** | Ethereum Sepolia | [`0xa1aecdf9…8e209d`](https://sepolia.etherscan.io/tx/0xa1aecdf992c02913491df02c36b6d3291a20c6d68b03feb0f545e7c4978e209d) |
| A transfer whose message names another caller, left undelivered on purpose | Base Sepolia | [`0xd517d29c…38d793`](https://sepolia.basescan.org/tx/0xd517d29c6466b55d63abf2b717f647ca847cb19ae3959c2cfaa0d3537e38d793) |
| Testnet gas sent to the wallet that executes | Ethereum Sepolia | [`0xc2a660c4…8a44db`](https://sepolia.etherscan.io/tx/0xc2a660c4290550050d784bcfa195988d41fa64361cf03912148ccbdb408a44db) |

Refusals proved against the live destination, each with the destination's own words as evidence:

| Refusal | How it was produced |
|---|---|
| `ALREADY_DELIVERED` | the same completed transfer asked twice; the destination contract reports the nonce as used |
| `CALLER_RESTRICTED` | a transfer whose message names a single allowed caller; the rail names that address and does not attempt |
| `NOT_FOUND` | a source transaction with no transfer behind it |
| `RECIPIENT_MISMATCH` | a request naming a recipient the message does not carry |
| `ATTESTATION_PENDING` | a transfer read before the source chain finalised; the rail defers and waits |

Receipts for these are in [`artifacts/receipts/`](artifacts/receipts). The pairing proof is in
[`artifacts/pairing.json`](artifacts/pairing.json).

---

## The gap in one paragraph

Moving value between two chains is two transactions, not one. The first burns on the source chain and
announces a message. The second hands a signed attestation to the destination contract, which mints.
Between them the value exists nowhere spendable: it has left the source and it has not arrived. That
window closes only if somebody is watching both ends. A rail that watches one end cannot tell a
transfer that has arrived from one that is stuck, and a rail that keeps its own list cannot tell that
somebody else already delivered the transfer it is about to send.

## The invariant

    One burn, exactly one mint.

For every message the source chain announced, the destination chain received it once, or it has not
received it yet, and there is no third possibility. `scripts/prove_pairing.py` reads both chains and
says which happened, per transfer:

    transfer                         amount  destination        attestation verdict
    6:0xde9c71de3325c9e83089ea    19.490000  26                 final      unwatched
    6:0x78d3613c7d387fd03d50d2     1.000000  ethereum-sepolia   final      stranded
    6:0x1fe5312960d98a21c655f1     1.000000  ethereum-sepolia   final      paired
    ...
    1 paired, 1 stranded, 6 unwatched

It exits non-zero when a burn is stranded with a signed attestation (value sitting still that could
move) and when a nonce has been received twice. The destination's answer to "have you received this
nonce" is what decides; nothing this rail wrote down is consulted.

