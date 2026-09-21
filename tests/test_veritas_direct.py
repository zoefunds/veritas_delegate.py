"""
Direct-mode tests for Veritas — AI Delegate Accountability Protocol.

These exercise business logic, storage transitions, escrow bookkeeping, and
validation/revert paths without spinning up full GenVM consensus (the
leader function only — validator comparison logic is exercised separately
in integration tests once a live/staging deployment address is available).

Run with:
    pytest tests/test_veritas_direct.py -v
"""

import json

CONTRACT_PATH = "contracts/veritas_delegate.py"

ONE_GEN = 10**18


# ---------------------------------------------------------------------------
# Registration / bonding
# ---------------------------------------------------------------------------


def test_register_delegate_requires_minimum_bond(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy(CONTRACT_PATH)
    direct_vm.sender = direct_alice

    direct_vm.value = ONE_GEN // 2  # below 1 GEN minimum
    with direct_vm.expect_revert("Bond below minimum"):
        contract.register_delegate("delegate_a", "ipfs://profile_a")


def test_register_delegate_success(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy(CONTRACT_PATH)
    direct_vm.sender = direct_alice
    direct_vm.value = ONE_GEN

    contract.register_delegate("delegate_a", "ipfs://profile_a")

    delegate = contract.get_delegate("delegate_a")
    assert delegate["status"] == "ACTIVE"
    assert delegate["bonded_atto"] == str(ONE_GEN)
    assert delegate["integrity_score"] == "7500"


def test_register_delegate_duplicate_id_reverts(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy(CONTRACT_PATH)

    direct_vm.sender = direct_alice
    direct_vm.value = ONE_GEN
    contract.register_delegate("delegate_a", "ipfs://a")

    direct_vm.sender = direct_bob
    direct_vm.value = ONE_GEN
    with direct_vm.expect_revert("already registered"):
        contract.register_delegate("delegate_a", "ipfs://b")


def test_top_up_bond_only_controller(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy(CONTRACT_PATH)
    direct_vm.sender = direct_alice
    direct_vm.value = ONE_GEN
    contract.register_delegate("delegate_a", "ipfs://a")

    direct_vm.sender = direct_bob
    direct_vm.value = ONE_GEN
    with direct_vm.expect_revert("not this delegate's controller"):
        contract.top_up_bond("delegate_a")

    direct_vm.sender = direct_alice
    direct_vm.value = ONE_GEN
    contract.top_up_bond("delegate_a")
    delegate = contract.get_delegate("delegate_a")
    assert delegate["bonded_atto"] == str(2 * ONE_GEN)


# ---------------------------------------------------------------------------
# Delegation
# ---------------------------------------------------------------------------


def test_delegate_stake_and_withdraw(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy(CONTRACT_PATH)

    direct_vm.sender = direct_alice
    direct_vm.value = ONE_GEN
    contract.register_delegate("delegate_a", "ipfs://a")

    direct_vm.sender = direct_bob
    direct_vm.value = ONE_GEN
    contract.delegate_stake("delegate_a")

    delegate = contract.get_delegate("delegate_a")
    assert delegate["total_delegated_atto"] == str(ONE_GEN)

    contract.withdraw_delegation("delegate_a")
    delegate = contract.get_delegate("delegate_a")
    assert delegate["total_delegated_atto"] == "0"


def test_withdraw_delegation_without_active_delegation_reverts(direct_vm, direct_deploy, direct_bob):
    contract = direct_deploy(CONTRACT_PATH)
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("No active delegation"):
        contract.withdraw_delegation("nonexistent")


# ---------------------------------------------------------------------------
# Proposals + voting
# ---------------------------------------------------------------------------


def _register_and_delegate(contract, direct_vm, controller, delegator, delegate_id="delegate_a"):
    direct_vm.sender = controller
    direct_vm.value = ONE_GEN
    contract.register_delegate(delegate_id, "ipfs://a")

    direct_vm.sender = delegator
    direct_vm.value = 5 * ONE_GEN
    contract.delegate_stake(delegate_id)
    direct_vm.value = 0


def test_create_proposal_and_cast_vote(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy(CONTRACT_PATH)
    _register_and_delegate(contract, direct_vm, direct_alice, direct_bob)

    direct_vm.sender = direct_bob
    contract.create_proposal(
        "prop_1",
        "Fund the widget grant",
        "Grant $10k to ship a widget integration by Q3",
        ["https://example.com/widget-status"],
        1000,  # voting_deadline
        2000,  # resolution_deadline
        0,     # now_ts
    )

    direct_vm.sender = direct_alice
    contract.cast_vote(
        "prop_1",
        "delegate_a",
        "YES",
        "This grant will ship a working widget integration by the Q3 deadline based on the team's track record.",
        ["https://example.com/widget-status"],
        100,
    )

    vote = contract.get_vote("prop_1", "delegate_a")
    assert vote["choice"] == "YES"
    assert vote["resolved"] is False
    assert vote["weight_atto"] == str(5 * ONE_GEN)

    proposal = contract.get_proposal("prop_1")
    assert proposal["yes_weight_atto"] == str(5 * ONE_GEN)


def test_cast_vote_rejects_short_rationale(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy(CONTRACT_PATH)
    _register_and_delegate(contract, direct_vm, direct_alice, direct_bob)

    direct_vm.sender = direct_bob
    contract.create_proposal("prop_1", "T", "D", ["https://example.com/x"], 1000, 2000, 0)

    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("too short"):
        contract.cast_vote("prop_1", "delegate_a", "YES", "too short", [], 100)


def test_cast_vote_after_deadline_reverts(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy(CONTRACT_PATH)
    _register_and_delegate(contract, direct_vm, direct_alice, direct_bob)

    direct_vm.sender = direct_bob
    contract.create_proposal("prop_1", "T", "D", ["https://example.com/x"], 1000, 2000, 0)

    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("deadline has passed"):
        contract.cast_vote(
            "prop_1",
            "delegate_a",
            "YES",
            "Rationale long enough to pass the minimum length validation check.",
            [],
            5000,
        )


# ---------------------------------------------------------------------------
# Outcome resolution (web + LLM mocked)
# ---------------------------------------------------------------------------


def test_resolve_vote_outcome_aligned(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy(CONTRACT_PATH)
    _register_and_delegate(contract, direct_vm, direct_alice, direct_bob)

    direct_vm.sender = direct_bob
    contract.create_proposal(
        "prop_1", "T", "D", ["https://example.com/widget-status"], 1000, 2000, 0
    )

    direct_vm.sender = direct_alice
    contract.cast_vote(
        "prop_1",
        "delegate_a",
        "YES",
        "This grant will ship a working widget integration by the Q3 deadline.",
        [],
        100,
    )

    direct_vm.mock_web(
        r".*example\.com/widget-status.*",
        {"status": 200, "body": "The widget integration shipped and was successfully delivered on time."},
    )
    direct_vm.mock_llm(
        r".*STATED RATIONALE.*",
        json.dumps({"alignment_score": 92, "summary": "Rationale matches shipped outcome."}),
    )

    verdict = contract.resolve_vote_outcome("prop_1", "delegate_a", 3000, b"")
    assert verdict == "ALIGNED"

    vote = contract.get_vote("prop_1", "delegate_a")
    assert vote["resolved"] is True
    assert vote["outcome_verdict"] == "ALIGNED"

    delegate = contract.get_delegate("delegate_a")
    assert int(delegate["integrity_score"]) > 7500  # EMA pulled toward 10000


def test_resolve_vote_outcome_misaligned_can_trigger_slash(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy(CONTRACT_PATH)
    _register_and_delegate(contract, direct_vm, direct_alice, direct_bob)

    direct_vm.sender = direct_bob
    contract.create_proposal(
        "prop_1", "T", "D", ["https://example.com/widget-status"], 1000, 2000, 0
    )

    direct_vm.sender = direct_alice
    contract.cast_vote(
        "prop_1",
        "delegate_a",
        "YES",
        "This grant will ship a working widget integration by the Q3 deadline.",
        [],
        100,
    )

    direct_vm.mock_web(
        r".*example\.com/widget-status.*",
        {"status": 200, "body": "The project was cancelled and the team abandoned the grant entirely."},
    )
    direct_vm.mock_llm(
        r".*STATED RATIONALE.*",
        json.dumps({"alignment_score": 5, "summary": "Rationale contradicted by cancellation."}),
    )

    verdict = contract.resolve_vote_outcome("prop_1", "delegate_a", 3000, b"")
    assert verdict == "MISALIGNED"

    delegate = contract.get_delegate("delegate_a")
    assert int(delegate["integrity_score"]) < 7500


def test_resolve_vote_outcome_before_window_reverts(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy(CONTRACT_PATH)
    _register_and_delegate(contract, direct_vm, direct_alice, direct_bob)

    direct_vm.sender = direct_bob
    contract.create_proposal(
        "prop_1", "T", "D", ["https://example.com/widget-status"], 1000, 2000, 0
    )
    direct_vm.sender = direct_alice
    contract.cast_vote(
        "prop_1", "delegate_a", "YES",
        "This grant will ship a working widget integration by the Q3 deadline.",
        [], 100,
    )

    with direct_vm.expect_revert("Resolution window has not opened"):
        contract.resolve_vote_outcome("prop_1", "delegate_a", 1500, b"")


# ---------------------------------------------------------------------------
# Slashing + compensation
# ---------------------------------------------------------------------------


def test_repeated_misalignment_slashes_and_funds_compensation_pool(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    contract = direct_deploy(CONTRACT_PATH)
    _register_and_delegate(contract, direct_vm, direct_alice, direct_bob)

    direct_vm.mock_llm(
        r".*STATED RATIONALE.*",
        json.dumps({"alignment_score": 2, "summary": "Badly contradicted."}),
    )
    direct_vm.mock_web(
        r".*example\.com/.*",
        {"status": 200, "body": "Cancelled, abandoned, failed outright."},
    )

    for i in range(6):
        proposal_id = f"prop_{i}"
        direct_vm.sender = direct_bob
        contract.create_proposal(
            proposal_id, "T", "D", ["https://example.com/status"], 1000, 2000, 0
        )
        direct_vm.sender = direct_alice
        contract.cast_vote(
            proposal_id, "delegate_a", "YES",
            "This grant will definitely ship a working integration by the deadline.",
            [], 100,
        )
        contract.resolve_vote_outcome(proposal_id, "delegate_a", 3000, b"")

    delegate = contract.get_delegate("delegate_a")
    assert int(delegate["integrity_score"]) < 3000
    assert int(delegate["compensation_pool_atto"]) > 0
    assert int(delegate["bonded_atto"]) < ONE_GEN

    direct_vm.sender = direct_bob
    claimed = contract.claim_compensation("delegate_a")
    assert claimed > 0

    delegation = contract.get_delegation(str(direct_bob), "delegate_a")
    assert delegation["active"] is False


def test_claim_compensation_without_pool_reverts(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy(CONTRACT_PATH)
    _register_and_delegate(contract, direct_vm, direct_alice, direct_bob)

    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("No compensation pool"):
        contract.claim_compensation("delegate_a")


# ---------------------------------------------------------------------------
# Stale-resolution recovery path
# ---------------------------------------------------------------------------


def test_reclaim_stale_vote_marks_inconclusive_without_slash(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    contract = direct_deploy(CONTRACT_PATH)
    _register_and_delegate(contract, direct_vm, direct_alice, direct_bob)

    direct_vm.sender = direct_bob
    contract.create_proposal(
        "prop_1", "T", "D", ["https://example.com/status"], 1000, 2000, 0
    )
    direct_vm.sender = direct_alice
    contract.cast_vote(
        "prop_1", "delegate_a", "YES",
        "This grant will ship a working integration by the deadline.",
        [], 100,
    )

    far_future = 2000 + 31 * 24 * 60 * 60
    contract.reclaim_stale_vote_bond_share("prop_1", "delegate_a", far_future)

    vote = contract.get_vote("prop_1", "delegate_a")
    assert vote["resolved"] is True
    assert vote["outcome_verdict"] == "INCONCLUSIVE"

    delegate = contract.get_delegate("delegate_a")
    assert delegate["integrity_score"] == "7500"  # untouched — no slash path taken


# ---------------------------------------------------------------------------
# Unbonding lifecycle
# ---------------------------------------------------------------------------


def test_unbond_blocked_while_votes_unresolved(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy(CONTRACT_PATH)
    _register_and_delegate(contract, direct_vm, direct_alice, direct_bob)

    direct_vm.sender = direct_bob
    contract.create_proposal("prop_1", "T", "D", ["https://example.com/status"], 1000, 2000, 0)
    direct_vm.sender = direct_alice
    contract.cast_vote(
        "prop_1", "delegate_a", "YES",
        "This grant will ship a working integration by the deadline.",
        [], 100,
    )

    with direct_vm.expect_revert("unresolved votes"):
        contract.request_unbond("delegate_a", 100)


def test_full_unbond_and_withdraw_cycle(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy(CONTRACT_PATH)
    direct_vm.sender = direct_alice
    direct_vm.value = ONE_GEN
    contract.register_delegate("delegate_a", "ipfs://a")
    direct_vm.value = 0

    contract.request_unbond("delegate_a", 1000)

    with direct_vm.expect_revert("cooldown not yet elapsed"):
        contract.withdraw_bond("delegate_a", 1000)

    contract.withdraw_bond("delegate_a", 1000 + 14 * 24 * 60 * 60 + 1)
    delegate = contract.get_delegate("delegate_a")
    assert delegate["status"] == "EXITED"
    assert delegate["bonded_atto"] == "0"


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------


def test_pause_blocks_new_registration(direct_vm, direct_deploy, direct_owner, direct_alice):
    contract = direct_deploy(CONTRACT_PATH)
    direct_vm.sender = direct_owner
    contract.set_paused(True)

    direct_vm.sender = direct_alice
    direct_vm.value = ONE_GEN
    with direct_vm.expect_revert("Contract is paused"):
        contract.register_delegate("delegate_a", "ipfs://a")
