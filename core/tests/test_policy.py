"""Unit tests for phoenix.policy.

The policy gate is pure: no I/O, no DB, no async. These tests run in
milliseconds and give us a safety net for every future edit to policy.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from phoenix.db.models import PolicyDecision
from phoenix.policy import Policy, Rule


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def policy_from_rules(*rules: Rule) -> Policy:
    """Build a Policy directly from Rule objects, bypassing YAML."""
    return Policy(list(rules))


def rule(
    agent: str,
    action_type: str,
    decision: PolicyDecision,
    when: dict | None = None,
    index: int = 0,
) -> Rule:
    return Rule(
        agent=agent,
        action_type=action_type,
        when=when or {},
        decision=decision,
        index=index,
    )


# ---------------------------------------------------------------------------
# Exact (agent, action_type) match, no predicates.
# ---------------------------------------------------------------------------


def test_exact_match_returns_rule_decision() -> None:
    p = policy_from_rules(rule("mercury", "gmail.label", PolicyDecision.AUTO))
    result = p.evaluate("mercury", "gmail.label", {})
    assert result.decision is PolicyDecision.AUTO
    assert result.rule_index == 0


def test_wrong_agent_falls_through() -> None:
    p = policy_from_rules(rule("mercury", "gmail.label", PolicyDecision.AUTO))
    result = p.evaluate("mars", "gmail.label", {})
    assert result.decision is PolicyDecision.APPROVAL
    assert result.rule_index is None


def test_wrong_action_type_falls_through() -> None:
    p = policy_from_rules(rule("mercury", "gmail.label", PolicyDecision.AUTO))
    result = p.evaluate("mercury", "gmail.archive", {})
    assert result.decision is PolicyDecision.APPROVAL


# ---------------------------------------------------------------------------
# Wildcard agent.
# ---------------------------------------------------------------------------


def test_wildcard_agent_matches_anyone() -> None:
    p = policy_from_rules(rule("*", "notify.push", PolicyDecision.AUTO))
    for a in ("mercury", "mars", "sol"):
        assert p.evaluate(a, "notify.push", {}).decision is PolicyDecision.AUTO


# ---------------------------------------------------------------------------
# Predicates.
# ---------------------------------------------------------------------------


def test_scalar_predicate_equality() -> None:
    p = policy_from_rules(
        rule("mercury", "gmail.trash", PolicyDecision.AUTO, when={"category": "noise"})
    )
    assert p.evaluate("mercury", "gmail.trash", {"category": "noise"}).decision is PolicyDecision.AUTO
    assert p.evaluate("mercury", "gmail.trash", {"category": "work"}).decision is PolicyDecision.APPROVAL


def test_list_predicate_membership() -> None:
    p = policy_from_rules(
        rule(
            "mercury",
            "gmail.archive",
            PolicyDecision.AUTO,
            when={"category": ["transactional", "newsletter"]},
        )
    )
    assert p.evaluate("mercury", "gmail.archive", {"category": "newsletter"}).decision is PolicyDecision.AUTO
    assert p.evaluate("mercury", "gmail.archive", {"category": "transactional"}).decision is PolicyDecision.AUTO
    assert p.evaluate("mercury", "gmail.archive", {"category": "personal"}).decision is PolicyDecision.APPROVAL


def test_missing_arg_is_non_match() -> None:
    """A predicate keyed on an arg that isn't present must NOT match."""
    p = policy_from_rules(
        rule("mercury", "gmail.trash", PolicyDecision.AUTO, when={"category": "noise"})
    )
    result = p.evaluate("mercury", "gmail.trash", {})
    assert result.decision is PolicyDecision.APPROVAL


def test_multiple_predicates_are_and() -> None:
    p = policy_from_rules(
        rule(
            "mercury",
            "gmail.archive",
            PolicyDecision.AUTO,
            when={"category": "noise", "confidence_gte": 0.9},
        )
    )
    # Both match.
    assert p.evaluate(
        "mercury", "gmail.archive", {"category": "noise", "confidence_gte": 0.9}
    ).decision is PolicyDecision.AUTO
    # One mismatches.
    assert p.evaluate(
        "mercury", "gmail.archive", {"category": "noise", "confidence_gte": 0.5}
    ).decision is PolicyDecision.APPROVAL


# ---------------------------------------------------------------------------
# First-match-wins ordering.
# ---------------------------------------------------------------------------


def test_first_match_wins_over_later_catch_all() -> None:
    """Order matters: a specific rule first, a broad rule second."""
    p = policy_from_rules(
        rule("mercury", "gmail.trash", PolicyDecision.AUTO,
             when={"category": "noise"}, index=0),
        rule("mercury", "gmail.trash", PolicyDecision.BLOCKED, index=1),
    )
    # Specific rule matches → AUTO.
    assert p.evaluate("mercury", "gmail.trash", {"category": "noise"}).decision is PolicyDecision.AUTO
    # Specific rule's predicate fails → falls through to BLOCKED.
    assert p.evaluate("mercury", "gmail.trash", {"category": "work"}).decision is PolicyDecision.BLOCKED


