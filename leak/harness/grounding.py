"""Grounding: reject claims that don't cite real evidence.

The model may only *assert* things it can point to. Every finding it submits
carries evidence ids; this module checks them with pluggable rules before the
finding is accepted. Rejections go back to the model as a tool error with the
exact reasons, which models are good at fixing ("evidence pdf:ab:p1:l99 does
not exist" -> it re-reads and resubmits).

The harness stays domain-agnostic: Leak supplies the rules (does this id
resolve? does the cited text contain the amount?) as plain callables.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

# A rule looks at a claim and returns an error message, or None if satisfied.
type Rule[T] = Callable[[T], str | None]


@dataclass
class GroundingResult:
    ok: bool
    errors: list[str]


class Grounder[T]:
    def __init__(
        self,
        *,
        evidence_of: Callable[[T], Sequence[str]],
        exists: Callable[[str], bool],
        rules: Sequence[Rule[T]] = (),
    ) -> None:
        self.evidence_of = evidence_of
        self.exists = exists
        self.rules = list(rules)

    def check(self, claim: T) -> GroundingResult:
        errors: list[str] = []
        ids = list(self.evidence_of(claim))
        if not ids:
            errors.append("no evidence cited: every claim needs at least one evidence id")
        missing = [i for i in ids if not self.exists(i)]
        if missing:
            errors.append(f"evidence ids do not exist: {missing}")
        # Only run content rules once the citations themselves are valid.
        if not errors:
            errors += [e for rule in self.rules if (e := rule(claim))]
        return GroundingResult(ok=not errors, errors=errors)
