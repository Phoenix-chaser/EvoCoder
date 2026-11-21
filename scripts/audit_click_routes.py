"""Audit the eight recorded GLM routing calls on two Click development tasks, offline."""

import argparse
import ast
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import httpx

from evocoder.contracts import LibrarySelection, canonical, digest


FOCUS = ("pallets-click-3d029e03eb", "pallets-click-42e2821ba3")
# The projection below reconstructs the archived integer_library_selection_v3 adapter.
ADAPTER_SHA256 = "1b28225b84ebdc88fec250ea20413191fa0fa165aef64d69576665961fc9c9c9"
WIRE_SCHEMA_HASH = "2a836ab9162622e426750855179a0267b274b5767c167caba32628838bf7489f"


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def read(path):
    return json.loads(path.read_text())


def readonly(path):
    wal = path.with_name(path.name + "-wal")
    query = "?mode=ro" if wal.exists() and wal.stat().st_size else "?mode=ro&immutable=1"
    connection = sqlite3.connect(path.resolve().as_uri() + query, uri=True)
    connection.row_factory = sqlite3.Row
    return closing(connection)


def artifact(db, key, kind):
    row = db.execute("SELECT body,kind FROM artifacts WHERE id=?", (key,)).fetchone()
    # One content hash may first be interned as a proposal_output, then registered as a skill.
    matched = row is not None and row["kind"] == kind
    if kind == "skill":
        matched = row is not None and db.execute("SELECT 1 FROM skills WHERE id=?", (key,)).fetchone() is not None
    require(matched, f"missing {kind} artifact {key}")
    body = json.loads(row["body"])
    require(digest(body) == key, f"bad artifact digest {key}")
    return body


def build_request(issue, skill, prompt, settings):
    # This pure builder prepares local HTTPX bytes. No transport or client is opened.
    parameters = LibrarySelection.model_json_schema()
    require(digest(parameters) == WIRE_SCHEMA_HASH, "unexpected LibrarySelection wire schema")
    parameters["properties"]["skill_index"]["enum"] = [-2, -1, 0]
    projection = {"task": {"issue": issue}, "skills": [{
        "applicability": skill["applicability"], "exclusions": skill["exclusions"], "skill_index": 0
    }]}
    system = (prompt
              + "\nReturn only the required tool fields. skill_index is the decision: choose an input index "
              "to apply, -1 to skip, or -2 if uncertain. Use an integer. The runtime binds artifact IDs."
              + "\nReturn exactly one call to the provided function select_library_skill; "
              "put the complete result in its arguments. Do not answer in plain text.")
    body = {
        "model": settings["model"],
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": canonical(projection)}],
        "tools": [{"type": "function", "function": {
            "name": "select_library_skill", "description": "Return exactly one complete structured result",
            "parameters": parameters,
        }}],
        "tool_choice": "auto", "max_tokens": settings["max_output_tokens"], "temperature": settings["temperature"],
        "thinking": {"type": "disabled" if settings["disable_thinking"] else "enabled"},
    }
    if settings["reasoning_effort"]:
        body["reasoning_effort"] = settings["reasoning_effort"]
    request = httpx.Request("POST", settings["base_url"].rstrip("/") + "/chat/completions", json=body)
    return projection, digest(body), hashlib.sha256(request.content).hexdigest()


def audit_run(db, ledger, record, arm, key, skill, prompt, settings):
    run_id = record[arm + "_run"]
    run = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    require(run and run["task_id"] == record["task_id"] and run["task_hash"] == record["task_hash"]
            and run["split"] == "dev" and run["purpose"] == "evaluate" and run["mode"] == "live"
            and json.loads(run["config"]) == settings, "run metadata differs from the paired record")
    events = [(r[0], r[1], json.loads(r[2])) for r in db.execute(
        "SELECT seq,kind,payload FROM events WHERE run_id=? ORDER BY seq", (run_id,))]

    def event(kind):
        found, = [(seq, data) for seq, k, data in events if k == kind]
        return found

    _, start = event("START")
    issue = start["task"]["issue"]
    response, = [(seq, d) for seq, k, d in events if k == "MODEL_RESPONSE"
                 and d["expected_function"] == "select_library_skill"]
    binding_seq, binding = event("ROUTING_RUNTIME_BINDING")
    route_seq, route = event("SKILL_ROUTE")
    raw = artifact(db, binding["content_artifact"], "model_library_selection")
    selection = LibrarySelection.model_validate(raw, context={"skill_count": 1})
    chosen = key if selection.decision == "apply" else None
    require(response[0] < binding_seq < route_seq and start["requested_skill_ids"] == [key]
            and route["requested_skill_ids"] == [key] and binding["contract"] == "integer_library_selection_v3"
            and selection.skill_index == binding["skill_index"]
            and selection.decision == binding["decision"] == route["decision"] == record[arm + "_route"]
            and chosen == binding["skill_id"] == route["skill_id"] == route["applied_skill_id"]
            == run["skill_id"] == record[arm + "_applied_skill"], "route/solver binding differs")
    for field in ("condition_indices", "exclusion_indices", "task_quotes", "reason"):
        require(route[field] == raw[field], f"controller changed reported {field}")
    require(all(0 <= i < len(skill["applicability"]) for i in selection.condition_indices)
            and all(q in issue for q in selection.task_quotes), "bad condition indices or literal quotes")
    logical = {"task": {"issue": issue}, "skills": [{
        "id": key, "applicability": skill["applicability"], "exclusions": skill["exclusions"]
    }]}
    require(digest(logical) == route["input_hash"] and digest(prompt) == route["prompt_hash"],
            "recorded logical routing input or prompt changed")
    projection, body_hash, wire_hash = build_request(issue, skill, prompt, settings)
    call = response[1]
    require(call["finish_reason"] == "tool_calls" and call["function_names"] == ["select_library_skill"]
            and call["choice_count"] == call["function_count"] == 1, "invalid response envelope")
    payment = ledger.execute("SELECT run_id,state,usage FROM calls WHERE id=?", (call["call_id"],)).fetchone()
    require(payment and payment["run_id"] == run_id and payment["state"] == "settled", "unbound route usage")
    usage = json.loads(payment["usage"])
    outcome = json.loads(run["result"])
    require(outcome["passed"] == record[arm + "_pass"] and outcome["routing"]["decision"] == selection.decision,
            "recorded outcome differs")
    grade = public = patch = None
    if outcome["patch"]:
        _, saved = event("PATCH")
        require(saved["artifact"] == outcome["patch"], "patch is not bound to run")
        artifact(db, saved["artifact"], "patch")
        patch = {"id": saved["artifact"], "diff": saved["diff"]}
        _, public = event("PUBLIC_VERIFY")
        public = public["observation"]["exit_code"]
        _, grade = event("GRADE")
        grade = grade["passed"]
        require(grade == outcome["passed"], "grade differs from run")
    return {
        "repeat": record["repeat"] + 1, "run_id": run_id, "task_hash": run["task_hash"],
        "recorded_input_hash": route["input_hash"], "recorded_prompt_hash": route["prompt_hash"],
        "rebuilt_wire_projection_hash": digest(projection), "rebuilt_body_hash": body_hash,
        "rebuilt_http_body_sha256": wire_hash, "config_hash": digest(settings),
        "response": call, "content_artifact": binding["content_artifact"], "binding_seq": binding_seq,
        "route_seq": route_seq, "selection": raw, "actual_route": route["decision"],
        "actually_applied": chosen, "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"], "cache_hit_tokens": usage["cache_hit_tokens"],
        "model_steps": outcome["model_steps"], "state": run["state"], "passed": outcome["passed"],
        "exhaustion_reason": outcome["exhaustion_reason"], "patch": patch, "public_exit_code": public,
        "grade": grade, "issue": issue,
    }