def test_blocked_wins_when_listed_first() -> None:
    """A broad BLOCKED rule placed before AUTO should win — safety guarantee."""
    p = policy_from_rules(
        rule("mercury", "gmail.send", PolicyDecision.BLOCKED, index=0),
        rule("mercury", "gmail.send", PolicyDecision.AUTO, index=1),
    )
    assert p.evaluate("mercury", "gmail.send", {}).decision is PolicyDecision.BLOCKED


# ---------------------------------------------------------------------------
# Default behaviour.
# ---------------------------------------------------------------------------


def test_empty_policy_defaults_to_approval() -> None:
    p = policy_from_rules()
    result = p.evaluate("mercury", "gmail.archive", {})
    assert result.decision is PolicyDecision.APPROVAL
    assert result.rule_index is None
    assert "no rule matched" in result.reason


# ---------------------------------------------------------------------------
# YAML loading — against the real infra/policy.yaml.
# ---------------------------------------------------------------------------


REPO_ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = REPO_ROOT / "infra" / "policy.yaml"


def test_real_policy_file_loads() -> None:
    """The actual policy.yaml must parse cleanly."""
    p = Policy.load(POLICY_PATH)
    # Sanity: the file should have at least a dozen rules.
    assert len(p._rules) >= 10


def test_real_policy_gmail_send_is_blocked() -> None:
    p = Policy.load(POLICY_PATH)
    assert p.evaluate("mercury", "gmail.send", {}).decision is PolicyDecision.BLOCKED


def test_real_policy_gmail_permadelete_is_blocked() -> None:
    p = Policy.load(POLICY_PATH)
    assert p.evaluate("mercury", "gmail.permadelete", {}).decision is PolicyDecision.BLOCKED


def test_real_policy_kite_place_order_is_blocked() -> None:
    p = Policy.load(POLICY_PATH)
    assert p.evaluate("mars", "kite.place_order", {"symbol": "RELIANCE"}).decision is PolicyDecision.BLOCKED


def test_real_policy_trash_noise_is_auto() -> None:
    p = Policy.load(POLICY_PATH)
    assert p.evaluate("mercury", "gmail.trash", {"category": "noise"}).decision is PolicyDecision.AUTO


def test_real_policy_trash_work_is_blocked() -> None:
    """Trashing a 'work' email must be refused by the catch-all."""
    p = Policy.load(POLICY_PATH)
    assert p.evaluate("mercury", "gmail.trash", {"category": "work"}).decision is PolicyDecision.BLOCKED


def test_real_policy_archive_newsletter_is_auto() -> None:
    p = Policy.load(POLICY_PATH)
    assert p.evaluate("mercury", "gmail.archive", {"category": "newsletter"}).decision is PolicyDecision.AUTO


def test_real_policy_archive_personal_is_approval() -> None:
    p = Policy.load(POLICY_PATH)
    assert p.evaluate("mercury", "gmail.archive", {"category": "personal"}).decision is PolicyDecision.APPROVAL


def test_real_policy_notify_push_wildcard() -> None:
    p = Policy.load(POLICY_PATH)
    assert p.evaluate("mars", "notify.push", {}).decision is PolicyDecision.AUTO
    assert p.evaluate("saturn", "notify.push", {}).decision is PolicyDecision.AUTO


def test_real_policy_unknown_action_defaults_to_approval() -> None:
    p = Policy.load(POLICY_PATH)
    result = p.evaluate("mercury", "something.not.in.policy", {})
    assert result.decision is PolicyDecision.APPROVAL
    assert result.rule_index is None


# ---------------------------------------------------------------------------
# YAML loading — malformed files fail at boot.
# ---------------------------------------------------------------------------


def test_load_missing_rules_key(tmp_path: Path) -> None:
    bad = tmp_path / "policy.yaml"
    bad.write_text("version: 1\n")
    with pytest.raises(ValueError, match="missing top-level 'rules'"):
        Policy.load(bad)


def test_load_invalid_decision(tmp_path: Path) -> None:
    bad = tmp_path / "policy.yaml"
    bad.write_text(
        "rules:\n"
        "  - agent: mercury\n"
        "    action_type: gmail.label\n"
        "    decision: maybe\n"
    )
    with pytest.raises(ValueError, match="invalid decision"):
        Policy.load(bad)


def test_load_missing_required_key(tmp_path: Path) -> None:
    bad = tmp_path / "policy.yaml"
    bad.write_text(
        "rules:\n"
        "  - agent: mercury\n"
        "    decision: auto\n"          # action_type missing
    )
    with pytest.raises(ValueError, match="missing key"):
        Policy.load(bad)
