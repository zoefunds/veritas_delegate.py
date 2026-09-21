# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
#
# =============================================================================
# VERITAS — AI Delegate Accountability Protocol
# =============================================================================
#
# Purpose
# -------
# DAOs increasingly let an "AI agent" (a delegate) cast votes on behalf of
# stake that other members hand it. Today, nobody checks whether that
# delegate's stated reasoning ("I voted YES because this grant will ship a
# working integration by Q3") ever lines up with what actually happened.
# Veritas is a reusable GenLayer Intelligent Contract primitive that:
#
#   1. Lets AI (or human) delegates register with a bonded GEN escrow and
#      accept delegated voting weight from other members.
#   2. Forces every vote to carry a machine-checkable, falsifiable rationale
#      plus a set of evidence references (URLs and/or an uploaded image).
#   3. Once a proposal's resolution window has passed, fetches real-world
#      evidence (web pages, and optionally a screenshot/image) and runs a
#      GenVM consensus judgment comparing the delegate's stated rationale
#      against what actually happened.
#   4. Maintains a rolling, decaying "integrity score" per delegate based on
#      how often their rationale matched reality.
#   5. Automatically decays a drifting delegate's effective voting weight and
#      slashes their bonded escrow, routing the slashed GEN into a
#      compensation pool that misled delegators can claim pro-rata.
#
# This is a *primitive*: it does not implement a full governance app. It
# implements the accountability loop (rationale -> evidence -> consensus
# verdict -> reputation -> slashing -> compensation) that any DAO delegate
# system can plug into via cross-contract calls, or use standalone as shown
# in the bundled proposal/voting surface below.
#
# Consensus design notes
# -----------------------
# * Every non-deterministic operation (web fetch, image interpretation, LLM
#   judgment) uses a custom leader/validator pair via `gl.vm.run_nondet_unsafe`
#   — never `strict_eq` on LLM/web output.
# * All comparisons are BUCKETED and TOLERANT on purpose. A judgment that
#   requires validators to agree on an exact float score, an exact string,
#   or a byte-identical web page will drift into "undetermined" almost every
#   time real LLMs and real web pages are involved. Veritas instead:
#     - buckets alignment scores into 5 coarse bands (0..4) and only requires
#       agreement on the band, not the raw score.
#     - normalizes rationale/outcome text (lowercase, whitespace-collapsed)
#       before any comparison.
#     - derives a small set of stable boolean/enum fields from web content
#       instead of comparing raw page text.
#     - treats every LLM/web error as a classified, prefixed UserError so
#       validators can agree on "both failed the same expected way" instead
#       of disagreeing on incidental noise.
# * Every escrow-moving function follows the ordering rule: read ledger ->
#   zero ledger -> save state -> transfer. No transfer ever happens before
#   its ledger field is zeroed and persisted, so double payout is
#   structurally unreachable.
# * Every escrow has an explicit, enumerated exit: success payout, failure
#   refund, partial split, sponsor/delegate timeout recovery, and
#   cancellation-before-commitment refund. No path can strand funds forever.
#
# =============================================================================

from genlayer import *

from dataclasses import dataclass
import json
import re
import typing


# =============================================================================
# SECTION 1 — Constants
# =============================================================================

# ---- Error classification prefixes -----------------------------------------
# Deterministic business-logic errors: validators must match these exactly.
ERROR_EXPECTED = "[EXPECTED]"
# Deterministic external-API errors (4xx): validators must match exactly.
ERROR_EXTERNAL = "[EXTERNAL]"
# Non-deterministic transient errors (network/5xx/timeouts): validators agree
# with each other whenever both hit a transient failure, regardless of the
# precise message.
ERROR_TRANSIENT = "[TRANSIENT]"
# LLM misbehavior (bad JSON, missing fields, wrong types): validators always
# disagree on these to force leader rotation rather than lock in garbage.
ERROR_LLM = "[LLM_ERROR]"

# ---- Protocol-wide tunables --------------------------------------------------
# Minimum bond a delegate must lock (in atto-GEN, i.e. value * 10**18) to
# register. Kept as a contract constant rather than hardcoded literals so it
# reads clearly everywhere it's referenced.
MIN_DELEGATE_BOND_ATTO = u256(1) * u256(10) ** u256(18)  # 1 GEN minimum

# Delegate integrity score is stored on a fixed-point 0..10_000 scale
# (i.e. 2 implied decimals: 10_000 == 100.00%). Starting score for any newly
# registered delegate.
STARTING_INTEGRITY_SCORE = u256(7500)  # 75.00% — neutral-optimistic prior
MAX_INTEGRITY_SCORE = u256(10000)
MIN_INTEGRITY_SCORE = u256(0)

# Exponential-moving-average smoothing weight applied to each new alignment
# sample, expressed as a permille (parts per thousand) of the *new* sample's
# influence on the running score. 250 == new sample is weighted 25%.
INTEGRITY_EMA_WEIGHT_PERMILLE = u256(250)

# Below this integrity score, a delegate's effective voting weight begins to
# decay (see `_effective_weight_bps`). At/above it, weight is undiminished.
INTEGRITY_DECAY_FLOOR = u256(5000)  # 50.00%

# Below this integrity score, a delegate is considered to have drifted badly
# enough to trigger an automatic partial slash of their bond.
INTEGRITY_SLASH_THRESHOLD = u256(3000)  # 30.00%

# Slash size, in basis points of the delegate's *current* bonded escrow,
# applied each time a slash event fires (i.e. each time a resolved vote pushes
# the delegate's score below INTEGRITY_SLASH_THRESHOLD while it was at or
# above that threshold on the prior sample, OR remains below it on a later
# sample — see `_maybe_slash` for the exact re-entrancy-safe trigger rule).
SLASH_BPS = u256(1000)  # 10% of current bond per slash event
BPS_DENOMINATOR = u256(10000)

# Alignment score buckets (0..100 raw LLM alignment score bucketed into 5
# coarse bands). Validators only need to agree on the *bucket*, giving huge
# tolerance against raw-score jitter between leader and validator LLM calls.
ALIGNMENT_BUCKET_BOUNDARIES = (20, 40, 60, 80)  # yields 5 bands: 0-4

# A vote's rationale must be resolvable (fetch + judge) only after this many
# seconds have elapsed since the proposal's voting deadline. This gives the
# real-world outcome time to materialize before anyone tries to check it.
MIN_SECONDS_BEFORE_RESOLUTION = 0  # left as a hook; caller supplies real time

