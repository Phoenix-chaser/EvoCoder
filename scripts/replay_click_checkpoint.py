"""Replay a real GLM learning checkpoint across green, original buggy source, and restore."""

import argparse
import json
import sqlite3
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path

from evocoder.contracts import Settings, digest
from evocoder.dataset import load_catalog
from evocoder.procedure_evidence import STATES, _public_nodes, _pytest_states, behavior_receipt
from evocoder.runtime import PublicCheckpoints
from evocoder.sandbox import Sandbox
from evocoder.store import Store


def read(path):
    return json.loads(path.read_text())


class Archive(Store):
    """Use the Store's existing read methods with a read-only SQLite connection."""

    def __init__(self, database):
        self.database = database.resolve()

    @contextmanager
    def connect(self, write=False):
        if write:
            raise ValueError("checkpoint replay cannot change the study archive")
        wal = self.database.with_name(self.database.name + "-wal")
        query = "?mode=ro" if wal.exists() and wal.stat().st_size else "?mode=ro&immutable=1"
        db = sqlite3.connect(self.database.as_uri() + query, uri=True)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()


def eligible_rows(protocol, result):
    for stage, skill, purpose in (("learning_replay", result.get("candidate"), "evaluate"),
                                 ("learning", protocol["parent"], "solve")):
        rows = result[stage]
        if any(r["task_id"] not in protocol["learn_ids"] for r in rows):
            raise ValueError("checkpoint replay source contains a task outside the frozen learning set")
        for key in protocol["learn_ids"]:
            for row in sorted((r for r in rows if r["task_id"] == key), key=lambda r: r.get("repeat", 0)):
                yield row, stage, skill, purpose


def choose(store, root, protocol, result, tasks):
    for row, stage, skill, purpose in eligible_rows(protocol, result):
        task = tasks[row["task_id"]]
        key = row["run_id"]
        run = store.run(key)
        if (run["state"] not in {"COMPLETE", "FAILED", "EXHAUSTED"} or run["mode"] != "live" or run["split"] != "learn"
                or run["purpose"] != purpose or run["skill_id"] != skill or not skill
                or run["config"] != protocol["settings"] or not run["config"].get("public_checkpoints")
                or run["result"]["image_id"] != protocol["settings"]["image"]):
            raise ValueError("replay source requires the recorded GLM learning arm with its actual skill applied")
        body = behavior_receipt(store, task, key)
        if digest(body) != row["behavior_hash"] or body != read(root / "behavior" / f"{key}.json"):
            raise ValueError("archived learning behavior does not match underlying events")
        for saved in body["saved_green"]:
            if saved["changed_from_input"]:
                e, = [e for e in store.events(key) if e["seq"] == saved["seq"] and e["kind"] == "PUBLIC_CHECKPOINT"]
                return task, {"run_id": key, "phase": stage, "skill_id": skill, "model": run["config"]["model"],
                              "task_id": task.id, "task_hash": body["task_hash"], "behavior_hash": digest(body),
                              "checkpoint_event": e}, store.get_artifact(saved["patch_hash"])
    return None


