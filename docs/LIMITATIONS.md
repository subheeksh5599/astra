# Limitations

This is a testnet rail. Every claim the README makes is reproducible from this repository, and this
file lists what it does not do. Where the rail cannot decide, it refuses by name rather than
approximating; the codes below are the honest edges of that.

## Scope

- **Testnets only.** Base Sepolia (domain 6) and Ethereum Sepolia (domain 0). Nothing here has been
  run against mainnet, and no part of it is audited.
- **Two domains watched.** A transfer to any other domain is refused with `UNSUPPORTED_DOMAIN`. That
  is a refusal, not a failure: an unwatched chain has no contract this rail may call.
- **Bounded discovery.** Transfers are found by reading a block range of the source chain's log
  stream through a public RPC. That range is a parameter, not an index; a transfer older than the
  window is invisible to discovery. Asking about it directly by transaction hash still works.

## Decoding

- **One of the two live message layouts is decoded field by field.** The later deployment's layout is
  parsed and pinned by tests against messages this project produced. The earlier deployment's message
  body is shorter and this rail does not decode it: those transfers are refused with
  `UNREADABLE_MESSAGE` in the product path rather than being attempted on a guess. (The spike scripts
  under `scripts/spike_*.py` do complete such a transfer by handing the message through unchanged,
  which is what demonstrated the older deployment end to end. The product path does not do that,
  because a rail that skips its own checks to move money is the rail this project exists to avoid.)
- **Cross-checking is only possible when there are two decodes.** `MESSAGE_INCONSISTENT` fires when our
  decode and the attestation service's decode disagree. When the service offers no decode, there is
  nothing to compare, and the receipt says so (`provenance: attestation-service`).

## Refusals

- **`ALREADY_DELIVERED` is read from the destination contract's record for the nonce.** That works on
  the later deployment. On the earlier deployment the same read is not available, so a repeat delivery
  may surface as `PREFLIGHT_REVERT` with the destination's (empty) revert text as evidence. Money
  still does not move; only the name of the refusal is less specific.
- **A simulation is the destination's answer, not a proof of the future.** It is evaluated against the
  state at simulation time. Between simulating and broadcasting, the destination can change; the
  destination contract is the final authority, and its revert is what the receipt then records.

## Dependencies

- **The attestation service is a dependency.** "Pending" and "final" come from it. The rail does not
  and cannot verify a signature itself; the destination contract does that, and it is the contract
  that refuses a bad attestation.
- **Public RPC endpoints rate-limit.** Discovery and the pairing proof read through public endpoints
  by default. Every endpoint is configurable per domain.

## Operations

- **The HTTP surface has no authentication.** It executes with the executor's key and finishes
  transfers it can. It binds to `127.0.0.1` by default. Do not expose it without putting
  authentication and rate limiting in front of it.
- **The executing wallet needs gas** on the destination chain. On these testnets that gas was supplied
  from this project's own testnet funds; nothing here sponsors it.
- **No key material is held by the rail.** The payer signs in their own wallet; the executor's key
  lives in the environment of whatever runs the execution call. Nothing in this repository reads a
  secret from a file it does not own.

## What is not claimed

- No mainnet deployment, no audit, no SLA, no uptime guarantee.
- No trusted cross-chain knowledge: the rail never asserts what one chain knows about another. It
  reads each chain for itself and reports the pair.
- The "received twice" check covers the window that was read. It is evidence about that window, not a
  global uniqueness proof.
- The tests cover the decisions and the decoding. They are not a substitute for an audit.
