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

## What the rail does

    observe   read the source chain's own log stream; read the destination's own record
    decide    complete, defer, or refuse — a pure function over what was observed
    act       hand the signed attestation to the destination through the execution layer
    record    write a receipt: what was seen, what was decided, what moved, and in what order

The refusal is the product. Every one carries a reason code and the evidence that produced it:

| Reason | Meaning | Read from |
|---|---|---|
| `ALREADY_DELIVERED` | the destination already holds this transfer | the destination contract's record for the nonce |
| `CALLER_RESTRICTED` | the message names one allowed caller and it is not the executor | the message's `destinationCaller` |
| `ATTESTATION_PENDING` | the source has not finalised, so nothing is signed yet | the attestation service |
| `RECIPIENT_MISMATCH` / `AMOUNT_MISMATCH` / `TOKEN_MISMATCH` | the request does not describe the transfer in the message | the message body |
| `ROUTE_MISMATCH` | the message travels between other domains than the caller named | the message header |
| `MESSAGE_INCONSISTENT` | our decode and the service's decode of the same bytes disagree | two decodes |
| `WRONG_TRANSMITTER` | the contract about to be called serves another domain | an on-chain read |
| `UNSUPPORTED_DOMAIN` | the transfer goes somewhere this rail does not watch | rail configuration |
| `PREFLIGHT_REVERT` | the destination would reject the delivery for another reason | the destination's simulation |
| `UNREADABLE_MESSAGE` | the message body could not be decoded, so nothing is attempted | our decoder |

## How it executes

The rail holds no key and signs nothing itself. Every state-changing call goes through an execution
layer: simulate first, read the verdict, broadcast with an idempotency key derived from the transfer,
then poll until the transaction hash appears. The destination's simulation is not decoration — it is
where the already-delivered refusal comes from, before anything is spent.

The payer side stays in the wallet of whoever is paying. Creating a transfer is their transaction:
they approve and they burn from their own address, and the message names them as the recipient.

## Two deployments, one rail

Two versions of the protocol are live on these testnets at once. The later one accepts a fee cap and a
finality threshold, so a transfer can be attested as soon as the source block is confirmed; the earlier
one waits for full finality. Both are in the configuration, both have been delivered by this rail, and
the message layout of the later one is decoded field by field. What is decoded and what is not is
stated in [docs/LIMITATIONS.md](docs/LIMITATIONS.md).

## Layout

    astra/config.py          chains, domains, contract addresses, environment
    astra/protocol.py        the wire format: header, body, corroboration against a second decode
    astra/attestation.py     the attestation service: pending, final, not found
    astra/keeperhub.py       the execution layer: simulate, broadcast, poll, idempotency
    astra/classifier.py      when money may move, as a pure function
    astra/rail.py            one pass over one transfer: observe, decide, act, record
    astra/inflight.py        discovery: the source log stream, the destination's verdict
    astra/rpc.py             a read-only JSON-RPC client
    scripts/                 the payer side, discovery, completion, and the invariant
    service/                 the HTTP surface and the browser control surface
    tests/                   32 tests, including the captured messages this project produced

## Run it

```bash
cp .env.example .env          # fill in the executor key and the payer wallet
uv venv .venv && uv pip install --python .venv/bin/python pytest
.venv/bin/python -m pytest tests -q

.venv/bin/python scripts/astra_inflight.py --blocks 4000
.venv/bin/python scripts/astra_open_transfer.py --amount 1      # your wallet signs this
.venv/bin/python scripts/astra_complete.py --burn-tx 0x… --source 6 --destination 0 --wait 300
.venv/bin/python scripts/prove_pairing.py --blocks 4000

.venv/bin/python service/astra_service.py --port 8099           # then open http://127.0.0.1:8099
```

## Evidence in the repository

- `artifacts/receipts/` — one receipt per decision, including the refusals, each carrying the evidence
- `artifacts/pairing.json` — the invariant's answer for a window of transfers
- `artifacts/spike-burn.json`, `artifacts/spike-burn-v2.json` — the raw attestations and executions
- `tests/fixtures/captured.json` — messages captured from the transfers above, pinned by the tests

## Limitations and roadmap

Read [docs/LIMITATIONS.md](docs/LIMITATIONS.md) before trusting this with anything. The short version:
it is a testnet rail, it watches two domains, and it decodes one of the two live message layouts. What
it does not do, it refuses by name rather than approximating.

Roadmap: decode the older layout as well, watch more domains, and turn the pairing proof into a
scheduled check whose failure is a receipt of its own.

---

MIT. See [LICENSE](LICENSE).