def exercise(task, source, settings, scratch):
    trace_id = "offline-click-checkpoint"
    green_hash = digest(source)
    original = {path: task.files[path] for path in task.editable}
    original_hash = digest(original)
    if green_hash == original_hash or set(source) != set(original):
        raise ValueError("replay requires a changed patch spanning all editable files")
    nodes = _public_nodes(task)
    if set(nodes) != set(STATES):
        raise ValueError("checkpoint replay requires both frozen public feature nodes")
    checkpoints = PublicCheckpoints(task, trace_id, scratch, settings)
    phases = []

    def phase(name, observation, workspace):
        states = _pytest_states(observation["output"], nodes)
        entry = {"stage": name, "workspace_hash": digest(workspace.export(task.editable)), "image_id": workspace.image_id,
                 "exit_code": observation["exit_code"], "timeout": observation["timeout"],
                 "named_states": states, "output": observation["output"][:10000]}
        phases.append(entry)
        if entry["timeout"] or entry["image_id"] != settings.image or set(states) != set(STATES):
            raise ValueError(f"{name} lacks two completed public feature checks in the frozen image")

    with ExitStack() as boxes:
        clean = boxes.enter_context(Sandbox({**task.files, **source}, settings, trace_id))
        first = checkpoints.test(clean, 1)
        phase("green", first, clean)
        if first.get("saved_checkpoint") != green_hash or phases[0]["workspace_hash"] != green_hash:
            raise ValueError("historical green patch did not reproduce")
        clean.close()

        # Deliberately replace the workspace with the known faulty version; no model makes this choice.
        broken = boxes.enter_context(Sandbox(task.files, settings, trace_id))
        red = checkpoints.test(broken, 2)
        phase("original_bug", red, broken)
        if (phases[1]["workspace_hash"] != original_hash or red["exit_code"] != 1
                or "failed" not in phases[1]["named_states"].values()):
            raise ValueError("original buggy source did not reproduce a failing feature check")

        restored, verified = checkpoints.restore(broken, boxes, green_hash, 3)
        phase("restored", verified, restored)
        if verified.get("restored_checkpoint") != green_hash or phases[2]["workspace_hash"] != green_hash:
            raise ValueError("restored editable workspace differs from the original green patch")
        if any(p["exit_code"] or set(p["named_states"].values()) != {"passed"} for p in (phases[0], phases[2])):
            raise ValueError("changed patch must pass both public states before and after restore")

    events = scratch.events(trace_id)
    restores = [e for e in events if e["kind"] == "CHECKPOINT_RESTORED"]
    if len(restores) != 1 or (restores[0]["payload"]["previous_patch_hash"], restores[0]["payload"]["restored_patch_hash"]) != (
        original_hash, green_hash
    ):
        raise ValueError("runtime did not bind the genuine restore to the original faulty workspace")
    return {"green_hash": green_hash, "original_hash": original_hash, "public_command": task.public_command,
            "public_nodes": nodes, "stages": phases, "runtime_events": events}


def replay(root):
    root = root.resolve()
    protocol, result = read(root / "protocol.json"), read(root / "result.json")
    if (protocol["claim"] != "independent_learn_procedure_revision" or not result["completed"]
            or result["protocol"] != protocol or protocol["settings"]["provider"] != "glm"):
        raise ValueError("expected a completed GLM procedure experiment")
    manifest = Path(protocol["learn_manifest"])
    if digest(read(manifest)) != protocol["learn_hash"]:
        raise ValueError("learning manifest changed")
    tasks = {t.id: t for t in load_catalog(manifest)}
    if any(t.split != "learn" for t in tasks.values()) or list(tasks) != protocol["learn_ids"]:
        raise ValueError("learning scope changed")
    selection = choose(Archive(root / "seed" / "evocoder.sqlite3"), root, protocol, result, tasks)
    report = {"schema": 1, "result_hash": digest(result), "protocol_hash": digest(protocol),
              "selection": "first changed public-green checkpoint in frozen task order; child learning replay, then parent",
              "model_calls": 0, "method": "controller replay outside GLM solving and release gates"}
    if selection is None:
        return {**report, "status": "no_changed_green_learning_checkpoint", "source": None}
    task, source, patch = selection
    with tempfile.TemporaryDirectory(prefix="evocoder-green-replay-") as directory:
        config = protocol["settings"]
        opts = Settings(_env_file=None, mode="mock", image=config["image"], state_dir=Path(directory),
                        command_timeout=config["command_timeout"], public_checkpoints=True)
        result = exercise(task, patch, opts, Store(opts.state_dir))
    return {**report, "status": "verified", "source": source, **result}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("choose a new replay report path")
    report = replay(args.root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps({"status": report["status"], "model_calls": 0, "source": (report.get("source") or {}).get("run_id")}))
