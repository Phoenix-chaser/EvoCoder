"""Replay the archived step-seven counterexample through actual proposal admission on a temporary store."""
import argparse
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from audit_click_step_claim import audit as historical_audit
from evocoder.comparison import dump
from evocoder.contracts import Settings, Skill, digest
from evocoder.dataset import load_catalog
from evocoder.evolution import Evolution
from evocoder.revision_diagnostic import read
from evocoder.step_contract import StepRevision
from evocoder.store import Store
from report_click_procedure import Archive


class RecordedDecision:
    def __init__(self, decision):
        self.decision = decision

    def request(self, schema, system, data, run_id):
        assert schema is StepRevision and "invocations" in data
        return self.decision


def verify(root):
    historical = historical_audit(root)
    if historical["accepted_as_historical_fact"] or historical["verdict"] != "unsupported_no_matching_invocation":
        raise ValueError("original factual counterexample no longer reproduces")
    protocol = read(root / "protocol.json")
    refs = historical["evidence_refs"]
    tasks = {t.id: t for t in load_catalog(Path(protocol["learn_manifest"]))}
    relevant = [r for r in historical["checks"] if r["task_id"] == historical["case_mapping"]["task_id"]]
    actual = next(r for r in relevant if r["disabled_actual_line"] and r["named_states"]["disabled"] == "failed")
    valid = {"run_id": actual["run_id"], "observation_seq": actual["observation_seq"], **actual["named_states"]}
    missing = {**valid, "enabled": "passed", "disabled": "failed"}
    template = {"steps": [{"when": "after_edit", "action": "run_suite", "target": "both_states",
                           "observable": "output_and_status"}], "evidence_refs": refs, "facts": [valid]}
    with tempfile.TemporaryDirectory() as folder:
        st = Store(Path(folder) / "work", unit="credits")
        archive = Archive(root / "seed" / "evocoder.sqlite3")
        with archive.connect() as old, closing(sqlite3.connect(st.db_path)) as dest:
            old.backup(dest)
        settings = Settings(_env_file=None, **protocol["settings"])
        raw = Skill.model_validate(archive.get_artifact(historical["candidate"]))
        results = []
        for label, model_reply, accepted in [
            ("archived_unstructured_step", raw, False),
            ("joint_statuses_without_same_invocation", StepRevision.model_validate({**template, "facts": [missing]}), False),
            ("actual_same_invocation", StepRevision.model_validate(template), True),
        ]:
            with st.connect() as db:
                before = db.execute("SELECT COUNT(*) FROM skills").fetchone()[0]
            error = None
            try:
                Evolution(settings, st, RecordedDecision(model_reply), feedback_policy="learn_outcome",
                          proposal_policy="bound_procedure", grounding_tasks=tasks).propose(refs, historical["parent"])
            except ValueError as exc:
                error = str(exc).splitlines()[0]
            with st.connect() as db:
                added = db.execute("SELECT COUNT(*) FROM skills").fetchone()[0] - before
            if added != int(accepted) or (error is None) != accepted:
                raise ValueError(f"unexpected admission result for {label}: {error}")
            results.append({"case": label, "accepted": accepted, "skill_rows_added": added, "reason": error})
    return {"schema": 1, "model_calls": 0, "candidate": historical["candidate"], "step": historical["step"],
            "historical_audit_hash": digest(historical), "cited_runs": refs, "test_observations": relevant,
            "claimed": missing, "positive_control": valid, "checks": results,
            "scope": "Steps generated in bound_procedure mode contain typed actions. Public node outcomes are "
                     "rendered only from a single cited event. Arbitrary narrative, timing, and causal claims are unsupported."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous", type=Path, default=Path("experiments/glm-click-procedure-002"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    receipt = verify(args.previous)
    dump(args.output, receipt)
    print([(x["case"], x["accepted"]) for x in receipt["checks"]])
