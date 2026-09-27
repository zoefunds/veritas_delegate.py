# Veritas Review — Validator Verdict Agreement Fix

## Status

Fixed, tested, documented, and pushed to `origin/main`.

Latest commit: `fe31c55 Fix validator verdict agreement and refresh deployment docs`.

## Issue addressed

The validator previously accepted adjacent numeric alignment buckets whenever
their distance was at most one. That allowed adjacent buckets to pass even
when they produced different consequential outcomes:

| Bucket | Verdict | Effect |
|---:|---|---|
| 0 | `MISALIGNED` | Integrity decreases; repeated results can slash. |
| 1 | `MISALIGNED` | Integrity decreases; repeated results can slash. |
| 2 | `INCONCLUSIVE` | Neutral integrity treatment. |
| 3 | `ALIGNED` | Integrity increases. |
| 4 | `ALIGNED` | Integrity increases. |

The unsafe boundary pairs were `1↔2` (`MISALIGNED`/`INCONCLUSIVE`) and
`2↔3` (`INCONCLUSIVE`/`ALIGNED`).

## Fix implemented

`contracts/veritas_delegate.py` now uses `_buckets_preserve_verdict()`.
The validator must satisfy both conditions: bucket distance is no greater
than one, and both buckets map to the same consequential verdict.

Therefore `0↔1` remains valid for `MISALIGNED`, and `3↔4` remains valid for
`ALIGNED`, while `1↔2` and `2↔3` are rejected.

## Regression tests

Added `tests/test_alignment_consensus.py` covering:

| Leader | Validator | Expected |
|---:|---:|---|
| 0 | 1 | Accept |
| 3 | 4 | Accept |
| 1 | 2 | Reject |
| 2 | 3 | Reject |
| 0 | 2 | Reject |
| 2 | 4 | Reject |

Full suite result: **20 passed**. `git diff --check` also passes.

## Latest deployed contract

Latest verified GenLayer StudioNet address:

`0xdb18abE502829D0Ff2FE5856ab1EB3BC6a5a5783`

Initial read-only inspection confirmed a fresh, unpaused instance with zero
delegates, proposals, votes, and slash events.

## Real StudioNet verification

The live smoke test was run against the latest address with a funded test
account and real GenLayer validator/LLM consensus. It exercised:

1. `register_delegate` with a 1 GEN bond.
2. `delegate_stake` with 2 GEN.
3. `create_proposal` using live GenLayer documentation as evidence.
4. `cast_vote` with a detailed, falsifiable rationale.
5. `resolve_vote_outcome` using live web fetching and validator consensus.

All transactions reached accepted majority consensus. The resolved live state
was `alignment_bucket: 2`, `outcome_verdict: INCONCLUSIVE`, `resolved: true`,
`integrity_score: 7500`, `votes_resolved: 1`, `votes_inconclusive: 1`, and
`total_slash_events: 0`.

This verifies that the deployed contract supports the complete accountability
flow and preserves neutral handling for an inconclusive result.

## Documentation and tooling updates

- README and design notes now use the latest deployment address.
- Documentation describes verdict-preserving, not unrestricted,
  adjacent-bucket tolerance.
- `scripts/live_smoke_test.mjs` targets the latest deployment.
- `scripts/resolve_existing_vote.mjs` no longer contains historical proposal
  or delegate IDs; it requires explicit environment variables.
- Documentation now reports 20 total tests, including the boundary matrix.

## Files changed

- `contracts/veritas_delegate.py` — fixed validator agreement logic.
- `tests/test_alignment_consensus.py` — added boundary regression tests.
- `README.md` — refreshed behavior, deployment, tests, and live results.
- `docs/DESIGN.md` — refreshed design rationale and verification report.
- `scripts/live_smoke_test.mjs` — updated deployment target.
- `scripts/resolve_existing_vote.mjs` — removed stale hardcoded IDs.

## Verification boundary

The local regression matrix definitively tests the exact `1↔2` and `2↔3`
cases. The live smoke test verifies the deployed contract's operational path
with real validators and LLMs.

GenLayer does not expose an external input that lets a caller force arbitrary
leader and validator LLM scores on a deployed contract. Consequently, the
live test cannot force those exact bucket pairs; deterministic source tests
cover them instead.

Future deployments should retain the deployment transaction and matching
source commit so the deployed artifact can be independently tied to this
tested implementation.

## Conclusion

The reported validator-verdict vulnerability is fixed in source, covered by
explicit regression tests, exercised through the latest real StudioNet
deployment, documented, committed, and pushed.
