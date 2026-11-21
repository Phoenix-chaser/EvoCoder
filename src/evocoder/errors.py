from __future__ import annotations


class ActionRejected(ValueError):
    """A billed, complete model reply whose arguments cannot be executed safely."""

    def __init__(self, issues: list[dict], artifact: str | None = None):
        self.issues, self.artifact = issues, artifact
        super().__init__("model_contract: model action rejected; no tool executed")


class RoutingRejected(ValueError):
    """Invalid auxiliary memory decision; skip memory explicitly, never guess applicability."""


class ProposalRejected(ValueError):
    """A settled proposal failed validation; keep the old skill and count the failed attempt."""