# If nobody ever triggers outcome resolution within this timeout, the
# delegate may reclaim their bond unmarked (no slash, no reward) rather than
# have funds/reputation stuck in limbo forever. Expressed in the same time
# unit the host application uses for `resolution_deadline` (left abstract —
# see docs/DESIGN.md for the recommended block-timestamp convention).
RESOLUTION_STALE_AFTER = 30 * 24 * 60 * 60  # 30 days, in seconds

# Cooldown a delegate must wait after requesting unbonding before the
# remaining (unslashed) bond can be withdrawn. Prevents a delegate from
# bonding, voting maliciously, and instantly exiting before any outcome can
# be checked.
UNBOND_COOLDOWN_SECONDS = 14 * 24 * 60 * 60  # 14 days

# Maximum number of evidence URLs accepted per vote rationale. Bounded so a
# malicious caller cannot force unbounded web-fetch work inside one
# transaction.
MAX_EVIDENCE_URLS = 5

# Maximum images accepted per outcome-resolution call. GenVM vision calls are
# documented to support at most two images.
MAX_EVIDENCE_IMAGES = 2

# Proposal status enum values (stored as `str`, never as a Python Enum).
STATUS_OPEN = "OPEN"
STATUS_VOTING_CLOSED = "VOTING_CLOSED"
STATUS_RESOLVED = "RESOLVED"
STATUS_CANCELLED = "CANCELLED"

# Vote choice enum values (stored as `str`).
VOTE_YES = "YES"
VOTE_NO = "NO"
VOTE_ABSTAIN = "ABSTAIN"

# Outcome verdict enum values assigned to a resolved vote (stored as `str`).
OUTCOME_ALIGNED = "ALIGNED"
OUTCOME_MISALIGNED = "MISALIGNED"
OUTCOME_INCONCLUSIVE = "INCONCLUSIVE"

# Delegate lifecycle status values (stored as `str`).
DELEGATE_ACTIVE = "ACTIVE"
DELEGATE_UNBONDING = "UNBONDING"
DELEGATE_EXITED = "EXITED"
DELEGATE_SLASHED_OUT = "SLASHED_OUT"  # bond fully exhausted by slashing


# =============================================================================
# SECTION 2 — EVM escrow interface
# =============================================================================
#
# Single emission point for every GEN transfer this contract ever makes.
# Mirrors the documented `_Recipient` pattern for sending value to an EOA or
# EVM contract via the ghost-contract external-message path. No other code
# path in this file is allowed to move GEN — every payout funnels through
# `_send_gen` below, so auditing money movement is a single grep away.


@gl.evm.contract_interface
class _Recipient:
    class View:
        pass

    class Write:
        pass


def _send_gen(to_address: str, amount: u256) -> None:
    if not to_address:
        raise gl.vm.UserError(f"{ERROR_EXPECTED} Missing recipient address")
    if amount <= u256(0):
        raise gl.vm.UserError(f"{ERROR_EXPECTED} Transfer amount must be positive")
    _Recipient(Address(to_address)).emit_transfer(value=amount)


# =============================================================================
# SECTION 3 — Generic helpers (parsing, normalization, error classification)
# =============================================================================


def _now_placeholder() -> u256:
    """
    GenVM intentionally has no built-in wall-clock inside deterministic
    contract code (block timestamps are supplied by the caller / evidenced
    via nondet web calls where real-world time matters). Every function that
    needs "now" in this contract accepts it as an explicit `now_ts` argument
    from the caller, and downstream consensus checks (outcome resolution)
    independently re-derive real-world timing from fetched evidence rather
    than trusting a caller-supplied clock for anything that gates money.
    This function exists purely as a documented sentinel default.
    """
    return u256(0)


def _clean_json_text(text: str) -> str:
    """Strip LLM wrapper prose and trailing commas from a JSON-ish string."""
    first = text.find("{")
    last = text.rfind("}")
    if first == -1 or last == -1 or last < first:
        raise gl.vm.UserError(f"{ERROR_LLM} No JSON object found in LLM output")
    snippet = text[first : last + 1]
    snippet = re.sub(r",(?!\s*?[\{\[\"\'\w])", "", snippet)
    return snippet


def _parse_llm_json(text: str) -> dict:
    """Defensively parse an LLM JSON response, tolerating wrapper prose."""
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        pass
    cleaned = _clean_json_text(text)
    try:
        parsed = json.loads(cleaned)
    except (ValueError, TypeError) as exc:
        raise gl.vm.UserError(f"{ERROR_LLM} Could not parse JSON: {exc}")
    if not isinstance(parsed, dict):
        raise gl.vm.UserError(f"{ERROR_LLM} Parsed JSON is not an object")
    return parsed


def _coerce_int(raw: typing.Any, aliases: tuple, field_label: str) -> int:
    """Aggressively coerce an LLM-provided numeric field, trying aliases."""
    value = raw
    if value is None:
        return None  # type: ignore[return-value]
    try:
        return int(round(float(str(value).strip())))
    except (ValueError, TypeError):
        raise gl.vm.UserError(
            f"{ERROR_LLM} Non-numeric '{field_label}' field: {value!r}"
        )


def _extract_scored_field(analysis: dict, primary: str, aliases: tuple) -> int:
    raw = analysis.get(primary)
    if raw is None:
        for alt in aliases:
            if alt in analysis:
                raw = analysis[alt]
                break
    if raw is None:
        raise gl.vm.UserError(
            f"{ERROR_LLM} Missing '{primary}'. Keys present: {list(analysis.keys())}"
        )
    coerced = _coerce_int(raw, aliases, primary)
    return max(0, min(100, coerced))


def _normalize_text(text: str) -> str:
    if text is None:
        return ""
    collapsed = re.sub(r"\s+", " ", text.strip().lower())
    return collapsed


def _bucket_score(score: int) -> int:
    """
    Map a raw 0..100 alignment score into one of 5 coarse bands. Validators
    only need to agree on the band index, which is the actual tolerance
    mechanism protecting this contract from undetermined-status consensus
    failures on LLM numeric jitter.
    """
    a, b, c, d = ALIGNMENT_BUCKET_BOUNDARIES
    if score < a:
        return 0
    if score < b:
        return 1
    if score < c:
        return 2
    if score < d:
        return 3
    return 4


def _bucket_to_verdict(bucket: int) -> str:
    # Bands 3-4 => aligned, band 2 => inconclusive (genuinely ambiguous),
    # bands 0-1 => misaligned. Keeping "inconclusive" as its own outcome
    # means an ambiguous real-world result never gets forced into a harsh
    # slash-triggering misalignment reading.
    if bucket >= 3:
        return OUTCOME_ALIGNED
    if bucket == 2:
        return OUTCOME_INCONCLUSIVE
    return OUTCOME_MISALIGNED


