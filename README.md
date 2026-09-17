<div align="center">

# ASTRA

### One burn, exactly one mint.

A cross-chain transfer leaves one chain and arrives on another, unless it does not. ASTRA watches both
ends, finishes what is in flight through an execution layer that holds the keys, and refuses, by name,
what may not move.

[![Network](https://img.shields.io/badge/watching-5%20testnets%20%C2%B7%202%20live%20deployments-0052FF?labelColor=0e1013)](astra/config.py)
[![Tests](https://img.shields.io/badge/tests-50%20passing-2ecc71?labelColor=0e1013)](tests/)
[![Invariant](https://img.shields.io/badge/invariant-executable%20%2B%20quantitative-3f9d8f?labelColor=0e1013)](scripts/prove_pairing.py)
[![License](https://img.shields.io/badge/license-MIT-yellow?labelColor=0e1013)](LICENSE)

</div>

---

## Live status

Everything below was produced by this repository against public testnets. Links are to the chains
themselves, not to this project.

| What | Where | Transaction |
|---|---|---|
| A transfer opened and left in flight | Base Sepolia (84532) | [`0x6c7fa66b…65d108`](https://sepolia.basescan.org/tx/0x6c7fa66b54ba1608f144d72b403ce4a3d1d07bb4da82ed27c14f3dbfca65d108) |
| **That transfer finished, executed by the execution layer** | Ethereum Sepolia (11155111) | [`0x596e00b4…aa64a45`](https://sepolia.etherscan.io/tx/0x596e00b4b14c99a95572bc53a2e8b48ac10aff48f4e4c5e62f6dda855aa64a45) |
| A transfer opened against the older deployment | Base Sepolia | [`0x1e15e0f1…93bcd3`](https://sepolia.basescan.org/tx/0x1e15e0f18046f3db2f8f4cd9bfd43f72b3f569b6948748c54697898a5293bcd3) |
| **It finished too, through the same rail** | Ethereum Sepolia | [`0xa1aecdf9…8e209d`](https://sepolia.etherscan.io/tx/0xa1aecdf992c02913491df02c36b6d3291a20c6d68b03feb0f545e7c4978e209d) |
| 0.05 USDC opened on the fast threshold, left in flight | Base Sepolia | [`0x9a616524…9ce5da`](https://sepolia.basescan.org/tx/0x9a616524a0a707c3a0cb7347e0ca7bb186bf72acc7db64cd040cf25a829ce5da) |
| **Finished by the rail, and the mint measured** | Ethereum Sepolia | [`0x09144b02…5baf752`](https://sepolia.etherscan.io/tx/0x09144b02b95fcb419a81feed42432fe9a9fcfd9d2e4152ea3c346e4095baf752) |
| 0.05 USDC opened once more, after the rail was widened to five chains | Base Sepolia | [`0x27e417c4…fc6168`](https://sepolia.basescan.org/tx/0x27e417c44a724686ee48f0b9a5817ad844536f29e8e09adc9ab9c0402afc6168) |
| **Finished by the rail on the same path: 0.049994 minted of 0.050000 burned** | Ethereum Sepolia | [`0xcb430dae…fb28b9`](https://sepolia.etherscan.io/tx/0xcb430daeb90c12aedd639c4463cf5d951abfcd5d49680bf32d84a93e6afb28b9) |
| A transfer whose message names another caller, left undelivered on purpose | Base Sepolia | [`0xd517d29c…38d793`](https://sepolia.basescan.org/tx/0xd517d29c6466b55d63abf2b717f647ca847cb19ae3959c2cfaa0d3537e38d793) |
| Testnet gas sent to the wallet that executes | Ethereum Sepolia | [`0xc2a660c4…8a44db`](https://sepolia.etherscan.io/tx/0xc2a660c4290550050d784bcfa195988d41fa64361cf03912148ccbdb408a44db) |

Every delivery settles to the protocol's own arithmetic: 0.050000 USDC burned, a 0.000006 fee for the
fast threshold, 0.049994 minted — and the rail reads the minted figure out of the token's own transfer
event rather than assuming it, so `prove_pairing.py` reports the pair as `minted 0.049994 of 0.049994`
for each one.

Refusals proved against the live destination, each with the destination's own words as evidence:

| Refusal | How it was produced |
|---|---|
| `ALREADY_DELIVERED` | the same completed transfer asked twice; the destination contract reports the nonce as used |
| `CALLER_RESTRICTED` | a transfer whose message names a single allowed caller; the rail names that address and does not attempt |
| `NOT_FOUND` | a source transaction with no transfer behind it |
| `RECIPIENT_MISMATCH` | a request naming a recipient the message does not carry |
| `ATTESTATION_PENDING` | a transfer read before the source chain finalised; the rail defers and waits |
| `UNSUPPORTED_DOMAIN` | a transfer travelling to a chain this rail does not serve, named rather than attempted |

Receipts for these are in [`artifacts/receipts/`](artifacts/receipts). The pairing proof is in
[`artifacts/pairing.json`](artifacts/pairing.json), and every scheduled pass is journaled under
[`artifacts/watch/`](artifacts/watch).

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
says which happened, per transfer, including how much value arrived:

    transfer                     amount  destination        attestation verdict
    6:931762504361313534443157 0.050000  ethereum-sepolia   final      paired  mint 0x09144b02b95f  minted 0.049994 of 0.049994
    6:546510176608399233227111 1.000000  ethereum-sepolia   final      stranded
    2:179291946512626312961173 1.000000  26                 final      unwatched
    ...
    1 paired, 1 stranded, 10 unwatched

Delivery is read from the destination's own receipt event, keyed by the source domain and the nonce
that event carries — the same statement on both live deployments, which is why the older one can be
paired here even though its storage does not answer the newer contract's replay question.

The invariant can break in three ways, and all three are checked:

    stranded        the attestation is signed and nothing has received it: value that could move
    received twice  the destination named the same nonce twice: value that moved twice
    short           the mint is less than the burn, by more than the protocol's own fee

It exits non-zero on any of them, so wiring it to a job makes "money is stuck" a failed job rather
than a paragraph. `scripts/astra_watch.py` is that job: it runs the same pass on an interval and
writes one journal entry per pass, and it does not spend anything. Finishing a transfer stays an
explicit act on purpose — a process that moves money unattended is the thing this rail exists to
replace with something you can ask about.

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
| `DEPLOYMENT_ABSENT` | the rail watches the chain, but this deployment is not on it | rail configuration |
| `PREFLIGHT_REVERT` | the destination would reject the delivery for another reason | the destination's simulation |
| `UNREADABLE_MESSAGE` | the message body could not be decoded, so nothing is attempted | our decoder |

## How it executes

The rail holds no key and signs nothing itself. Every state-changing call goes through an execution
layer: simulate first, read the verdict, broadcast with an idempotency key derived from the transfer,
then poll until the transaction hash appears. The destination's simulation is not decoration — it is
where the already-delivered refusal comes from, before anything is spent.

The payer side stays in the wallet of whoever is paying. Creating a transfer is their transaction:
they approve and they burn from their own address, and the message names them as the recipient.

## Five chains, two deployments, one rail

    domain 0  ethereum-sepolia   11155111
    domain 2  optimism-sepolia   11155420
    domain 3  arbitrum-sepolia   421614
    domain 6  base-sepolia       84532
    domain 7  polygon-amoy       80002

Every one of those is a chain the execution layer can deliver on and the protocol is deployed on; the
addresses are in `astra/config.py`, and each destination contract is asked for the domain it serves
before it is called. A chain that this particular deployment is not on is refused with
`DEPLOYMENT_ABSENT` instead of being attempted and failing.

Two versions of the protocol are live at once. The later one accepts a fee cap and a finality
threshold, so a transfer can be attested as soon as the source block is confirmed; the earlier one
waits for full finality. **Both message layouts are now decoded field by field** — the offsets were
derived from real messages and then cross-checked against the messenger's own deposit event on the
source chain, and the test suite pins each layout against two real transfers.

## Layout

    astra/config.py          chains, domains, contract addresses, environment
    astra/protocol.py        the wire format: both live layouts, corroborated against a second decode
    astra/attestation.py     the attestation service: pending, final, not found
    astra/keeperhub.py       the execution layer: simulate, broadcast, poll, idempotency
    astra/classifier.py      when money may move, as a pure function
    astra/rail.py            one pass over one transfer: observe, decide, act, record
    astra/inflight.py        discovery: the source log stream, the destination's verdict
    astra/pairing.py         the invariant: a verdict per transfer, and the three ways it breaks
    astra/rpc.py             a read-only JSON-RPC client
    scripts/                 the payer side, discovery, completion, the invariant, the watcher
    service/                 the HTTP surface and the browser control surface
    tests/                   50 tests, including the captured messages this project produced

## Run it

```bash
cp .env.example .env          # fill in the executor key and the payer wallet
uv venv .venv && uv pip install --python .venv/bin/python pytest
.venv/bin/python -m pytest tests -q

.venv/bin/python scripts/astra_inflight.py --blocks 4000
.venv/bin/python scripts/astra_open_transfer.py --amount 1              # your wallet signs this
.venv/bin/python scripts/astra_complete.py --burn-tx 0x… --source 6 --destination 0 --wait 300
.venv/bin/python scripts/prove_pairing.py --blocks 4000                 # exits non-zero if broken
.venv/bin/python scripts/astra_watch.py --once --blocks 2000            # one pass, one journal entry

.venv/bin/python service/astra_service.py --port 8099                   # open http://127.0.0.1:8099
```

## Evidence in the repository

- `artifacts/receipts/` — one receipt per decision, including the refusals, each carrying the evidence
- `artifacts/pairing.json` — the invariant's answer for a window of transfers, with amounts measured
- `artifacts/watch/` — the journal of scheduled passes
- `artifacts/spike-burn.json`, `artifacts/spike-burn-v2.json` — the raw attestations and executions
- `tests/fixtures/captured.json` — messages captured from the transfers above, pinned by the tests

## Limitations and roadmap

Read [docs/LIMITATIONS.md](docs/LIMITATIONS.md) before trusting this with anything. The short version:
it is a testnet rail, it watches five chains, it decodes both live message layouts, and it holds no
key. What it does not do, it refuses by name rather than approximating.

Roadmap: mainnet configuration, domains beyond the ones the execution layer can currently reach, and a
hosted read-only surface so the pairing proof can be watched without running anything.

---

MIT. See [LICENSE](LICENSE).
