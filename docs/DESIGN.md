# Veritas — Design Notes

This document goes deeper than the README on the *why* behind specific
implementation choices, plus reference material (storage layout, exact
math) that a builder extending or auditing this contract needs.

## Table of contents

1. [Storage layout (append-only from here on)](#storage-layout-append-only-from-here-on)
2. [Time-handling convention](#time-handling-convention)
3. [Slash / decay math, worked example](#slash--decay-math-worked-example)
4. [Why bucketed comparison instead of exact-match](#why-bucketed-comparison-instead-of-exact-match)
5. [Why derived signals instead of raw page text](#why-derived-signals-instead-of-raw-page-text)
6. [Error classification decision table](#error-classification-decision-table)
7. [Why `run_nondet_unsafe` and not the convenience wrappers](#why-run_nondet_unsafe-and-not-the-convenience-wrappers)
8. [Why votes carry weight at cast-time, not at tally-time](#why-votes-carry-weight-at-cast-time-not-at-tally-time)
9. [What direct-mode tests do and don't prove](#what-direct-mode-tests-do-and-dont-prove)
10. [What the live integration run proved](#what-the-live-integration-run-proved)
11. [Extending this primitive](#extending-this-primitive)
12. [Explicitly out of scope](#explicitly-out-of-scope)

---

## Storage layout (append-only from here on)

`contracts/veritas_delegate.py` declares storage fields in this order. If
this contract is ever made upgradable, **only append new fields at the
end** — do not reorder or insert, since GenLayer storage layout is
positional (see the `write-contract` skill's storage rules).

| # | Field | Type | Purpose |
|---|---|---|---|
| 1 | `owner` | `Address` | Admin address; can pause and transfer ownership. |
| 2 | `delegates` | `TreeMap[str, Delegate]` | Primary delegate records, keyed by `delegate_id`. |
| 3 | `delegate_ids` | `DynArray[str]` | Insertion-order index over `delegates` for `list_delegate_ids`. |
| 4 | `delegations` | `TreeMap[str, DelegationRecord]` | Keyed by `f"{delegator}:{delegate_id}"`. |
| 5 | `delegation_keys` | `DynArray[str]` | Index over `delegations`. |
| 6 | `proposals` | `TreeMap[str, Proposal]` | Keyed by `proposal_id`. |
| 7 | `proposal_ids` | `DynArray[str]` | Index over `proposals`. |
| 8 | `votes` | `TreeMap[str, VoteRecord]` | Keyed by `f"{proposal_id}:{delegate_id}"`. |
| 9 | `vote_keys` | `DynArray[str]` | Index over `votes`; also scanned by `_has_unresolved_votes` and `_maybe_close_proposal`. |
| 10 | `compensation_receipts` | `DynArray[CompensationClaimReceipt]` | Append-only audit trail of every paid compensation claim. |
| 11–16 | counters | `u256` each | `total_delegates`, `total_proposals`, `total_votes_cast`, `total_slash_events`, `total_slashed_atto`, `total_compensation_paid_atto` — O(1) protocol-wide stats, maintained alongside the collections above rather than recomputed by scanning them. |
| 17 | `paused` | `bool` | Emergency admin gate on new state-creating calls. Never touches already-escrowed funds. |

### Why composite string keys instead of nested maps

GenLayer's `TreeMap` calldata format only supports `str` keys (no nested
`TreeMap[TreeMap[...], ...]`), so every "many-to-many" relationship in this
contract (a delegator's delegation to a specific delegate; a delegate's
vote on a specific proposal) is flattened into a single string key via
`f"{a}:{b}"`. This is why `delegate_id` and `proposal_id` are validated to
be non-empty, caller-chosen strings rather than auto-incrementing
integers — a colon-joined composite key needs both halves to be
unambiguous, human-readable identifiers for debugging and for frontends
reading state directly.

## Time-handling convention

GenVM contract code has no implicit wall clock. Every function that gates
logic on time (`voting_deadline`, `resolution_deadline`, unbond cooldown)
takes an explicit `now_ts: int` parameter from the caller. This is
deliberate, not an oversight:

- Values compared against `now_ts` for pure business-logic gates (has the
  voting deadline passed? has the cooldown elapsed?) can, at worst, let a
  caller invoke a function slightly early or late relative to true wall
  time. The contract already reverts with `[EXPECTED]` if the *state*
  doesn't allow the call (e.g. `cast_vote` still checks `proposal.status
  == STATUS_OPEN` regardless of what `now_ts` claims), so a lying `now_ts`
  cannot manufacture a state transition that wasn't otherwise valid.
- The one place where real-world timing is actually load-bearing for a
  payout-adjacent decision — whether a proposal's outcome has had time to
  materialize — is **not** enforced by trusting `now_ts` at all. It's
  enforced by `resolve_vote_outcome` going out and fetching live evidence
  itself. A caller cannot fake "the grant shipped" by lying about the
  clock; the contract independently checks.
- The 30-day stale-resolution timeout (`RESOLUTION_STALE_AFTER`) is
  measured from `proposal.resolution_deadline`, which was fixed at
  proposal-creation time and cannot be retroactively changed — so a caller
  cannot manufacture an early stale-timeout by passing a large `now_ts`
  to `create_proposal` itself, since `create_proposal` doesn't read
  `now_ts` for anything except the informational `created_at` field.

Frontends should pass block timestamps (or, on Studio/local test
harnesses, whatever monotonic clock the harness exposes) consistently for
all `now_ts` arguments across a single proposal's lifecycle. Passing
wildly inconsistent timestamps across calls on the *same* proposal doesn't
break invariants, but will produce a confusing user experience (e.g. a
vote that appears to have been cast "before" the proposal was "created").

## Slash / decay math, worked example

Assume a delegate registers with 1 GEN bonded and accrues six consecutive
`MISALIGNED` verdicts (this is exactly
`tests/test_veritas_direct.py::test_repeated_misalignment_slashes_and_funds_compensation_pool`).

**Integrity trajectory** (EMA formula:
`new = (old * 750 + sample * 250) / 1000`, `sample = 0` for MISALIGNED):

| Vote # | Integrity before | Integrity after | Crossed slash threshold (3000)? |
|---|---|---|---|
| start | — | 7500 | — |
| 1 | 7500 | 5625 | no |
| 2 | 5625 | 4218 | no |
| 3 | 4218 | 3163 | no (still ≥ 3000) |
| 4 | 3163 | 2372 | **yes — slash #1 fires** |
| 5 | 2372 | 1779 | yes — slash #2 fires |
| 6 | 1779 | 1334 | yes — slash #3 fires |

**Bond trajectory** (`_maybe_slash` runs *after* the integrity update on
every resolution; it slashes 10% of whatever the bond currently is, so
three slashes compound multiplicatively, not additively):

| Slash event | Bond before | Slashed (10%) | Bond after |
|---|---|---|---|
| #1 (after vote 4) | 1.000 GEN | 0.100 GEN | 0.900 GEN |
| #2 (after vote 5) | 0.900 GEN | 0.090 GEN | 0.810 GEN |
| #3 (after vote 6) | 0.810 GEN | 0.081 GEN | 0.729 GEN |

Final state: `bonded_atto ≈ 0.729 GEN` (≈ `0.9³` of the original),
`compensation_pool_atto ≈ 0.271 GEN`. This is exactly what the test
asserts (`bonded_atto < ONE_GEN` and `compensation_pool_atto > 0`), and the
compounding-not-additive behavior is intentional: slashing a *percentage of
current balance* rather than a fixed absolute amount means a delegate can
never be slashed into negative bond, and each subsequent slash is
naturally smaller in absolute terms as the bond shrinks.

**Effective weight**, independently, uses a different (linear, not EMA)
curve and only kicks in below a *higher* threshold (5000, vs. the 3000
slash threshold) — so weight erosion is visible to delegators well before
the harsher slashing consequence arrives:

```
effective_weight_bps(score) =
    10000                                  if score >= 5000
    1000 + (score * 9000) / 5000           if score <  5000
```

At `score = 4218` (after vote 2 above, before any slash), effective weight
is already down to `1000 + (4218*9000)/5000 = 1000 + 7592 = 8592 bps`
(85.92% of nominal) — delegators see influence draining before the bond
itself is touched.

## Why bucketed comparison instead of exact-match

An earlier draft compared raw `alignment_score` integers with a fixed
tolerance window (`abs(a - b) <= 10`). That still risks `undetermined`
status whenever an LLM's score legitimately swings by more than the
window on a genuinely ambiguous case — and ambiguous cases are exactly
when the network most needs to still reach a *determinate* result (which
should usually be `INCONCLUSIVE`, not a stalled transaction). Bucketing
into 5 bands, and treating adjacent bands as agreement only when they map to
the same consequential verdict, gives a larger tolerance envelope while
preserving the boundary between "clearly shipped" (bucket 4), ambiguous
(bucket 2), and "clearly failed" (bucket 0). This prevents validator
disagreement from changing integrity, voting weight, or slashing behavior.

## Why derived signals instead of raw page text

Comparing two independently-fetched copies of the same web page
byte-for-byte is close to guaranteed to fail consensus in practice: ads
rotate, "last updated" timestamps tick over, A/B-tested markup differs,
CDN edge nodes serve slightly different cached snapshots. `_derive_page_signal`
collapses all of that into one of four fixed strings by scanning for a
small vocabulary of outcome-indicating keywords (`shipped`, `cancelled`,
`delayed`, etc.). This is deliberately crude — the point isn't to build a
sophisticated NLP classifier in pure Python, it's to produce a signal
that's *stable enough to compare* and then hand the actual nuanced
judgment to the LLM call, which sees the derived signals as context rather
than trying to agree on raw text itself.

## Error classification decision table

`_handle_leader_error` is the single place every validator in this
contract decides whether to agree or disagree with a leader that errored.
The classification determines the outcome:

| Leader error prefix | Validator behavior | Rationale |
|---|---|---|
| `[EXPECTED]` | Agree only if validator's own message is byte-identical | Business-logic errors (bad input, wrong caller, invalid state) are fully deterministic — if the validator doesn't hit the exact same one, something is actually wrong. |
| `[EXTERNAL]` | Agree only if byte-identical | External API 4xx responses (auth, not-found, bad request) are also expected to be deterministic given the same request. |
| `[TRANSIENT]` | Agree if *both* are transient, regardless of exact message | Network blips, timeouts, and 5xx responses are inherently non-deterministic in *when* they happen, but agreeing "we both got a transient failure" is the correct outcome — it should trigger a retry, not a rotation punishing an innocent leader. |
| `[LLM_ERROR]` or unclassified (including bare `SystemError`/`Exception`) | Always disagree | Force leader rotation rather than risk two nodes independently agreeing on the *same* garbage output for the wrong reasons. See the honest caveat in the README about what happens when *every* leader hits the same malformed-input bug. |

## Why `run_nondet_unsafe` and not the convenience wrappers

GenLayer offers `prompt_comparative` and `prompt_non_comparative` as
shortcuts. Veritas uses the lower-level `gl.vm.run_nondet_unsafe` custom
leader/validator pattern everywhere instead, because:

- `prompt_non_comparative` never re-runs the task — it only asks
  validators to judge the leader's output against criteria. For a
  decision that gates slashing and compensation, that's leader-output-only
  validation: the leader could hallucinate a favorable "match" and a
  validator using only the criteria (not independent re-derivation) might
  rubber-stamp it. Veritas's validator always independently re-fetches
  evidence and re-runs the LLM judgment (`validator_fn` calls `leader_fn()`
  itself), which is a strictly stronger guarantee.
- `prompt_comparative` is closer to what Veritas does, but its built-in
  comparison is a single natural-language "principle" string evaluated by
  another LLM call, with no way to inject the bucketing/majority-signal
  tolerance logic this contract needs. Writing the comparator in Python
  (as `validator_fn` does) makes the tolerance rules explicit, testable in
  direct mode without any LLM involved, and auditable by reading code
  instead of reverse-engineering LLM-judged-LLM-output behavior.

## Why votes carry weight at cast-time, not at tally-time

`cast_vote` snapshots the delegate's `_effective_weight_atto` into the
`VoteRecord.weight_atto` field at the moment of casting, and the
proposal's running tallies (`yes_weight_atto` etc.) are incremented then,
not recomputed later. This is intentional: if a delegate's integrity score
changes *after* they voted (e.g. from an unrelated proposal resolving
first), that must not retroactively change the weight of a vote that
already happened — voters and observers need the tally at any point in
time to be an honest record of what was actually cast, not a live-moving
target. The cost of this design is that `get_proposal`'s weight totals are
a running sum rather than derived on-demand from current delegate state;
that's a deliberate trade in favor of auditability over always-current
weighting.

## What direct-mode tests do and don't prove

`tests/test_veritas_direct.py` (18 tests, all passing) exercises: access
control, input validation, every documented revert message, the full
delegate lifecycle (register → top-up → unbond → withdraw), the full
delegation lifecycle, proposal/vote creation and validation, the
slash/compensation math end-to-end (via mocked web/LLM responses), and the
stale-resolution recovery path. `tests/test_alignment_consensus.py` adds two
deterministic regression tests covering all consequential adjacent-bucket
and non-adjacent-bucket cases. The full suite currently has 20 passing tests.
**What it does not exercise**: actual
validator disagreement/consensus behavior, since direct mode runs only the
leader function — `gl.vm.run_nondet_unsafe`'s `validator_fn` is invoked as
plain Python but there's no second, independently-executing node to
disagree with. That gap is closed by the live integration run below.

## What the live integration run proved

`scripts/live_smoke_test.mjs` was run against the deployed instance
(`0xdb18abE502829D0Ff2FE5856ab1EB3BC6a5a5783`) on GenLayer StudioNet with 5
real validators running a mix of Claude, GPT, and Gemini models behind
different providers (openrouter, llm-router). Results:

- `register_delegate`, `delegate_stake`, `create_proposal`, `cast_vote`
  each reached `MAJORITY_AGREE` / `ACCEPTED` on the first attempt.
- The first `resolve_vote_outcome` attempt hit `MAJORITY_DISAGREE` /
  `UNDETERMINED` — traced to a test-script bug (a literal `"0x"` string
  passed where a zero-length `Uint8Array` was required for the
  `supplemental_image bytes` parameter, which got embedded as 2 raw ASCII
  bytes and crashed the vision-model call inside `leader_fn` with a bare
  `SystemError`). Critically, **this failed safe**: the vote's
  `resolved` flag stayed `false`, no funds moved, no integrity score
  changed, and the contract remained callable.
- After correcting the client-side encoding, the retry reached
  `MAJORITY_AGREE` / `ACCEPTED` and resolved the vote to `INCONCLUSIVE`
  with a genuine LLM-generated summary, and the delegate's integrity
  score updated exactly per the EMA formula (stayed at 7500, since
  `INCONCLUSIVE` samples the neutral prior).

This is meaningful because it validated the two riskiest parts of the
design under real conditions no mock can fully replicate: (1) that
multiple *different* LLM backends can actually converge on the same bucket
often enough for consensus to succeed on a genuinely fuzzy judgment, and
(2) that a client-caused failure degrades to a clean, fund-safe
`UNDETERMINED` rather than corrupting state — the fail-safe property the
whole escrow design is built around.

## Extending this primitive

- **Cross-contract use**: another DAO's governance contract can call
  `cast_vote` / read `get_delegate` via `gl.get_contract_at(...)` to pull
  in Veritas-scored delegate weight without re-implementing any of the
  accountability logic — see the README's
  [Integrating Veritas](../README.md#integrating-veritas-from-another-contract)
  section for a code sample.
- **Swapping the evidence source**: `resolve_vote_outcome` accepts
  arbitrary evidence URLs from the proposal and an optional image; a fork
  could add cross-chain RPC verification (see the `write-contract` skill's
  `verify_deposit` pattern) as an additional evidence leg feeding the same
  bucketed judgment, without changing the consensus mechanics.
- **Alternate slashing curves**: `SLASH_BPS`, `INTEGRITY_SLASH_THRESHOLD`,
  `INTEGRITY_DECAY_FLOOR`, and `INTEGRITY_EMA_WEIGHT_PERMILLE` are the four
  knobs that shape how aggressively the protocol punishes drift; they're
  isolated as named constants specifically so a fork can retune them
  without touching control flow.
- **Multiple evidence images**: `MAX_EVIDENCE_IMAGES` is already set to
  GenVM's documented 2-image vision limit; `resolve_vote_outcome` currently
  only accepts one `supplemental_image` argument, so extending to two
  would mean widening that parameter to a list and threading it through
  `_run_outcome_consensus`.

## Explicitly out of scope

To keep this a primitive rather than an app (per the bounty's own
guidance), Veritas does not implement: a frontend, a proposal-discovery UI,
notification/alerting for delegators, an on-chain reputation leaderboard
beyond the raw stored counters, or a governance execution layer (actually
disbursing a grant, upgrading a parameter, etc. once a proposal "passes").
Those all belong in a Projects-category submission built *on top of* this
primitive, not inside it.
