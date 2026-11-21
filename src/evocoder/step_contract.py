"""Controller-rendered procedural revisions and single-invocation evidence bindings."""
from typing import Literal

from pydantic import Field, model_validator

from evocoder.contracts import Skill, Strict, digest
from evocoder.grounding import RECEIPT_MARKER


class Instruction(Strict):
    when: Literal["initially", "if_uncovered", "after_edit", "if_uncertain", "after_green", "on_regression", "before_submit"]
    action: Literal["run_suite", "read_coverage", "trace", "patch", "write_cli_check", "rerun_cli_check",
                    "save_green", "restore_green", "submit"]
    target: Literal["reported_case", "enabled", "disabled", "missing_states", "both_states", "boundary_inputs"]
    observable: Literal["output", "status", "output_and_status", "spacing", "side_effects"]


class TestFact(Strict):
    run_id: str
    observation_seq: int = Field(ge=1)
    enabled: Literal["passed", "failed", "unreported"]
    disabled: Literal["passed", "failed", "unreported"]


class StepRevision(Strict):
    """Only normative operations and bounded factual assertions can enter the skill."""
    steps: list[Instruction] = Field(min_length=1, max_length=12)
    evidence_refs: list[str] = Field(min_length=1, max_length=6)
    facts: list[TestFact] = Field(max_length=6)

    @model_validator(mode="after")
    def unique(self):
        if len(set(self.evidence_refs)) != len(self.evidence_refs) or len({digest(f.model_dump()) for f in self.facts}) != len(self.facts):
            raise ValueError("duplicate reference or test fact")
        return self


WHEN = {"initially": "Before editing", "if_uncovered": "If a required behavior is missing from the supplied tests",
        "after_edit": "After editing", "if_uncertain": "When the behavior is unclear",
        "after_green": "After public tests pass", "on_regression": "If a later edit regresses a check",
        "before_submit": "Before submitting"}
TARGET = {"reported_case": "the reported behavior", "enabled": "the enabled state", "disabled": "the disabled state",
          "missing_states": "each required state missing from public coverage",
          "both_states": "both the enabled and disabled states", "boundary_inputs": "representative boundary inputs"}
OBS = {"output": "observable CLI output", "status": "the process exit status", "output_and_status": "CLI output and exit status",
       "spacing": "exact output lines and separators", "side_effects": "externally visible effects"}
OPERATIONS = {
    "run_suite": "run the supplied public tests and inspect their assertions",
    "read_coverage": "read the public examples and map their assertions to the issue requirements",
    "trace": "trace the rendering or decision path from the CLI entry point",
    "patch": "change the responsible implementation and preserve adjacent behavior",
    "write_cli_check": "write and execute your own small standalone Python CLI check using cli_check or command; invoke the real CLI and assert the expected behavior",
    "rerun_cli_check": "rerun the relevant authored CLI check against the current editable source",
    "save_green": "run the frozen public command and retain the resulting green checkpoint",
    "restore_green": "restore a previously verified checkpoint and recheck it",
    "submit": "submit the verified patch",
}


def render_step(step):
    return f"{WHEN[step.when]}, {OPERATIONS[step.action]}. Check {TARGET[step.target]} against {OBS[step.observable]}."


def available_invocations(catalog):
    """Only clean, model-requested public tests expose official per-state pytest verdicts."""
    return [
        {"run_id": key, "observation_seq": row["observation_seq"], "patch_hash": row["patch_hash"],
         "output_hash": row["output_hash"], "enabled": row["named_states"].get("enabled", "unreported"),
         "disabled": row["named_states"].get("disabled", "unreported")}
        for key, receipt in catalog["receipts"].items()
        for row in receipt["procedure"]["solver_requested_public_tests"]
    ]


def bind_revision(raw, parent, catalog):
    """The same fail-closed admission path is used by the live proposer and archive regression."""
    plan = StepRevision.model_validate(raw)
    if not set(plan.evidence_refs) <= catalog["receipts"].keys():
        raise ValueError("proposal cites a run outside frozen learning evidence")
    source = {(v["run_id"], v["observation_seq"]): v for v in available_invocations(catalog)}
    bound = []
    for fact in plan.facts:
        witness = source.get((fact.run_id, fact.observation_seq))
        if (fact.run_id not in plan.evidence_refs or not witness
                or any(getattr(fact, k) != witness[k] for k in ("enabled", "disabled"))):
            raise ValueError("feature-state claim lacks one matching cited test invocation")
        bound.append(witness)
    facts = [
        f"Learning run {v['run_id']}, public test observation {v['observation_seq']}, editable snapshot "
        f"{v['patch_hash'][:16]}: enabled={v['enabled']}, disabled={v['disabled']}."
        for v in bound
    ]
    # Replace historical free-text counterexamples and verification as well as steps. Routing
    # sees only the original scope fields; these come from the frozen parent, never the proposer.
    child = Skill.model_validate({
        "name": parent.name, "applicability": parent.applicability, "exclusions": parent.exclusions,
        "parent": parent.hash, "steps": [render_step(s) for s in plan.steps],
        "counterexamples": facts, "verification": ["Use the task's frozen public test command on the final editable snapshot."],
        "evidence_refs": plan.evidence_refs, "change_summary": RECEIPT_MARKER,
    })
    return child, bound
