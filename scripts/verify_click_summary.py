"""Replay the archived Click #3509 summary through the revision admission path without a provider."""

import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from evocoder.contracts import Settings, Skill, digest
from evocoder.dataset import load_catalog
from evocoder.evolution import Evolution, RECEIPT_MARKER
from evocoder.grounding import render_summary, validate_summary
from evocoder.store import Store


class RecordedProposal:
    def __init__(self, skill):
        self.skill = skill

    def request(self, schema, prompt, data, run_id):
        assert schema is Skill
        return self.skill


def replay(previous: Path) -> dict:
    result = json.loads((previous / "result.json").read_text())
    protocol = result["protocol"]
    tasks = {t.id: t for t in load_catalog(Path(protocol["manifest"])) if t.split == "learn"}
    follow = [item for item in result["follow_up"] if item["eligible_for_revision"]]
    target = next(item for item in follow if item["task_id"] == "pallets-click-29bacc773e")
    refs = [*protocol["seed_evidence_runs"], *(item["run_id"] for item in follow)]
    source = (previous / "seed" / "evocoder.sqlite3").resolve()
    wal = source.with_name(source.name + "-wal")
    if wal.exists() and wal.stat().st_size:
        raise ValueError("archive is not checkpointed")
    with TemporaryDirectory() as tmp:
        store = Store(Path(tmp), unit="credits")
        # Copy only learning evidence and skills into this offline fixture.
        # Billing records and the original ledger binding stay in the archive.
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro&immutable=1", uri=True)) as archive:
            with store.connect(write=True) as destination:
                for table in ("artifacts", "skills", "runs", "events"):
                    query, params = f"SELECT * FROM {table}", ()
                    if table in {"runs", "events"}:
                        column = "id" if table == "runs" else "run_id"
                        query += f" WHERE {column} IN ({','.join('?' for _ in refs)})"
                        params = tuple(refs)
                    rows = archive.execute(query, params)
                    slots = ",".join("?" for _ in rows.description)
                    destination.executemany(f"INSERT INTO {table} VALUES ({slots})", rows)
        original = Skill.model_validate(store.get_artifact(result["candidate"]))
        settings = Settings(_env_file=None, **protocol["settings"])
        proposer = RecordedProposal(original)
        evolve = Evolution(settings, store, proposer, feedback_policy=protocol["feedback_policy"],
                           proposal_policy="grounded_patch_tests", grounding_tasks=tasks)
        with store.connect() as db:
            before = db.execute("SELECT count(*) FROM skills").fetchone()[0]
        try:
            evolve.propose(refs, protocol["parent"])
        except ValueError as exc:
            if "rejected free-text summary" not in str(exc):
                raise
        else:
            raise ValueError("archived unsupported summary was admitted")
        with store.connect() as db:
            assert before == db.execute("SELECT count(*) FROM skills").fetchone()[0]
        proposer.skill = original.model_copy(update={"change_summary": RECEIPT_MARKER})
        corrected_id = evolve.propose(refs, protocol["parent"])
        corrected = Skill.model_validate(store.get_artifact(corrected_id))
        with store.connect() as db:
            row = db.execute("SELECT payload FROM events WHERE kind='SUMMARY_VERIFIED' ORDER BY rowid DESC LIMIT 1").fetchone()
        receipt = store.get_artifact(json.loads(row[0])["artifact"])
        catalog = store.get_artifact(receipt["catalog"])
        validate_summary(corrected.change_summary, corrected.evidence_refs, catalog)
        key = target["run_id"]
        observed = catalog["receipts"][key]
        methods = {name for file in observed["files"] for name in (file["symbols"] or [])}
        assert target["run_id"] in corrected.evidence_refs and receipt["candidate"] == corrected_id
        assert methods == {"Command.format_help_text", "Command.get_short_help_str"}
        assert observed["public_check"]["status"] == "passed" and observed["final_grade"]["status"] == "failed"
        line = render_summary(catalog, [key])
        # Each negative control changes precisely one fact in an otherwise valid summary.
        controls = {
            "historical_claim": original.change_summary,
            "fabricated_symbol": line.replace("Command.format_help_text", "Option.get_help_record"),
            "wrong_public_verdict": line.replace("public test passed", "public test failed"),
            "wrong_final_verdict": line.replace("final grade failed", "final grade passed"),
        }
        for text in controls.values():
            assert text != line
            try:
                validate_summary(text, [key], catalog)
            except ValueError:
                continue
            raise ValueError("false summary passed receipt validation")
        return {
            "schema": 1, "run_id": key, "source_result_hash": digest(result), "parent": protocol["parent"],
            "old_candidate": result["candidate"], "previous_summary": original.change_summary,
            "false_summary_rejected": True, "canonical_accepted": True, "model_calls": 0,
            "rejected_controls": list(controls), "verified_summary": line, "learning_receipt": observed,
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    proof = replay(args.previous)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as target:
        json.dump(proof, target, ensure_ascii=False, indent=2)
        target.write("\n")
    print(proof["verified_summary"])
