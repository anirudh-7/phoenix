"""Policy gate.

Every action an agent proposes passes through `Policy.evaluate()`. The result
is one of three decisions:

    AUTO      — execute immediately.
    APPROVAL  — queue for user approval; do not execute until approved.
    BLOCKED   — refuse to execute; record the attempt.

Rules live in infra/policy.yaml and are loaded once at startup. The gate
itself is a pure function: given (agent, action_type, args), return a
`Decision`. No I/O, no DB, no side effects. That's why this module is
trivial to unit-test and safe to call from anywhere in the codebase.

Matching semantics (first match wins):
  - `agent` matches if the rule's agent equals the action's agent, or the
    rule's agent is "*".
  - `action_type` is exact equality.
  - `when:` predicates are AND-ed. For each key, the rule's value may be a
    scalar (equality) or a list (membership). A key named in `when` that
    is absent from the action's args is a non-match.

Default when no rule matches: APPROVAL. We prefer to surface unknown
actions to the user rather than silently block or silently run them.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from phoenix.config import settings
from phoenix.db.models import PolicyDecision


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    """A single policy rule, parsed from YAML."""

    agent: str                          # "mercury", "mars", or "*"
    action_type: str                    # e.g. "gmail.archive"
    when: dict[str, Any]                # predicates on args; empty = always match
    decision: PolicyDecision
    index: int                          # position in file, for audit trail


@dataclass(frozen=True)
class Decision:
    """Result of evaluating the policy against a proposed action."""

    decision: PolicyDecision
    reason: str                         # human-readable, stored in actions.policy_reason
    rule_index: int | None              # None when default was used


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


class Policy:
    """A loaded policy. Immutable. Thread-safe. One per process."""

    def __init__(self, rules: list[Rule]) -> None:
        self._rules = rules

    @classmethod
    def load(cls, path: Path | None = None) -> Policy:
        """Load and validate the policy from YAML.

        Raises ValueError if the file is malformed or contains an unknown
        decision value. We validate eagerly so a typo in policy.yaml fails
        at boot, not at the moment an action is proposed.
        """
        target = path or settings.policy_path
        data = yaml.safe_load(target.read_text())
        if not isinstance(data, dict) or "rules" not in data:
            raise ValueError(f"{target}: missing top-level 'rules' key")

        rules: list[Rule] = []
        for i, raw in enumerate(data["rules"]):
            if not isinstance(raw, dict):
                raise ValueError(f"{target}: rule #{i} is not a mapping")
            try:
                rules.append(
                    Rule(
                        agent=raw["agent"],
                        action_type=raw["action_type"],
                        when=dict(raw.get("when") or {}),
                        decision=PolicyDecision(raw["decision"]),
                        index=i,
                    )
                )
            except KeyError as e:
                raise ValueError(f"{target}: rule #{i} missing key {e}") from e
            except ValueError as e:
                raise ValueError(f"{target}: rule #{i} has invalid decision: {e}") from e
        return cls(rules)

    def evaluate(
        self,
        agent: str,
        action_type: str,
        args: dict[str, Any] | None = None,
    ) -> Decision:
        """Classify a proposed action. Pure function — call as often as you like."""
        args = args or {}
        for rule in self._rules:
            if not _agent_matches(rule.agent, agent):
                continue
            if rule.action_type != action_type:
                continue
            if not _predicates_match(rule.when, args):
                continue
            return Decision(
                decision=rule.decision,
                reason=_format_reason(rule, args),
                rule_index=rule.index,
            )
        return Decision(
            decision=PolicyDecision.APPROVAL,
            reason=f"no rule matched ({agent}, {action_type}); default=approval",
            rule_index=None,
        )


# ---------------------------------------------------------------------------
# Matching helpers — package-private, exposed for tests.
# ---------------------------------------------------------------------------


def _agent_matches(rule_agent: str, action_agent: str) -> bool:
    return rule_agent == "*" or rule_agent == action_agent


def _predicates_match(when: dict[str, Any], args: dict[str, Any]) -> bool:
    """Every key in `when` must satisfy its condition against `args`."""
    for key, expected in when.items():
        if key not in args:
            return False
        actual = args[key]
        if isinstance(expected, list):
            if actual not in expected:
                return False
        elif actual != expected:
            return False
    return True


def _format_reason(rule: Rule, args: dict[str, Any]) -> str:
    if rule.when:
        matched = ", ".join(f"{k}={args.get(k)!r}" for k in rule.when)
        return f"rule #{rule.index} ({rule.agent}, {rule.action_type}; {matched})"
    return f"rule #{rule.index} ({rule.agent}, {rule.action_type})"
