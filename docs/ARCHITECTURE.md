# Architecture

## One pass over one transfer

    request        {source_domain, destination_domain, burn_tx, expect?}
      |
      v
    observe        attestation service -> pending | final | not_found
                   final -> parse the message bytes, corroborate against the service's decode
      |
      v
    verify         read the destination contract's chain domain, compare with the message
      |
      v
    decide         classifier.decide(request, observed) -> complete | defer | refuse(reason)
      |
      +-- defer ----> receipt: nothing happened, and that is the correct outcome
      +-- refuse ---> receipt: no money moved, and here is why
      +-- complete -> destination record read -> simulate -> broadcast -> poll for the hash
      |
      v
    record         artifacts/receipts/<utc>-<transfer>-<action>.json

## Why each step exists

**Observe reads the chains, not a book.** A local list of "pending" transfers can disagree with both
chains and cannot know that somebody else delivered a transfer. The source chain's log stream says
which messages exist; the destination's record says which ones arrived.

**Verify before calling.** A message names its destination domain. The contract that will be called
answers with the domain it serves. If they disagree the call is aimed at the wrong contract, so the
rail stops before spending anything on it.

**Decide in one pure function.** `astra/classifier.py` has no network client and no clock. Given the
observed facts it returns `complete`, `defer`, or `refuse` with a reason code, which is what makes
every refusal reproducible in a test and explainable in a demo. The order is deliberate: an
already-delivered transfer is refused before anything is simulated, and an unattested one is deferred
rather than refused, because it is early, not wrong.

**Act through an execution layer.** The rail signs nothing. The call is simulated first, the verdict
is read, and only then is it broadcast with an idempotency key derived from the transfer's own
identity (source domain and nonce). The status is polled until the transaction hash appears, because
a single status read can lag the write.

**Record both outcomes.** A refusal is written to the same folder as a delivery, with the evidence
that produced it. The pair of them is the story: the delivery is why the refusal is correct.

## Components

| Module | Responsibility |
|---|---|
| `astra/config.py` | domains, chains, contract addresses, environment. One endpoint per domain, deliberately |
| `astra/protocol.py` | the wire format: header, body, transfer identity, corroboration |
| `astra/attestation.py` | the attestation service: pending, final, not found, deferred polling |
| `astra/keeperhub.py` | the execution layer: simulate, read, broadcast, poll, idempotency |
| `astra/classifier.py` | when money may move: pure, ordered, reason-coded |
| `astra/rail.py` | one pass: observe, verify, decide, act, record |
| `astra/inflight.py` | discovery from the source log stream; the destination's verdict per transfer |
| `astra/pairing.py` | the invariant: a verdict per transfer from the destinations' own events, and the three ways it can break |
| `astra/rpc.py` | read-only JSON-RPC |

## The invariant, over a window of transfers

`astra/pairing.py` answers a different question from the rail: not "what should happen to this
transfer" but "what happened to all of them". It reads every message each source chain announced in a
window, then reads each destination's own receipt events, and pairs them by the key both live
deployments publish: source domain and nonce. Three things make a pass exit non-zero — a signed
attestation with nothing received (stranded), the same nonce received more than once, and a mint that
is short of the burn by more than the fee the message itself carries (measured from the token's own
transfer event in the minting transaction).

`scripts/astra_watch.py` runs that pass on an interval and journals each one, so a transfer able to
move and not moving becomes a record rather than an observation someone happened to make. It does not
spend: finishing stays an explicit act.

## The receipt

    {
      "transfer_id": "6:0x1fe531…",              the protocol's own identity: source domain + nonce
      "request":     {source_domain, destination_domain, burn_tx, expect?},
      "observed":    {attestation_state, attestation_status, message, transmitter_check,
                      destination_record, preflight, executing_wallet},
      "decision":    {action, reason, detail, evidence?},
      "execution":   {http, execution_id, status: {transactionHash, transactionLink, …}},
      "transaction_hash": "0x…" | null,          null when nothing moved
      "seconds":     3.4
    }

`decision.evidence` carries the words of whoever decided: the destination's revert, or the
destination's record of the nonce.

## The HTTP surface

| Route | Purpose |
|---|---|
| `GET /api/health` | liveness and the configured deployment |
| `GET /api/config` | chains, domains, token and contract addresses for the browser — nothing is configured in the client |
| `GET /api/inflight?blocks&limit` | the transfers in flight across every watched domain, newest first |
| `POST /api/complete` | run one pass over one transfer and return the receipt |
| `GET /api/inspect?burn_tx&source_domain&destination_domain` | the same pass with `dry_run`: the decision and the evidence, and nothing broadcast |
| `GET /api/payer` | who this machine signs for and what that address holds on each watched chain — asked before a transfer is offered, so a person is told where their value is |
| `POST /api/open` | create a transfer from the key this machine holds, with `finish: true` to wait for the attestation and hand it over in the same call. Returns the steps as they happened, both transactions, and both links |
| `GET /api/receipts`, `GET /api/receipt/<name>` | the receipts as written |

The browser pages are static files served by the same process: a landing page and a control surface
where a person connects a wallet, opens a transfer with their own signature, watches it in flight, and
finishes it through the rail.

## Two signers, kept apart

Creating a transfer and finishing one are different transactions with different
signers, and the receipt never blurs them:

    payer side   approve + burn on the source chain, signed by the payer's key
                 (astra/payer.py; used by the script and by the control surface)
    rail side    receiveMessage on the destination, broadcast by the execution
                 layer, signed by nobody this project holds

The rail holds no key. The payer side holds exactly one, and only to make transfers
that the rail then has to be correct about: a transfer the rail did not create, does
not own and cannot remember.

## Testing

The tests split the same way the code does. `tests/test_protocol.py` pins both decoders against
messages this project actually produced on testnet (they are fixtures, and they are real). The
decision function and the execution client's polling are exercised with a scripted transport, because
the states that matter — a hash that arrives late, a terminal failure, a revert — are states a live
chain will not produce on demand. `tests/test_pairing.py` holds the invariant's arithmetic, including
the two spellings of one nonce that must land on one identity.

Two consistency tests exist because a divergence here would be silent: the watched domains must be
exactly the chains the configured deployment is present on, and every watched domain must have a chain
id, a token and an endpoint.
