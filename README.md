# Veritas — AI Delegate Accountability Protocol

A reusable GenLayer Intelligent Contract primitive that makes AI (or human)
governance delegates provably accountable to the members who delegate stake
to them, by checking their stated voting rationale against real-world
outcomes through validator consensus, and automatically slashing/decaying
delegates whose reasoning stops matching reality.

Deployed reference instance (GenLayer StudioNet):
`0x0E700fFBA3F6679232d4Aa533C6f879A28e69614`

---

## Table of contents

1. [The problem this solves](#the-problem-this-solves)
2. [Why this needs GenLayer consensus](#why-this-needs-genlayer-consensus-not-a-backend)
3. [Core concepts](#core-concepts)
4. [State machines](#state-machines)
5. [Full method reference](#full-method-reference)
6. [Walkthrough: one complete accountability cycle](#walkthrough-one-complete-accountability-cycle)
7. [Consensus design — how we avoid `undetermined` status](#consensus-design--how-we-avoid-undetermined-status)
8. [Escrow safety model](#escrow-safety-model)
9. [Integrity score & slashing math](#integrity-score--slashing-math)
10. [Integrating Veritas from another contract](#integrating-veritas-from-another-contract)
11. [Security considerations & known limitations](#security-considerations--known-limitations)
12. [Files in this repo](#files-in-this-repo)
13. [Running it yourself](#running-it-yourself)
14. [Live integration testing](#live-integration-testing)
15. [Glossary](#glossary)

---

## The problem this solves

DAOs increasingly let an "AI agent" cast governance votes on behalf of stake
that other members hand it. In every implementation we could find, the
member is trusting the model on faith: nobody checks whether the delegate's
stated reasoning for a vote ("I voted YES because this grant will ship a
working integration by Q3") ever matched what actually happened. There is no
on-chain mechanism that:

- forces a delegate's vote to carry a falsifiable claim,
- independently checks that claim against evidence once the real-world
  outcome is knowable,
- translates a pattern of bad reasoning into concrete consequences (lost
  voting weight, slashed collateral, compensation for the people who trusted
  it) — automatically, without a human moderator adjudicating each case.

Veritas is that mechanism, built as a standalone primitive so any DAO's
governance stack can plug into it via cross-contract calls (see
[Integrating Veritas from another contract](#integrating-veritas-from-another-contract))
rather than reimplementing the accountability loop from scratch.

## Why this needs GenLayer consensus (not a backend)

The core state transition this contract performs — "does this delegate's
rationale match what actually happened" — is a **subjective judgment over
external, changing evidence** (web pages, sometimes an image), and that
judgment **gates real money and real governance weight** (slashing,
compensation payouts, vote-weight decay). Three things follow from that:

- A single off-chain server computing this judgment would just mean
  everyone is trusting *that server's* LLM call and *that server's* claim
  about what a webpage said at a point in time. There would be no way for
  an affected delegate or delegator to challenge or verify the finding.
- GenLayer's validator consensus makes the judgment Byzantine-fault
  tolerant: multiple independent nodes (running different LLM backends —
  in our live test, a mix of Claude, GPT, and Gemini models behind
  different providers) each re-derive the evidence and the judgment, and
  the result only finalizes if a majority agrees.
- Because the judgment happens **on-chain**, the rationale, the evidence
  URLs, the verdict, and the resulting state change (score update, slash,
  compensation pool credit) are all public and auditable by anyone,
  including the delegators who are relying on the delegate.

This is precisely the class of problem GenLayer is for: a decision that
can't be reduced to a deterministic function call, but that needs to settle
into deterministic, disputable on-chain state.

## Core concepts

| Concept | What it is |
|---|---|
| **Delegate** | An AI or human agent that registers with a bonded GEN escrow and casts governance votes carrying delegated weight. Identified by a caller-chosen `delegate_id` string; controlled by whichever address registered it. |
| **Delegation** | A member locking GEN "behind" a delegate they trust. This GEN is not spent — it determines the delegate's voting weight and is the pool that compensation claims draw from if the delegate is later slashed. |
| **Proposal** | A governance item with a `voting_deadline` and a `resolution_deadline`, plus one or more `outcome_evidence_urls` describing where its real-world result can later be checked. |
| **Vote** | One delegate's YES/NO/ABSTAIN choice on one proposal, carrying the delegate's effective weight and a mandatory, substantive **rationale** (min. 20 characters) plus up to 5 evidence URLs. Abstain votes carry no rationale and are never checked. |
| **Outcome resolution** | The consensus-critical step: after the resolution window opens, `resolve_vote_outcome` fetches the proposal's evidence, optionally interprets a supplied image, and runs a leader/validator judgment comparing the vote's rationale against what the evidence shows. Produces one of `ALIGNED` / `MISALIGNED` / `INCONCLUSIVE`. |
| **Integrity score** | A 0–10000 (0.00%–100.00%) fixed-point score per delegate, updated by an exponential moving average after every resolved vote. Aligned → pulled toward 10000; misaligned → pulled toward 0; inconclusive → pulled toward the neutral 7500 starting prior. |
| **Effective weight** | A delegate's *actual* voting weight, derived from `total_delegated_atto` scaled down once integrity drops below a floor (see [math](#integrity-score--slashing-math)). Decays continuously, well before slashing kicks in. |
| **Slash** | When integrity drops below a hard threshold, 10% of the delegate's *current remaining* bond moves into a compensation pool. Fires again on every subsequent resolution while integrity stays below threshold. |
| **Compensation pool** | Per-delegate GEN accumulated from slashes, claimable pro-rata by delegators who had an active delegation to that delegate. |

## State machines

### Delegate lifecycle

```
                register_delegate (payable, >= 1 GEN)
                            │
                            ▼
                      ┌───────────┐
        top_up_bond   │  ACTIVE   │  request_unbond
        (payable) ──► │           │ ───────────────►  ┌────────────┐
                      └───────────┘                    │ UNBONDING  │
                            │                           └────────────┘
              integrity < 30% on a                            │
              resolved vote (repeatable)                      │ withdraw_bond
                            │                                  │ (after 14-day
                            ▼                                  │  cooldown)
                    bond fully consumed                        ▼
                     by repeated slashes                 ┌───────────┐
                            │                             │  EXITED   │
                            ▼                             └───────────┘
                    ┌────────────────┐
                    │ SLASHED_OUT     │   (bonded_atto == 0; delegate can no
                    └────────────────┘    longer be delegated new stake)
```

`request_unbond` is blocked while the delegate has any unresolved YES/NO
vote outstanding (`_has_unresolved_votes`), so a delegate cannot dodge an
in-flight accountability check by exiting early.

### Proposal / Vote lifecycle

```
create_proposal                                 close_voting
      │                                          (after voting_deadline)
      ▼                                                │
  ┌────────┐   cast_vote (per delegate)   ┌──────────────────┐
  │  OPEN   │ ───────────────────────────► │  (still OPEN or   │
  └────────┘                               │  VOTING_CLOSED)   │
      │                                    └──────────────────┘
      │ cancel_proposal                              │
      │ (only if zero votes so far)                  │ resolve_vote_outcome
      ▼                                               │ per delegate, after
  ┌───────────┐                                       │ resolution_deadline
  │ CANCELLED │                                       ▼
  └───────────┘                             all non-abstain votes resolved?
                                                       │
                                                       ▼
                                              ┌───────────┐
                                              │ RESOLVED  │
                                              └───────────┘
```

Each `VoteRecord` independently moves from `resolved: false` to
`resolved: true` with an `outcome_verdict` the first time either
`resolve_vote_outcome` succeeds **or** `reclaim_stale_vote_bond_share`
fires after the resolution window has been open for 30+ days unclaimed
(the stuck-fund recovery path — see [Escrow safety model](#escrow-safety-model)).

## Full method reference

All money amounts are `u256` atto-GEN (value × 10¹⁸) unless noted. `now_ts`
parameters are caller-supplied Unix timestamps — see the
[time-handling convention](docs/DESIGN.md#time-handling-convention) for why
this is safe.

### Delegate lifecycle

| Method | Payable | Who can call | Effect |
|---|---|---|---|
| `register_delegate(delegate_id, metadata_uri)` | ✅ (≥ 1 GEN) | Anyone (once per `delegate_id`) | Creates a `Delegate` with `bonded_atto = msg.value`, `integrity_score = 7500`, `status = ACTIVE`. Caller becomes the delegate's `controller`. |
| `top_up_bond(delegate_id)` | ✅ | Controller only | Adds `msg.value` to `bonded_atto`. |
| `request_unbond(delegate_id, now_ts)` | ✗ | Controller only | Moves `ACTIVE → UNBONDING`, records `unbond_requested_at`. Reverts if any non-abstain vote by this delegate is still unresolved. |
| `withdraw_bond(delegate_id, now_ts)` | ✗ | Controller only | After a 14-day cooldown from `unbond_requested_at`, zeroes `bonded_atto`, sets `status = EXITED`, transfers the remaining bond to the controller. |

### Delegation

| Method | Payable | Who can call | Effect |
|---|---|---|---|
| `delegate_stake(delegate_id)` | ✅ | Anyone | Adds `msg.value` to the caller's `DelegationRecord.principal_atto` for this delegate (creating the record if new) and to the delegate's `total_delegated_atto`. Delegate must be `ACTIVE` or `UNBONDING`. |
| `withdraw_delegation(delegate_id)` | ✗ | The delegator | Zeroes the caller's active `principal_atto`, deducts it from the delegate's `total_delegated_atto`, and transfers it back. |

### Proposals & voting

| Method | Payable | Who can call | Effect |
|---|---|---|---|
| `create_proposal(proposal_id, title, description, outcome_evidence_urls, voting_deadline, resolution_deadline, now_ts)` | ✗ | Anyone | Creates a `Proposal` in `OPEN` status. Requires ≥1 and ≤5 `http(s)://` evidence URLs, and `resolution_deadline >= voting_deadline`. |
| `cancel_proposal(proposal_id)` | ✗ | Creator or owner | Only while `OPEN` and zero votes exist. Sets `CANCELLED`. |
| `cast_vote(proposal_id, delegate_id, choice, rationale, evidence_urls, now_ts)` | ✗ | The delegate's controller | Requires `choice ∈ {YES, NO, ABSTAIN}`, `rationale` ≥ 20 chars, `now_ts <= voting_deadline`, delegate `ACTIVE`, and exactly one vote per `(proposal_id, delegate_id)`. Records the vote with the delegate's **current effective weight** (see [math](#integrity-score--slashing-math)) and adds that weight to the proposal's running tally. |
| `close_voting(proposal_id, now_ts)` | ✗ | Anyone | Requires `now_ts > voting_deadline`. Moves `OPEN → VOTING_CLOSED`. |

### Outcome resolution (consensus-critical)

| Method | Payable | Who can call | Effect |
|---|---|---|---|
| `resolve_vote_outcome(proposal_id, delegate_id, now_ts, supplemental_image)` | ✗ | Anyone | Requires `now_ts >= resolution_deadline` and the vote not already resolved. Fetches the proposal's evidence URLs, optionally interprets `supplemental_image` (pass an empty `bytes` value — a zero-length `Uint8Array` from a JS client — if there is no image), and runs the bucketed leader/validator judgment. Sets `resolved = true`, `outcome_verdict`, `alignment_bucket`, `resolution_summary`. Then updates the delegate's integrity score and, if applicable, triggers a slash. Returns the verdict string. |

### Compensation & recovery

| Method | Payable | Who can call | Effect |
|---|---|---|---|
| `claim_compensation(delegate_id)` | ✗ | A delegator with an active delegation to this delegate | Pays the caller `compensation_pool_atto × (their principal / delegate's total_delegated_atto)`, capped at the pool balance. Zeroes the claimant's delegation record so it can't be claimed twice. Returns the amount paid. |
| `reclaim_stale_vote_bond_share(proposal_id, delegate_id, now_ts)` | ✗ | Anyone | If `now_ts >= resolution_deadline + 30 days` and the vote is still unresolved, marks it `INCONCLUSIVE` with **no** integrity penalty and **no** slash. Moves no GEN — it only unblocks state so the delegate isn't held hostage by nobody bothering to call `resolve_vote_outcome`. |

### Admin

| Method | Who can call | Effect |
|---|---|---|
| `set_paused(paused)` | Owner | Blocks new registrations/delegations/proposals/votes/resolutions while `true`. Never touches funds already escrowed. |
| `transfer_ownership(new_owner)` | Owner | Changes `owner`. |

### Views (all read-only, no gas beyond a call)

| Method | Returns |
|---|---|
| `get_delegate(delegate_id)` | Full delegate record, including live `effective_weight_bps`. |
| `list_delegate_ids()` | All registered delegate IDs. |
| `get_proposal(proposal_id)` | Full proposal record including current YES/NO/ABSTAIN weight tallies. |
| `list_proposal_ids()` | All proposal IDs. |
| `get_vote(proposal_id, delegate_id)` | Full vote record, including rationale, evidence, and (once resolved) verdict + summary. |
| `get_delegation(delegator, delegate_id)` | A delegator's current principal and active flag for one delegate. |
| `get_protocol_stats()` | Protocol-wide counters: total delegates, proposals, votes, slash events, GEN slashed, GEN compensated. |
| `preview_effective_weight(delegate_id)` | The exact bps/atto math behind a delegate's current effective weight, for frontends. |

## Walkthrough: one complete accountability cycle

This mirrors what `scripts/live_smoke_test.mjs` actually executed against
the deployed instance.

1. **Alice registers as a delegate** with a 1 GEN bond:
   `register_delegate("alice-agent", "ipfs://alice-model-card")`, sending
   1 GEN. → `Delegate{bonded_atto: 1e18, integrity_score: 7500, status: ACTIVE}`.
2. **Bob delegates 2 GEN to Alice**:
   `delegate_stake("alice-agent")`, sending 2 GEN. → Alice's
   `total_delegated_atto` becomes 2e18; Bob's delegation record tracks his
   2 GEN principal.
3. **Bob creates a proposal** to fund a grant, pointing at where the
   grant's real-world outcome can be checked:
   `create_proposal("prop-1", "Fund widget grant", "...", ["https://example.com/widget-status"], votingDeadline, resolutionDeadline, now)`.
4. **Alice votes YES with a falsifiable rationale**:
   `cast_vote("prop-1", "alice-agent", "YES", "This grant will ship a working widget integration by the Q3 deadline based on the team's track record.", ["https://example.com/widget-status"], now)`.
   The vote is recorded carrying Alice's full 2 GEN of effective weight
   (100% — her integrity is at the neutral starting score, above the decay
   floor).
5. **Time passes; the resolution window opens.** Anyone calls
   `resolve_vote_outcome("prop-1", "alice-agent", laterTimestamp, emptyBytes)`.
   Validators independently fetch `https://example.com/widget-status`,
   derive a stable signal from it (`POSITIVE`/`NEGATIVE`/`MIXED`/`UNCLEAR`),
   and independently ask an LLM to score how well Alice's rationale matches
   that signal, bucketed 0–4. If ≥3 of 5 validators land on the same or an
   adjacent bucket, consensus reaches `MAJORITY_AGREE` and the vote resolves.
6. **Outcome A — the grant shipped.** Verdict `ALIGNED`. Alice's integrity
   score EMA-updates upward (75.00% → 81.25%). No slash. Bob's delegation is
   untouched.
7. **Outcome B — the grant was cancelled, repeated across several votes.**
   Verdict `MISALIGNED` each time. Alice's score EMA-decays toward 0. Once
   it crosses below 50%, her *effective* weight on future votes starts
   shrinking even though her `total_delegated_atto` (2 GEN) is unchanged.
   Once it crosses below 30%, each further misaligned resolution slashes
   10% of her *current* bond into her compensation pool. After Bob notices,
   he calls `withdraw_delegation("alice-agent")` to pull his remaining
   principal, or, if a slash already happened, `claim_compensation("alice-agent")`
   to recover his pro-rata share of what was slashed.

## Consensus design — how we avoid `undetermined` status

Every non-deterministic step is deliberately **tolerant**, not strict —
this was the explicit design brief, and it's been validated live (see
[Live integration testing](#live-integration-testing)):

- **Bucketed alignment, not raw scores.** The LLM's 0–100 alignment score
  is mapped into 5 coarse bands (`_bucket_score`). Validators agree if
  their bucket matches the leader's **or is adjacent** — a strictly larger
  tolerance envelope than a fixed numeric window, because it scales with
  how coarse the underlying judgment genuinely is.
- **Derived signals, not raw page text.** Raw web content is reduced to
  one of four stable strings (`POSITIVE` / `NEGATIVE` / `MIXED` /
  `UNCLEAR`) via keyword-based derivation (`_derive_page_signal`) *before*
  any comparison. Comparing raw fetched text directly is the #1 cause of
  spurious `undetermined` results in web-consensus contracts, because
  timestamps, ads, and minor DOM differences make byte-for-byte agreement
  nearly impossible.
- **Majority-signal agreement across multiple URLs**, not unanimous
  agreement. If a proposal cites 3 evidence URLs and only 1 gives a
  different signal between leader and validator, that's still treated as
  agreement (a single stale/cached page shouldn't torpedo consensus).
- **Classified errors, not raw exceptions.** Every expected failure mode
  is prefixed (`[EXPECTED]` / `[EXTERNAL]` / `[TRANSIENT]` / `[LLM_ERROR]`)
  so `_handle_leader_error` can decide whether validators *should* agree
  (both hit the same deterministic or transient failure) or must disagree
  (an LLM produced garbage — force rotation rather than lock in bad
  output). Our live test surfaced one case *not* covered by this
  classification — see the honest caveat below.
- **`strict_eq` is never used on anything touching an LLM or a live web
  page.** Every nondeterministic operation goes through the custom
  `gl.vm.run_nondet_unsafe` leader/validator pattern, which we fully
  control end-to-end, rather than the stricter convenience wrappers.

**Honest caveat, found during live testing, not by inspection:** if
`gl.nondet.exec_prompt` is fed malformed input (in our case, a test-script
bug passed 2 raw ASCII bytes where an empty image was intended) it can
raise a bare `SystemError` instead of a `gl.vm.UserError`. That still falls
into `_handle_leader_error`'s final `except Exception: return False`
clause, so validators disagree rather than silently agreeing on garbage —
but because *every* validator hits the identical malformed input, they all
disagree identically, and if that were to happen for every leader rotation
the transaction would end `UNDETERMINED` rather than eventually converging.
This is a legitimate finding: **it is caused by bad caller input, not
non-determinism in the contract's own logic**, and it fails safe (no state
change, no fund movement) rather than failing open. It's documented here
rather than silently fixed by loosening the error classification, because
loosening it would mean agreeing on genuinely broken LLM output — the
wrong trade to make. Callers should always pass a real zero-length byte
value (not a string) when there is no image.

## Escrow safety model

Every GEN transfer in this contract funnels through a single function,
`_send_gen`, mirroring the audited pattern from ShipBond: read the ledger
field into a local, **zero the ledger field, persist state, then transfer**
— never the reverse. This makes double-payout structurally unreachable: a
second call into any payout path finds its ledger field already at zero and
reverts before ever reaching `_send_gen`.

Every escrow has a small, enumerated, exhaustive set of exits:

| Escrow | Success exit | Failure/decay exit | Cancellation exit | Timeout/recovery exit |
|---|---|---|---|---|
| Delegate bond | `withdraw_bond` after cooldown | Slashed piecemeal via `_maybe_slash` into compensation pool | — | `request_unbond` cannot be blocked forever — it only requires zero *unresolved* votes, and every vote has its own timeout exit below |
| Delegated principal | `withdraw_delegation` any time it's still active | Slashed portion becomes claimable via `claim_compensation` | `withdraw_delegation` before any slash also serves as the pre-loss exit | — |
| Vote resolution | `resolve_vote_outcome` succeeds → verdict recorded | `MISALIGNED` verdict recorded, feeds integrity/slash | `cancel_proposal` before any vote exists | `reclaim_stale_vote_bond_share` after 30 days unclaimed |
| Compensation pool | `claim_compensation` pro-rata payout | — | — | Pool sits idle (no funds at risk of loss) until claimed; nothing forces claims, but nothing expires them either |

No path can strand funds indefinitely, and no path can be triggered twice
against the same ledger balance.

## Integrity score & slashing math

See [docs/DESIGN.md](docs/DESIGN.md#slash--decay-math-worked-example) for a
fully worked numeric example (six consecutive misaligned votes, exact
integrity-score trajectory, exact slash amounts). Summary of the formulas:

```
new_integrity = (old_integrity * 750 + sample * 250) / 1000
  where sample = 10000 if ALIGNED, 0 if MISALIGNED, 7500 if INCONCLUSIVE

effective_weight_bps =
  10000                                            if integrity >= 5000
  1000 + (integrity * 9000) / 5000                 if integrity < 5000   (linear decay to a 10% floor)

slash_amount = current_bonded_atto * 1000 / 10000  (10%)  — fires once per
  resolution while integrity_score < 3000, taken from whatever remains of
  the bond at that moment (not the original bond)
```

All three curves are named constants at the top of the contract
(`INTEGRITY_EMA_WEIGHT_PERMILLE`, `INTEGRITY_DECAY_FLOOR`,
`INTEGRITY_SLASH_THRESHOLD`, `SLASH_BPS`) specifically so a fork can retune
the aggressiveness of the accountability loop without touching control
flow.

## Integrating Veritas from another contract

Because Veritas is a primitive, not an app, another DAO's governance
contract can read a delegate's live, consensus-verified track record
without reimplementing any of this:

```python
veritas = gl.get_contract_at(Address(VERITAS_CONTRACT_ADDRESS))

delegate_info = veritas.view().get_delegate(delegate_id)
integrity = int(delegate_info["integrity_score"])
effective_weight_atto = int(delegate_info["total_delegated_atto"])  # or call
weight_preview = veritas.view().preview_effective_weight(delegate_id)

if integrity < 3000:
    # This DAO's own governance contract can, for example, refuse to
    # count this delegate's vote at all, regardless of what Veritas itself
    # already decided to do about slashing.
    ...
```

Writes (e.g. having your own contract trigger `resolve_vote_outcome` as
part of its own proposal-finalization flow) work the same way via
`.emit()` — see the `write-contract` skill's cross-contract-interaction
notes for the `on="accepted"` vs `on="finalized"` tradeoff.

## Security considerations & known limitations

- **Rationale quality is enforced by length, not substance, at cast time.**
  A delegate could write 20+ characters of vague hedging that technically
  passes validation. The substance check happens later, at resolution
  time, when the LLM judges the rationale against evidence — a vague
  rationale simply tends to score as `INCONCLUSIVE` rather than
  `ALIGNED`, which is the intended incentive (specificity is rewarded
  because it's what makes `ALIGNED` verdicts achievable).
- **Evidence URLs are supplied by the proposal creator, not verified as
  authoritative.** A malicious proposal creator could point resolution at
  a page they control. This is a proposal-design problem, not a contract
  bug — the same way a Kleros-style dispute is only as good as the
  evidence submitted to it. DAOs adopting this primitive should require
  evidence URLs to point at neutral, hard-to-manipulate sources (a
  project's own GitHub repo, a public block explorer, etc.) as a matter of
  proposal-creation policy.
- **`supplemental_image` must be a genuine zero-length byte value when
  unused**, not a string like `"0x"` — see the honest caveat under
  [Consensus design](#consensus-design--how-we-avoid-undetermined-status).
- **The installed `genlayer` CLI (v0.39.2) cannot send GEN value with
  `write`** (it hardcodes `value: 0n`), so payable methods must currently
  be called via `genlayer-js` directly or another SDK. This is a CLI
  limitation, not a contract issue, and is expected to be fixed upstream.
- **No on-chain appeal path for a resolved verdict beyond GenLayer's
  native transaction appeal mechanism** (`genlayer appeal <txHash>`).
  Veritas does not implement its own second-layer appeals process; it
  relies on GenLayer's protocol-level appeal/re-consensus mechanism for
  disputing a specific transaction's result.

## Files in this repo

- [`contracts/veritas_delegate.py`](contracts/veritas_delegate.py) — the
  contract (1,400+ lines).
- [`contracts/abi.json`](contracts/abi.json) — extracted ABI schema.
- [`tests/test_veritas_direct.py`](tests/test_veritas_direct.py) — 18
  direct-mode tests covering every state transition and revert path.
- [`scripts/live_smoke_test.mjs`](scripts/live_smoke_test.mjs) /
  [`scripts/resolve_existing_vote.mjs`](scripts/resolve_existing_vote.mjs)
  — live integration scripts used against the deployed instance.
- [`docs/DESIGN.md`](docs/DESIGN.md) — storage layout, time-handling
  convention, and the fully worked slash-math example.

## Running it yourself

```bash
pip install genvm-linter genlayer-test
genvm-lint check contracts/veritas_delegate.py
pytest tests/test_veritas_direct.py -v
```

Runner version pinned to
`py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6` — verified
via `genvm-lint check`, `genvm-lint schema`, and `genvm-lint typecheck` to
load, validate, and extract its ABI cleanly, and confirmed live on
StudioNet (see below).

To deploy:

```bash
genlayer deploy --contract contracts/veritas_delegate.py
```

## Live integration testing

`scripts/live_smoke_test.mjs` and `scripts/resolve_existing_vote.mjs` drive
the deployed contract end-to-end via `genlayer-js` directly. Both scripts
sign with a dedicated throwaway keystore whose password is read from an
env var, never hardcoded:

```bash
npm install
export VERITAS_TEST_KEYSTORE_PASSWORD="<password for your own test keystore>"
node scripts/live_smoke_test.mjs
```

This was run against `0x0E700fFBA3F6679232d4Aa533C6f879A28e69614` on
StudioNet with 5 real validators running a mix of Claude/GPT/Gemini
models. Every step (`register_delegate`, `delegate_stake`,
`create_proposal`, `cast_vote`, `resolve_vote_outcome`) reached
`MAJORITY_AGREE` / `ACCEPTED`, including the vision/web-fetch consensus
round, and the resolved vote carried a genuine LLM-generated verdict and
summary (`INCONCLUSIVE`, "The evidence signal is unclear and neither
confirms nor contradicts the claim...").

## Glossary

- **atto-GEN** — GEN denominated at 10⁻¹⁸ scale (i.e. `value * 10**18`),
  the standard cross-chain money representation used throughout this
  contract's `u256` fields.
- **bps** — basis points; 1 bps = 0.01%. `10000 bps = 100%`.
- **Bucket** — one of 5 coarse bands (0–4) that a 0–100 alignment score is
  mapped into before consensus comparison.
- **EMA** — exponential moving average; how the integrity score updates
  after each resolved vote.
- **Ledger field** — the storage field that is the single source of truth
  for how much GEN is currently escrowed for a given purpose (e.g.
  `bonded_atto`, `compensation_pool_atto`). Payout logic reads only from
  these, never re-derives amounts from anything else.