def audit(root):
    protocol, result = read(root / "protocol.json"), read(root / "result.json")
    require(result["completed"] and result["protocol"] == protocol
            and read(root / "started.json")["protocol_hash"] == digest(protocol), "not a completed frozen run")
    sources = {f.name: f.read_text() for f in (root / "source").glob("*.py")}
    require(digest(sources) == protocol["source_hash"]
            and hashlib.sha256(sources["model.py"].encode()).hexdigest() == ADAPTER_SHA256,
            "unrecognized frozen routing adapter")
    node, = [n for n in ast.parse(sources["routing.py"]).body if isinstance(n, ast.Assign)
             and any(isinstance(t, ast.Name) and t.id == "LIBRARY_PROMPT" for t in n.targets)]
    prompt = ast.literal_eval(node.value)
    settings = protocol["settings"]
    require(settings["provider"] == "glm", "requires GLM archive")
    # Resolve relative ledger paths from the archived experiments directory, independent of cwd.
    account = Path(settings["state_dir"])
    if not account.is_absolute():
        account = root.parent.parent / account
    groups = []
    with readonly(root / "seed/evocoder.sqlite3") as db, readonly(account / "evocoder.sqlite3") as ledger:
        for task_id in FOCUS:
            pair = sorted((r for r in result["paired_rows"] if r["task_id"] == task_id),
                          key=lambda row: row["repeat"])
            require([r["repeat"] for r in pair] == [0, 1], "focus task needs two actual paired rounds")
            for arm, key in (("baseline", protocol["parent"]), ("candidate", result["candidate"])):
                skill = artifact(db, key, "skill")
                runs = [audit_run(db, ledger, r, arm, key, skill, prompt, settings) for r in pair]
                groups.append({
                    "task_id": task_id, "arm": arm, "skill_id": key, "conditions": skill["applicability"],
                    "exclusions": skill["exclusions"], "runs": runs,
                    "matching_recorded_inputs": runs[0]["recorded_input_hash"] == runs[1]["recorded_input_hash"]
                    and runs[0]["recorded_prompt_hash"] == runs[1]["recorded_prompt_hash"],
                    "matching_reconstructed_http_body": runs[0]["rebuilt_http_body_sha256"] == runs[1]["rebuilt_http_body_sha256"],
                    "route_flipped": runs[0]["actual_route"] != runs[1]["actual_route"],
                })
    return {
        "schema": 1, "source_result_hash": digest(result), "frozen_source_hash": protocol["source_hash"],
        "parent": protocol["parent"], "candidate": result["candidate"], "model_calls": 0, "focus_tasks": list(FOCUS),
        "group_count": len(groups), "flipped_groups": sum(g["route_flipped"] for g in groups),
        "generation": {k: settings[k] for k in ("model", "temperature", "disable_thinking", "reasoning_effort", "max_output_tokens")},
        "httpx_version_used_for_reconstruction": httpx.__version__,
        "method": "Archived logical input, prompt, valid raw selection, controller binding, provider metadata and usage; "
                  "reconstructed local HTTP body. The archive did not retain original request bytes or a server seed.",
        "groups": groups,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    receipt = audit(args.previous.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as f:
        json.dump(receipt, f, ensure_ascii=False, indent=2)
        f.write("\n")
    for g in receipt["groups"]:
        print(g["task_id"], g["arm"], [t["actual_route"] for t in g["runs"]],
              [t["passed"] for t in g["runs"]], g["matching_reconstructed_http_body"])