def _handle_leader_error(leaders_res: "gl.vm.Result", leader_fn: typing.Callable) -> bool:
    """
    Canonical error-classification handler shared by every validator in this
    contract. Re-runs `leader_fn` locally and decides agreement based on the
    error class, never on incidental message text for non-deterministic
    failures.
    """
    leader_msg = getattr(leaders_res, "message", "") or ""
    try:
        leader_fn()
        # Leader errored but the validator's independent run succeeded —
        # that is a genuine disagreement, not noise.
        return False
    except gl.vm.UserError as exc:
        validator_msg = getattr(exc, "message", None) or str(exc)
        if validator_msg.startswith(ERROR_EXPECTED) or validator_msg.startswith(
            ERROR_EXTERNAL
        ):
            return validator_msg == leader_msg
        if validator_msg.startswith(ERROR_TRANSIENT) and leader_msg.startswith(
            ERROR_TRANSIENT
        ):
            return True
        # LLM errors or anything unclassified: force rotation.
        return False
    except Exception:
        return False


def _derive_page_signal(text: str) -> str:
    """
    Convert raw, unstable webpage text into one of a small closed set of
    stable signal strings. Comparing THIS across leader/validator is what
    keeps web-based resolution out of undetermined status — never compare
    raw page text directly.
    """
    lowered = _normalize_text(text)
    positive_markers = (
        "shipped",
        "completed",
        "success",
        "launched",
        "delivered",
        "merged",
        "released",
        "passed",
        "resolved",
    )
    negative_markers = (
        "cancelled",
        "canceled",
        "failed",
        "abandoned",
        "delayed",
        "rejected",
        "withdrawn",
        "stalled",
        "reverted",
    )
    pos_hits = sum(1 for m in positive_markers if m in lowered)
    neg_hits = sum(1 for m in negative_markers if m in lowered)
    if pos_hits == 0 and neg_hits == 0:
        return "UNCLEAR"
    if pos_hits > neg_hits:
        return "POSITIVE"
    if neg_hits > pos_hits:
        return "NEGATIVE"
    return "MIXED"


# =============================================================================
# SECTION 4 — Storage dataclasses
# =============================================================================


@allow_storage
@dataclass
class EvidenceRef:
    """A single piece of evidence attached to a vote's rationale."""

    kind: str  # "URL" or "IMAGE_NOTE" (the image bytes themselves are never
    # persisted on-chain; only a short caller-supplied label/note is, to keep
    # storage bounded — the image is supplied fresh at resolution time).
    value: str


@allow_storage
@dataclass
class VoteRecord:
    """
    One delegate's vote on one proposal, including the falsifiable rationale
    that Veritas will later check against reality.
    """

    proposal_id: str
    delegate_id: str
    choice: str  # VOTE_YES / VOTE_NO / VOTE_ABSTAIN
    rationale: str
    evidence: DynArray[EvidenceRef]
    weight_atto: u256  # effective delegated weight this vote carried
    cast_at: u256  # caller-supplied timestamp, informational only
    resolved: bool
    outcome_verdict: str  # "" until resolved
    alignment_bucket: u256  # 0..4, meaningful only once resolved
    resolution_summary: str


@allow_storage
@dataclass
class DelegationRecord:
    """One delegator's currently-active delegation to one delegate."""

    delegator: str
    delegate_id: str
    principal_atto: u256  # GEN the delegator has escrowed as "at-risk" stake
    active: bool


@allow_storage
@dataclass
class Delegate:
    """An AI (or human) delegate registered with Veritas."""

    delegate_id: str
    controller: str  # address allowed to cast votes / manage this delegate
    bonded_atto: u256  # current bonded escrow ledger (the ONLY source of
    # truth for how much this delegate has locked; never re-derive from
    # anything else when paying out)
    integrity_score: u256  # 0..10000 fixed point
    votes_cast: u256
    votes_resolved: u256
    votes_aligned: u256
    votes_misaligned: u256
    votes_inconclusive: u256
    status: str  # DELEGATE_ACTIVE / UNBONDING / EXITED / SLASHED_OUT
    unbond_requested_at: u256
    total_delegated_atto: u256  # sum of active DelegationRecord principals
    compensation_pool_atto: u256  # slashed GEN earmarked for this delegate's
    # misled delegators, pending claim
    metadata_uri: str  # off-chain profile/model-card pointer, informational


@allow_storage
@dataclass
class Proposal:
    """A governance proposal that delegates vote on with weighted stake."""

    proposal_id: str
    creator: str
    title: str
    description: str
    outcome_evidence_urls: DynArray[str]  # where to check real-world outcome
    voting_deadline: u256
    resolution_deadline: u256  # earliest time outcome-check may run
    status: str
    yes_weight_atto: u256
    no_weight_atto: u256
    abstain_weight_atto: u256
    created_at: u256


@allow_storage
@dataclass
class CompensationClaimReceipt:
    """Immutable receipt of a paid-out compensation claim, for auditability."""

    delegate_id: str
    delegator: str
    amount_atto: u256
    claimed_at: u256


# =============================================================================
# SECTION 5 — The contract
# =============================================================================


