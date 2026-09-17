# Limitations

This is a testnet rail. Every claim the README makes is reproducible from this repository, and this
file lists what it does not do. Where the rail cannot decide, it refuses by name rather than
approximating; the codes below are the honest edges of that.

## Scope

- **Testnets only.** Domains 0 (Ethereum Sepolia), 2 (Optimism Sepolia), 3 (Arbitrum Sepolia),
  6 (Base Sepolia) and 7 (Polygon Amoy). Nothing here has been run against mainnet, and no part of it
  is audited.
- **Five domains watched, and only where the deployment exists.** The later deployment is present on
  all five; the earlier one only on domains 0 and 6. A transfer to a chain this rail does not watch is
  refused with `UNSUPPORTED_DOMAIN`, and one bound for a chain the *deployment* is not on is refused
  with `DEPLOYMENT_ABSENT`. Both are refusals, not failures: there is no contract to call.
- **Bounded discovery.** Transfers are found by reading a block range of each source chain's log
  stream through a public RPC. That range is a parameter, not an index; a transfer older than the
  window is invisible to discovery. Asking about it directly by transaction hash still works.
- **Only one direction has been exercised end to end.** The executable path was delivered
  base-sepolia → ethereum-sepolia, because that is where the payer wallet holds both value and gas.
  The rail is written against the message rather than against a direction, and the other destination
  chains are configured and verified on chain, but no delivery to domains 2, 3 or 7 has been made:
  that is a funding question (testnet USDC and gas at the payer address on those chains), not a code
  path.

## Decoding

- **Both live message layouts are decoded field by field, and neither is guessed at.** The offsets
  were derived from real messages this project produced and then cross-checked against the messenger's
  own deposit event on the source chain — the nonce, amount and recipient the event carries are the
  same values the decoder produces. A message whose length matches neither layout is refused with
  `UNREADABLE_MESSAGE` instead of being read at whatever offsets happen to be plausible.
- **The older layout has no destination caller**, because that deployment's burn takes none. The rail
  reads that as "no caller field" rather than as the zero address, and skips the caller check for it;
  a zero address there would have claimed a transfer was open by policy when the protocol simply does
  not carry the field. Its nonce is a counter rather than a 32-byte word, and the rail normalises both
  spellings to one identity.
- **Address-shaped fields are compared as bytes, never as text.** A destination on a chain with its
  own address alphabet reports the same thirty-two bytes differently, and reading that as a conflict
  would refuse a transfer that two agreeing decoders both described correctly. When the bytes agree,
  the second decoder's notation is adopted so the receipt shows the value the way the destination
  chain writes it.
- **A field is EVM-shaped only when its first twelve bytes are zero.** A full-width value — a
  destination whose addresses use all thirty-two bytes — is read as it stands. This was a real defect:
  the rail was taking the last twenty bytes of a recipient on a chain with full-width addresses, which
  is a different address. The money was never at risk (the destination contract verifies the message),
  but the rail's own evidence said the wrong thing, and a request checked against it would have been
  checked against nobody. It is fixed and pinned by a test against the real message that exposed it.
- **The older layout's fields are read as EVM-shaped addresses.** That layout is twenty-byte fields by
  construction, and the deployment carrying it is only present on domains 0 and 6. A transfer from it
  to a chain with full-width addresses would be read with the same twenty-byte rule, and the rail's
  answer for it should be treated as unverified.
- **Cross-checking is only possible when there are two decodes.** `MESSAGE_INCONSISTENT` fires when our
  decode and the attestation service's decode disagree. When the service offers no decode, there is
  nothing to compare, and the receipt says so (`provenance: attestation-service`).

## The invariant

- **Delivery is read from the destination's own receipt event**, keyed by source domain and nonce. That
  key exists on both live deployments, which is what lets the older deployment be paired here. The
  older deployment's *storage* does not answer the newer contract's replay question, so its
  `usedNonces` read is unavailable.
- **`ALREADY_DELIVERED` from the contract's own record therefore applies to the later deployment.** On
  the earlier one a repeat delivery may surface as `PREFLIGHT_REVERT` with the destination's (empty)
  revert text as evidence. Money still does not move; only the name of the refusal is less specific.
- **The value check compares the mint to the burn less the fee the message carries.** It is measured
  from the token's own transfer event in the minting transaction. When that transaction cannot be read,
  the receipt says `null` rather than calling it broken: an unmeasured value is not a mismatch.
- **Everything is windowed.** The "received twice" count and the value check cover the block range that
  was read. That is evidence about that window, not a global uniqueness proof.
- **A simulation is the destination's answer, not a proof of the future.** It is evaluated against the
  state at simulation time. Between simulating and broadcasting, the destination can change; the
  destination contract is the final authority, and its revert is what the receipt then records.

## Dependencies

- **The attestation service is a dependency.** "Pending" and "final" come from it. The rail does not
  and cannot verify a signature itself; the destination contract does that, and it is the contract
  that refuses a bad attestation.
- **Public RPC endpoints rate-limit.** Discovery, the pairing proof and the watcher read through public
  endpoints by default. Every endpoint is configurable per domain, and a domain whose endpoint is slow
  slows the whole pass: the pass is sequential by design, because a verdict about one transfer must not
  depend on another transfer's timing.

## Operations

- **The HTTP surface has no authentication.** It executes with the executor's key and finishes
  transfers it can. It binds to `127.0.0.1` by default. Do not expose it without putting
  authentication and rate limiting in front of it.
- **The watcher does not spend.** It observes, names and journals; finishing a transfer stays an
  explicit act. A pass writes one journal file per run, and a failed pass (a stranded transfer or a
  double delivery) exits non-zero so a scheduler can treat it as a failed job.
- **The executing wallet needs gas** on the destination chain. On these testnets that gas was supplied
  from this project's own testnet funds; nothing here sponsors it.
- **No key material is held by the rail.** The payer signs in their own wallet; the executor's key
  lives in the environment of whatever runs the execution call. Nothing in this repository reads a
  secret from a file it does not own.

## What is not claimed

- No mainnet deployment, no audit, no SLA, no uptime guarantee.
- No trusted cross-chain knowledge: the rail never asserts what one chain knows about another. It
  reads each chain for itself and reports the pair.
- No hosted deployment: the surface runs where the environment runs, and the demo is that instance.
- The tests cover the decisions, the decoding and the invariant's arithmetic. They are not a substitute
  for an audit.