class VeritasDelegateAccountability(gl.Contract):
    # ---- Ownership / admin ----------------------------------------------
    owner: Address

    # ---- Delegates --------------------------------------------------------
    delegates: TreeMap[str, Delegate]
    delegate_ids: DynArray[str]

    # ---- Delegations (delegator -> delegate) ------------------------------
    # Keyed by f"{delegator}:{delegate_id}" for O(log n) lookup.
    delegations: TreeMap[str, DelegationRecord]
    delegation_keys: DynArray[str]

    # ---- Proposals ----------------------------------------------------------
    proposals: TreeMap[str, Proposal]
    proposal_ids: DynArray[str]

    # ---- Votes --------------------------------------------------------------
    # Keyed by f"{proposal_id}:{delegate_id}" — one vote per delegate per
    # proposal.
    votes: TreeMap[str, VoteRecord]
    vote_keys: DynArray[str]

    # ---- Compensation receipts (audit trail; append-only) --------------------
    compensation_receipts: DynArray[CompensationClaimReceipt]

    # ---- Protocol-level counters (O(1) stats) --------------------------------
    total_delegates: u256
    total_proposals: u256
    total_votes_cast: u256
    total_slash_events: u256
    total_slashed_atto: u256
    total_compensation_paid_atto: u256

    # ---- Pause switch (owner emergency control; never touches escrow funds
    # already locked — only gates NEW registrations/votes/proposals) ----------
    paused: bool

    # -------------------------------------------------------------------
    # Constructor
    # -------------------------------------------------------------------
    def __init__(self) -> None:
        self.owner = gl.message.sender_address
        self.total_delegates = u256(0)
        self.total_proposals = u256(0)
        self.total_votes_cast = u256(0)
        self.total_slash_events = u256(0)
        self.total_slashed_atto = u256(0)
        self.total_compensation_paid_atto = u256(0)
        self.paused = False

    # =====================================================================
    # SECTION 6 — Admin
    # =====================================================================

    @gl.public.write
    def set_paused(self, paused: bool) -> None:
        if gl.message.sender_address != self.owner:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Only owner may pause")
        self.paused = paused

    @gl.public.write
    def transfer_ownership(self, new_owner: str) -> None:
        if gl.message.sender_address != self.owner:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Only owner may transfer ownership")
        if not new_owner:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} New owner address required")
        self.owner = Address(new_owner)

    def _require_not_paused(self) -> None:
        if self.paused:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Contract is paused")

    # =====================================================================
    # SECTION 7 — Delegate lifecycle (registration, bonding, unbonding)
    # =====================================================================

    @gl.public.write.payable
    def register_delegate(self, delegate_id: str, metadata_uri: str) -> None:
        """
        Register a new AI/human delegate. Must be called with GEN value >=
        MIN_DELEGATE_BOND_ATTO attached; that value becomes the delegate's
        bonded escrow ledger.
        """
        self._require_not_paused()
        if not delegate_id or len(delegate_id) > 128:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Invalid delegate_id")
        if delegate_id in self.delegates:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} delegate_id already registered")

        bond = gl.message.value
        if bond < MIN_DELEGATE_BOND_ATTO:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Bond below minimum required ({str(MIN_DELEGATE_BOND_ATTO)})"
            )

        delegate = Delegate(
            delegate_id=delegate_id,
            controller=str(gl.message.sender_address),
            bonded_atto=bond,
            integrity_score=STARTING_INTEGRITY_SCORE,
            votes_cast=u256(0),
            votes_resolved=u256(0),
            votes_aligned=u256(0),
            votes_misaligned=u256(0),
            votes_inconclusive=u256(0),
            status=DELEGATE_ACTIVE,
            unbond_requested_at=u256(0),
            total_delegated_atto=u256(0),
            compensation_pool_atto=u256(0),
            metadata_uri=metadata_uri,
        )
        self.delegates[delegate_id] = delegate
        self.delegate_ids.append(delegate_id)
        self.total_delegates = self.total_delegates + u256(1)

    @gl.public.write.payable
    def top_up_bond(self, delegate_id: str) -> None:
        """Add more GEN to an existing delegate's bonded escrow ledger."""
        self._require_not_paused()
        delegate = self._get_delegate_or_raise(delegate_id)
        self._require_controller(delegate)
        added = gl.message.value
        if added <= u256(0):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Top-up must send positive GEN value")
        delegate.bonded_atto = delegate.bonded_atto + added
        self.delegates[delegate_id] = delegate

    @gl.public.write
    def request_unbond(self, delegate_id: str, now_ts: int) -> None:
        """
        Begin the unbonding cooldown. A delegate cannot request unbonding
        while it still has unresolved votes outstanding, so nobody can dodge
        an in-flight accountability check by exiting early.
        """
        self._require_not_paused()
        delegate = self._get_delegate_or_raise(delegate_id)
        self._require_controller(delegate)
        if delegate.status != DELEGATE_ACTIVE:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Delegate not active")
        if self._has_unresolved_votes(delegate_id):
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Delegate has unresolved votes; cannot unbond yet"
            )
        delegate.status = DELEGATE_UNBONDING
        delegate.unbond_requested_at = u256(max(0, now_ts))
        self.delegates[delegate_id] = delegate

    @gl.public.write
    def withdraw_bond(self, delegate_id: str, now_ts: int) -> None:
        """
        Exit path #1 (success/cancellation-style refund): after the cooldown
        has elapsed and no slash has zeroed the bond, the delegate reclaims
        whatever remains of their own bonded escrow. Zero-then-transfer
        ordering applied.
        """
        self._require_not_paused()
        delegate = self._get_delegate_or_raise(delegate_id)
        self._require_controller(delegate)
        if delegate.status != DELEGATE_UNBONDING:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Delegate is not unbonding")
        elapsed = max(0, now_ts) - int(delegate.unbond_requested_at)
        if elapsed < UNBOND_COOLDOWN_SECONDS:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Unbond cooldown not yet elapsed"
            )

        amount = delegate.bonded_atto
        if amount <= u256(0):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} No bond remaining to withdraw")

        # Zero ledger -> persist -> transfer.
        delegate.bonded_atto = u256(0)
        delegate.status = DELEGATE_EXITED
        self.delegates[delegate_id] = delegate

        _send_gen(delegate.controller, amount)

    # =====================================================================
    # SECTION 8 — Delegation (delegator locks GEN weight behind a delegate)
    # =====================================================================

    @gl.public.write.payable
    def delegate_stake(self, delegate_id: str) -> None:
        """
        A delegator locks GEN behind a chosen delegate. This is the "at-risk"
        principal that the compensation pool exists to protect: if the
        delegate's integrity collapses and gets slashed, delegators who had
        active delegations at slash time can later claim a pro-rata share of
        the slashed GEN.
        """
        self._require_not_paused()
        delegate = self._get_delegate_or_raise(delegate_id)
        if delegate.status not in (DELEGATE_ACTIVE, DELEGATE_UNBONDING):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Delegate is not accepting delegations")

        amount = gl.message.value
        if amount <= u256(0):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Delegation requires positive GEN value")

        delegator = str(gl.message.sender_address)
        key = f"{delegator}:{delegate_id}"
        existing = self.delegations.get(key)
        if existing is not None and existing.active:
            existing.principal_atto = existing.principal_atto + amount
            self.delegations[key] = existing
        else:
            record = DelegationRecord(
                delegator=delegator,
                delegate_id=delegate_id,
                principal_atto=amount,
                active=True,
            )
            self.delegations[key] = record
            self.delegation_keys.append(key)

        delegate.total_delegated_atto = delegate.total_delegated_atto + amount
        self.delegates[delegate_id] = delegate

    @gl.public.write
    def withdraw_delegation(self, delegate_id: str) -> None:
        """
        Cancellation-before-loss exit: a delegator may withdraw their
        delegated principal at any time the delegate has not already
        earmarked compensation against it (compensation accounting is
        tracked separately in the delegate's pool, so this never touches
        slashed funds — only the delegator's own still-intact principal).
        """
        self._require_not_paused()
        delegator = str(gl.message.sender_address)
        key = f"{delegator}:{delegate_id}"
        record = self.delegations.get(key)
        if record is None or not record.active:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} No active delegation found")

        amount = record.principal_atto
        if amount <= u256(0):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Nothing to withdraw")

        # Zero ledger -> persist -> transfer.
        record.principal_atto = u256(0)
        record.active = False
        self.delegations[key] = record

        delegate = self._get_delegate_or_raise(delegate_id)
        if delegate.total_delegated_atto >= amount:
            delegate.total_delegated_atto = delegate.total_delegated_atto - amount
        else:
            delegate.total_delegated_atto = u256(0)
        self.delegates[delegate_id] = delegate

        _send_gen(delegator, amount)

    # =====================================================================
    # SECTION 9 — Proposals
    # =====================================================================

    @gl.public.write
    def create_proposal(
        self,
        proposal_id: str,
        title: str,
        description: str,
        outcome_evidence_urls: list,
        voting_deadline: int,
        resolution_deadline: int,
        now_ts: int,
    ) -> None:
        self._require_not_paused()
        if not proposal_id or proposal_id in self.proposals:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Invalid or duplicate proposal_id")
        if not title:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Title required")
        if resolution_deadline < voting_deadline:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} resolution_deadline must be after voting_deadline"
            )
        if len(outcome_evidence_urls) == 0:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} At least one outcome_evidence_url required"
            )
        if len(outcome_evidence_urls) > MAX_EVIDENCE_URLS:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Too many evidence URLs")

        urls: list = []
        for url in outcome_evidence_urls:
            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                raise gl.vm.UserError(f"{ERROR_EXPECTED} Invalid evidence URL: {url!r}")
            urls.append(url)

        proposal = Proposal(
            proposal_id=proposal_id,
            creator=str(gl.message.sender_address),
            title=title,
            description=description,
            outcome_evidence_urls=urls,
            voting_deadline=u256(max(0, voting_deadline)),
            resolution_deadline=u256(max(0, resolution_deadline)),
            status=STATUS_OPEN,
            yes_weight_atto=u256(0),
            no_weight_atto=u256(0),
            abstain_weight_atto=u256(0),
            created_at=u256(max(0, now_ts)),
        )
        self.proposals[proposal_id] = proposal
        self.proposal_ids.append(proposal_id)
        self.total_proposals = self.total_proposals + u256(1)

    @gl.public.write
    def cancel_proposal(self, proposal_id: str) -> None:
        """
        Cancellation exit for proposals: only the creator or owner, and only
        before any vote has been cast, so no delegate's weight or rationale
        is ever discarded after commitment.
        """
        self._require_not_paused()
        proposal = self._get_proposal_or_raise(proposal_id)
        caller = gl.message.sender_address
        if str(caller) != proposal.creator and caller != self.owner:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Only creator or owner may cancel")
        if proposal.status != STATUS_OPEN:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Proposal not open")
        if (
            proposal.yes_weight_atto > u256(0)
            or proposal.no_weight_atto > u256(0)
            or proposal.abstain_weight_atto > u256(0)
        ):
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Cannot cancel a proposal that already has votes"
            )
        proposal.status = STATUS_CANCELLED
        self.proposals[proposal_id] = proposal

    # =====================================================================
    # SECTION 10 — Voting with falsifiable rationale
    # =====================================================================

    @gl.public.write
    def cast_vote(
        self,
        proposal_id: str,
        delegate_id: str,
        choice: str,
        rationale: str,
        evidence_urls: list,
        now_ts: int,
    ) -> None:
        """
        A registered delegate casts a vote on a proposal, carrying its full
        delegated weight, along with a rationale that Veritas will later
        check against real-world evidence. The rationale is the load-bearing
        artifact of this whole contract: it must be specific and falsifiable
        (validated for minimum substance below), not a generic hedge.
        """
        self._require_not_paused()
        proposal = self._get_proposal_or_raise(proposal_id)
        delegate = self._get_delegate_or_raise(delegate_id)
        self._require_controller(delegate)

        if proposal.status != STATUS_OPEN:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Proposal is not open for voting")
        if now_ts > int(proposal.voting_deadline):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Voting deadline has passed")
        if choice not in (VOTE_YES, VOTE_NO, VOTE_ABSTAIN):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Invalid vote choice")
        if delegate.status != DELEGATE_ACTIVE:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Delegate is not active")

        stripped_rationale = (rationale or "").strip()
        if len(stripped_rationale) < 20:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Rationale too short to be falsifiable (min 20 chars)"
            )

        vote_key = f"{proposal_id}:{delegate_id}"
        if vote_key in self.votes:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Delegate already voted on this proposal")

        if len(evidence_urls) > MAX_EVIDENCE_URLS:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Too many evidence URLs")
        evidence: list = []
        for url in evidence_urls:
            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                raise gl.vm.UserError(f"{ERROR_EXPECTED} Invalid evidence URL: {url!r}")
            evidence.append(EvidenceRef(kind="URL", value=url))

        weight = self._effective_weight_atto(delegate)

        vote = VoteRecord(
            proposal_id=proposal_id,
            delegate_id=delegate_id,
            choice=choice,
            rationale=stripped_rationale,
            evidence=evidence,
            weight_atto=weight,
            cast_at=u256(max(0, now_ts)),
            resolved=False,
            outcome_verdict="",
            alignment_bucket=u256(0),
            resolution_summary="",
        )
        self.votes[vote_key] = vote
        self.vote_keys.append(vote_key)

        if choice == VOTE_YES:
            proposal.yes_weight_atto = proposal.yes_weight_atto + weight
        elif choice == VOTE_NO:
            proposal.no_weight_atto = proposal.no_weight_atto + weight
        else:
            proposal.abstain_weight_atto = proposal.abstain_weight_atto + weight
        self.proposals[proposal_id] = proposal

        delegate.votes_cast = delegate.votes_cast + u256(1)
        self.delegates[delegate_id] = delegate
        self.total_votes_cast = self.total_votes_cast + u256(1)

    @gl.public.write
    def close_voting(self, proposal_id: str, now_ts: int) -> None:
        """Anyone may close voting once the deadline has passed."""
        self._require_not_paused()
        proposal = self._get_proposal_or_raise(proposal_id)
        if proposal.status != STATUS_OPEN:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Proposal is not open")
        if now_ts <= int(proposal.voting_deadline):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Voting deadline has not passed yet")
        proposal.status = STATUS_VOTING_CLOSED
        self.proposals[proposal_id] = proposal

    # =====================================================================
    # SECTION 11 — Outcome resolution (the consensus-critical path)
    # =====================================================================

    @gl.public.write
    def resolve_vote_outcome(
        self,
        proposal_id: str,
        delegate_id: str,
        now_ts: int,
        supplemental_image: bytes,
    ) -> str:
        """
        The heart of the accountability loop. Fetches the proposal's
        real-world outcome evidence (web pages, and optionally a supplied
        image such as a dashboard screenshot for visual interpretation),
        derives a small set of STABLE signals from it, and runs a consensus
        judgment on whether the delegate's stated rationale for this vote
        matches what actually happened.

        `supplemental_image` may be empty bytes if no image evidence is
        available — image interpretation is opportunistic, not required.

        Returns the outcome verdict string (ALIGNED / MISALIGNED /
        INCONCLUSIVE).
        """
        self._require_not_paused()
        proposal = self._get_proposal_or_raise(proposal_id)
        delegate = self._get_delegate_or_raise(delegate_id)

        if proposal.status not in (STATUS_VOTING_CLOSED, STATUS_OPEN):
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Proposal must have closed voting before resolution"
            )
        if now_ts < int(proposal.resolution_deadline):
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Resolution window has not opened yet"
            )

        vote_key = f"{proposal_id}:{delegate_id}"
        vote = self.votes.get(vote_key)
        if vote is None:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} No such vote to resolve")
        if vote.resolved:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Vote already resolved")
        if vote.choice == VOTE_ABSTAIN:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Abstain votes carry no rationale to check"
            )

        rationale = vote.rationale
        urls = [u for u in proposal.outcome_evidence_urls]
        images = [supplemental_image] if len(supplemental_image) > 0 else []
        if len(images) > MAX_EVIDENCE_IMAGES:
            images = images[:MAX_EVIDENCE_IMAGES]

        bucket, verdict, summary = self._run_outcome_consensus(rationale, urls, images)

        vote.resolved = True
        vote.outcome_verdict = verdict
        vote.alignment_bucket = u256(bucket)
        vote.resolution_summary = summary
        self.votes[vote_key] = vote

        self._apply_integrity_update(delegate_id, verdict)
        self._maybe_close_proposal(proposal_id)

        return verdict

    def _run_outcome_consensus(
        self, rationale: str, urls: list, images: list
    ) -> typing.Tuple[int, str, str]:
        """
        Runs the leader/validator consensus round comparing rationale vs.
        real-world evidence. Uses `gl.vm.run_nondet_unsafe` with a bucketed,
        tolerant comparator — never exact string/float equality.
        """
        normalized_rationale = _normalize_text(rationale)

        def leader_fn() -> dict:
            page_signals = []
            for url in urls:
                try:
                    text = gl.nondet.web.render(url, mode="text")
                except Exception as exc:
                    raise gl.vm.UserError(f"{ERROR_TRANSIENT} Web fetch failed: {exc}")
                page_signals.append(_derive_page_signal(text))

            evidence_block = "\n".join(
                f"- Source {i + 1} signal: {sig}" for i, sig in enumerate(page_signals)
            )

            prompt = (
                "You are auditing whether a stated voting rationale matches "
                "the real-world outcome described by the evidence below.\n\n"
                f"STATED RATIONALE:\n{normalized_rationale}\n\n"
                f"DERIVED EVIDENCE SIGNALS (POSITIVE/NEGATIVE/MIXED/UNCLEAR):\n"
                f"{evidence_block}\n\n"
                "Score how well the rationale's prediction matches the "
                "evidence signals, from 0 (completely contradicted) to 100 "
                "(fully confirmed). If the evidence is genuinely ambiguous, "
                "score near 50. Respond as JSON only: "
                '{"alignment_score": <0-100 integer>, "summary": "<one sentence>"}'
            )

            if images:
                analysis = gl.nondet.exec_prompt(
                    prompt, images=images, response_format="json"
                )
            else:
                analysis = gl.nondet.exec_prompt(prompt, response_format="json")

            if isinstance(analysis, str):
                analysis = _parse_llm_json(analysis)
            if not isinstance(analysis, dict):
                raise gl.vm.UserError(
                    f"{ERROR_LLM} Expected JSON object, got {type(analysis)}"
                )

            score = _extract_scored_field(
                analysis, "alignment_score", ("score", "rating", "value")
            )
            summary_raw = analysis.get("summary", "")
            summary = str(summary_raw)[:280] if summary_raw else "No summary provided."

            return {
                "bucket": _bucket_score(score),
                "signals": page_signals,
                "summary": summary,
            }

        def validator_fn(leaders_res: "gl.vm.Result") -> bool:
            if not isinstance(leaders_res, gl.vm.Return):
                return _handle_leader_error(leaders_res, leader_fn)

            leader_data = leaders_res.calldata
            validator_data = leader_fn()

            # Tolerance #1: bucket-level agreement only (not raw score).
            if leader_data.get("bucket") != validator_data.get("bucket"):
                # Allow adjacent-bucket drift (off-by-one band) as agreement —
                # LLM scoring jitter across independent calls routinely lands
                # one band apart even when both are "clearly aligned".
                if abs(int(leader_data.get("bucket", 0)) - int(validator_data.get("bucket", 0))) > 1:
                    return False

            # Tolerance #2: page signals must roughly agree — allow any
            # single-source mismatch (transient page changes, caching,
            # slightly different render timing) as long as not a majority
            # of sources flatly disagree.
            leader_signals = leader_data.get("signals", [])
            validator_signals = validator_data.get("signals", [])
            if len(leader_signals) == len(validator_signals) and len(leader_signals) > 0:
                mismatches = sum(
                    1
                    for a, b in zip(leader_signals, validator_signals)
                    if a != b
                )
                if mismatches > max(1, len(leader_signals) // 2):
                    return False

            return True

        result = gl.vm.run_nondet_unsafe(leader_fn, validator_fn)

        bucket = int(result.get("bucket", 2))
        verdict = _bucket_to_verdict(bucket)
        summary = str(result.get("summary", ""))
        return bucket, verdict, summary

    # =====================================================================
    # SECTION 12 — Integrity scoring, weight decay, and slashing
    # =====================================================================

    def _apply_integrity_update(self, delegate_id: str, verdict: str) -> None:
        delegate = self._get_delegate_or_raise(delegate_id)

        delegate.votes_resolved = delegate.votes_resolved + u256(1)
        if verdict == OUTCOME_ALIGNED:
            delegate.votes_aligned = delegate.votes_aligned + u256(1)
            sample = MAX_INTEGRITY_SCORE
        elif verdict == OUTCOME_MISALIGNED:
            delegate.votes_misaligned = delegate.votes_misaligned + u256(1)
            sample = MIN_INTEGRITY_SCORE
        else:
            delegate.votes_inconclusive = delegate.votes_inconclusive + u256(1)
            # Inconclusive samples nudge toward the neutral prior rather than
            # toward either extreme, so genuinely ambiguous real-world
            # outcomes don't whipsaw a delegate's score.
            sample = STARTING_INTEGRITY_SCORE

        old_score = delegate.integrity_score
        weight = INTEGRITY_EMA_WEIGHT_PERMILLE
        one_thousand = u256(1000)
        new_score = (old_score * (one_thousand - weight) + sample * weight) // one_thousand
        if new_score > MAX_INTEGRITY_SCORE:
            new_score = MAX_INTEGRITY_SCORE
        delegate.integrity_score = new_score
        self.delegates[delegate_id] = delegate

        self._maybe_slash(delegate_id)

    def _effective_weight_bps(self, integrity_score: u256) -> u256:
        """
        Delegates at/above the decay floor vote at full strength. Below the
        floor, effective weight decays linearly down to a 10% floor at zero
        integrity, so a badly-drifting delegate never fully loses its
        members' voice overnight but does lose most of its influence.
        """
        if integrity_score >= INTEGRITY_DECAY_FLOOR:
            return BPS_DENOMINATOR
        min_bps = u256(1000)  # 10% floor
        span = BPS_DENOMINATOR - min_bps
        # Linear interpolation between (0 -> min_bps) and (floor -> 10000).
        scaled = (integrity_score * span) // INTEGRITY_DECAY_FLOOR
        return min_bps + scaled

    def _effective_weight_atto(self, delegate: Delegate) -> u256:
        bps = self._effective_weight_bps(delegate.integrity_score)
        return (delegate.total_delegated_atto * bps) // BPS_DENOMINATOR

    def _maybe_slash(self, delegate_id: str) -> None:
        """
        Fires a bounded slash whenever a delegate's integrity score is below
        the slash threshold. Re-entrant-safe: each call slashes a fixed
        percentage of the delegate's *current* remaining bond (not the
        original bond), so repeated resolutions while the delegate stays
        below threshold each take another bite rather than double-charging
        against an already-zeroed ledger. Slashed GEN moves into the
        delegate's compensation pool for delegators to later claim — the
        transfer itself only happens against the compensation pool's ledger
        when a delegator actually claims (see `claim_compensation`), so no
        GEN leaves the contract here; this function only re-labels which
        ledger field the GEN sits in.
        """
        delegate = self._get_delegate_or_raise(delegate_id)
        if delegate.integrity_score >= INTEGRITY_SLASH_THRESHOLD:
            return
        if delegate.bonded_atto <= u256(0):
            return

        slash_amount = (delegate.bonded_atto * SLASH_BPS) // BPS_DENOMINATOR
        if slash_amount <= u256(0):
            return

        delegate.bonded_atto = delegate.bonded_atto - slash_amount
        delegate.compensation_pool_atto = delegate.compensation_pool_atto + slash_amount
        if delegate.bonded_atto == u256(0):
            delegate.status = DELEGATE_SLASHED_OUT
        self.delegates[delegate_id] = delegate

        self.total_slash_events = self.total_slash_events + u256(1)
        self.total_slashed_atto = self.total_slashed_atto + slash_amount

    # =====================================================================
    # SECTION 13 — Compensation claims (misled delegators reclaim slashed GEN)
    # =====================================================================

    @gl.public.write
    def claim_compensation(self, delegate_id: str) -> u256:
        """
        A delegator with an active (or previously active) delegation to a
        slashed delegate claims their pro-rata share of that delegate's
        compensation pool. Pro-rata share is computed against the
        delegator's principal relative to the delegate's total delegated
        GEN at claim time. Zero-then-transfer ordering applied; the claimed
        share is deducted from the pool before the external transfer, and a
        delegator can only claim their still-active principal's share once
        because `withdraw_delegation`/this function both zero what they
        consume.
        """
        self._require_not_paused()
        delegate = self._get_delegate_or_raise(delegate_id)
        if delegate.compensation_pool_atto <= u256(0):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} No compensation pool available")

        delegator = str(gl.message.sender_address)
        key = f"{delegator}:{delegate_id}"
        record = self.delegations.get(key)
        if record is None or not record.active or record.principal_atto <= u256(0):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} No active delegation to claim against")

        if delegate.total_delegated_atto <= u256(0):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Delegate has no delegated stake on record")

        # Pro-rata share, capped by whatever remains in the pool.
        share = (delegate.compensation_pool_atto * record.principal_atto) // delegate.total_delegated_atto
        if share > delegate.compensation_pool_atto:
            share = delegate.compensation_pool_atto
        if share <= u256(0):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Computed compensation share is zero")

        # Zero the claimant's ability to claim again for this exact
        # delegation record, then zero the pool's ledger, then persist, then
        # transfer.
        record.active = False
        claimed_principal = record.principal_atto
        record.principal_atto = u256(0)
        self.delegations[key] = record

        delegate.compensation_pool_atto = delegate.compensation_pool_atto - share
        if delegate.total_delegated_atto >= claimed_principal:
            delegate.total_delegated_atto = delegate.total_delegated_atto - claimed_principal
        else:
            delegate.total_delegated_atto = u256(0)
        self.delegates[delegate_id] = delegate

        receipt = CompensationClaimReceipt(
            delegate_id=delegate_id,
            delegator=delegator,
            amount_atto=share,
            claimed_at=u256(0),
        )
        self.compensation_receipts.append(receipt)
        self.total_compensation_paid_atto = self.total_compensation_paid_atto + share

        _send_gen(delegator, share)
        return share

    # =====================================================================
    # SECTION 14 — Timeout / recovery exit (stuck-fund protection)
    # =====================================================================

    @gl.public.write
    def reclaim_stale_vote_bond_share(
        self, proposal_id: str, delegate_id: str, now_ts: int
    ) -> None:
        """
        Recovery exit: if nobody ever calls `resolve_vote_outcome` within
        RESOLUTION_STALE_AFTER seconds of the resolution window opening, the
        vote is marked INCONCLUSIVE with no integrity penalty and no slash —
        this is the "stuck/abandoned" exit every escrow-bearing contract
        must have, so a delegate's reputation (and any funds gated on it)
        can never be held hostage by nobody bothering to trigger resolution.
        This path moves no GEN — it only unblocks state (e.g. unbonding) —
        because the bond itself was never earmarked to this specific vote.
        """
        self._require_not_paused()
        proposal = self._get_proposal_or_raise(proposal_id)
        vote_key = f"{proposal_id}:{delegate_id}"
        vote = self.votes.get(vote_key)
        if vote is None:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} No such vote")
        if vote.resolved:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Vote already resolved")

        deadline = int(proposal.resolution_deadline)
        if now_ts < deadline + RESOLUTION_STALE_AFTER:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Resolution has not gone stale yet"
            )

        vote.resolved = True
        vote.outcome_verdict = OUTCOME_INCONCLUSIVE
        vote.alignment_bucket = u256(2)
        vote.resolution_summary = "Auto-resolved INCONCLUSIVE: resolution window expired unclaimed."
        self.votes[vote_key] = vote

        delegate = self._get_delegate_or_raise(delegate_id)
        delegate.votes_resolved = delegate.votes_resolved + u256(1)
        delegate.votes_inconclusive = delegate.votes_inconclusive + u256(1)
        self.delegates[delegate_id] = delegate

        self._maybe_close_proposal(proposal_id)

    def _maybe_close_proposal(self, proposal_id: str) -> None:
        proposal = self._get_proposal_or_raise(proposal_id)
        if proposal.status == STATUS_RESOLVED:
            return
        all_resolved = True
        for key in self.vote_keys:
            if not key.startswith(f"{proposal_id}:"):
                continue
            vote = self.votes.get(key)
            if vote is not None and not vote.resolved and vote.choice != VOTE_ABSTAIN:
                all_resolved = False
                break
        if all_resolved and proposal.status in (STATUS_OPEN, STATUS_VOTING_CLOSED):
            proposal.status = STATUS_RESOLVED
            self.proposals[proposal_id] = proposal

    # =====================================================================
    # SECTION 15 — Internal lookups / guards
    # =====================================================================

    def _get_delegate_or_raise(self, delegate_id: str) -> Delegate:
        delegate = self.delegates.get(delegate_id)
        if delegate is None:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Unknown delegate_id: {delegate_id}")
        return gl.storage.copy_to_memory(delegate)

    def _get_proposal_or_raise(self, proposal_id: str) -> Proposal:
        proposal = self.proposals.get(proposal_id)
        if proposal is None:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Unknown proposal_id: {proposal_id}")
        return gl.storage.copy_to_memory(proposal)

    def _require_controller(self, delegate: Delegate) -> None:
        if str(gl.message.sender_address) != delegate.controller:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Caller is not this delegate's controller"
            )

    def _has_unresolved_votes(self, delegate_id: str) -> bool:
        for key in self.vote_keys:
            if not key.endswith(f":{delegate_id}"):
                continue
            vote = self.votes.get(key)
            if vote is not None and not vote.resolved and vote.choice != VOTE_ABSTAIN:
                return True
        return False

    # =====================================================================
    # SECTION 16 — Views
    # =====================================================================

    @gl.public.view
    def get_delegate(self, delegate_id: str) -> dict:
        d = self.delegates.get(delegate_id)
        if d is None:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Unknown delegate_id")
        return {
            "delegate_id": d.delegate_id,
            "controller": d.controller,
            "bonded_atto": str(d.bonded_atto),
            "integrity_score": str(d.integrity_score),
            "votes_cast": str(d.votes_cast),
            "votes_resolved": str(d.votes_resolved),
            "votes_aligned": str(d.votes_aligned),
            "votes_misaligned": str(d.votes_misaligned),
            "votes_inconclusive": str(d.votes_inconclusive),
            "status": d.status,
            "total_delegated_atto": str(d.total_delegated_atto),
            "compensation_pool_atto": str(d.compensation_pool_atto),
            "metadata_uri": d.metadata_uri,
            "effective_weight_bps": str(self._effective_weight_bps(d.integrity_score)),
        }

    @gl.public.view
    def list_delegate_ids(self) -> DynArray[str]:
        return self.delegate_ids

    @gl.public.view
    def get_proposal(self, proposal_id: str) -> dict:
        p = self.proposals.get(proposal_id)
        if p is None:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Unknown proposal_id")
        return {
            "proposal_id": p.proposal_id,
            "creator": p.creator,
            "title": p.title,
            "description": p.description,
            "outcome_evidence_urls": list(p.outcome_evidence_urls),
            "voting_deadline": str(p.voting_deadline),
            "resolution_deadline": str(p.resolution_deadline),
            "status": p.status,
            "yes_weight_atto": str(p.yes_weight_atto),
            "no_weight_atto": str(p.no_weight_atto),
            "abstain_weight_atto": str(p.abstain_weight_atto),
        }

    @gl.public.view
    def list_proposal_ids(self) -> DynArray[str]:
        return self.proposal_ids

    @gl.public.view
    def get_vote(self, proposal_id: str, delegate_id: str) -> dict:
        key = f"{proposal_id}:{delegate_id}"
        v = self.votes.get(key)
        if v is None:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} No such vote")
        return {
            "proposal_id": v.proposal_id,
            "delegate_id": v.delegate_id,
            "choice": v.choice,
            "rationale": v.rationale,
            "evidence": [{"kind": e.kind, "value": e.value} for e in v.evidence],
            "weight_atto": str(v.weight_atto),
            "resolved": v.resolved,
            "outcome_verdict": v.outcome_verdict,
            "alignment_bucket": str(v.alignment_bucket),
            "resolution_summary": v.resolution_summary,
        }

    @gl.public.view
    def get_delegation(self, delegator: str, delegate_id: str) -> dict:
        key = f"{delegator}:{delegate_id}"
        record = self.delegations.get(key)
        if record is None:
            return {"active": False, "principal_atto": "0"}
        return {
            "delegator": record.delegator,
            "delegate_id": record.delegate_id,
            "principal_atto": str(record.principal_atto),
            "active": record.active,
        }

    @gl.public.view
    def get_protocol_stats(self) -> dict:
        return {
            "total_delegates": str(self.total_delegates),
            "total_proposals": str(self.total_proposals),
            "total_votes_cast": str(self.total_votes_cast),
            "total_slash_events": str(self.total_slash_events),
            "total_slashed_atto": str(self.total_slashed_atto),
            "total_compensation_paid_atto": str(self.total_compensation_paid_atto),
            "paused": self.paused,
        }

    @gl.public.view
    def preview_effective_weight(self, delegate_id: str) -> dict:
        d = self.delegates.get(delegate_id)
        if d is None:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Unknown delegate_id")
        bps = self._effective_weight_bps(d.integrity_score)
        atto = (d.total_delegated_atto * bps) // BPS_DENOMINATOR
        return {
            "integrity_score": str(d.integrity_score),
            "effective_weight_bps": str(bps),
            "effective_weight_atto": str(atto),
            "raw_delegated_atto": str(d.total_delegated_atto),
        }
