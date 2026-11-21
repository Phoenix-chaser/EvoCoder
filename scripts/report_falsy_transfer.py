"""Audit the raw transfer archive or verify the published paired observations without a key."""

import argparse
import re
import subprocess
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

from falsy_transfer_cases import DEV, PILOT, make_case
from falsy_transfer_protocol import (
    ARMS,
    FAMILIES,
    LEARNED,
    PREVIOUS,
    order,
    pilot_decision,
    probe_patch,
    rescue_targets,
)
from report_click_procedure import Archive, read
from run_falsy_transfer import require
from run_optional_control import curve_point

from evocoder.comparison import dump
from evocoder.contracts import Settings, digest
from evocoder.dataset import load_catalog
from evocoder.gate import gate_summary, skill_behavior
from evocoder.jobs import bounded_map

PUBLIC = Path("docs/results/falsy-transfer.json")
REGISTRATION = Path("benchmarks/falsy_transfer/protocol.json")
GATE_FIELDS = (
    "task_id",
    "task_hash",
    "source_group",
    "split",
    "repeat",
    "baseline_run",
    "candidate_run",
    "baseline_pass",
    "candidate_pass",
    "baseline_applied_skill",
    "candidate_applied_skill",
    "candidate_route",
    "skill_behavior",
    "infrastructure_error",
)


def arm_summary(rows, label):
    return {
        "trials": len(rows),
        "passed": sum(r[label]["passed"] for r in rows),
        "applied": sum(r[label]["applied"] is not None for r in rows),
        "exhausted": sum(r[label]["state"] == "EXHAUSTED" for r in rows),
        "calls": sum(r[label]["calls"] for r in rows),
        "actions": sum(r[label]["steps"] for r in rows),
    }


def summarize(data):
    result = {}
    for phase, arms in ARMS.items():
        rows = data[phase]
        result[phase] = {
            "arms": {a: arm_summary(rows, a) for a in arms},
            "comparisons": {
                a: {
                    "wins": sum(not r["bare"]["passed"] and r[a]["passed"] for r in rows),
                    "losses": sum(r["bare"]["passed"] and not r[a]["passed"] for r in rows),
                    "falsy_rescues": sum(bool(rescue_targets(r, a)) for r in rows),
                }
                for a in arms
                if a != "bare"
            },
        }
    result["shared_dev_rescues"] = sum(
        bool(set(rescue_targets(r, "manual")) & set(rescue_targets(r, "learned"))) for r in data["dev"]
    )
    outcomes = []
    for row in data["pilot"]:
        targets = rescue_targets(row, "manual")
        b, m = row["bare"]["passed"], row["manual"]["passed"]
        label = {
            (True, True): "both_pass",
            (True, False): "bare_only",
            (False, True): "manual_only",
            (False, False): "both_miss",
        }
        outcomes.append(
            {
                "task": row["task"],
                "repeat": row["repeat"],
                "targets": targets,
                "outcome": "falsy_rescue" if targets else label[b, m],
            }
        )
    result["pilot"]["outcomes"] = outcomes
    return result


def verify(data, p):
    require(
        data["protocol"] == digest(p)
        and p["learned"] == LEARNED
        and p["previous"] == PREVIOUS
        and digest(p["manual"]) == p["manual_hash"],
        "unbound protocol or skill",
    )
    manifest = {r["id"]: r for r in p["cases"]}
    calibrated = {r["task"]: r for r in p["calibration"]}
    require(
        set(calibrated) == set(p["pilot"] + p["dev"])
        and all(
            r["snapshot"] == manifest[k]["hash"]
            and not r["broken"]["public"]
            and any(not r["broken"][n] for n in r["targets"])
            and all(r["fixed"].values())
            for k, r in calibrated.items()
        ),
        "incomplete calibrated item set",
    )
    seen = set()
    for phase in ARMS:
        rows = data[phase]
        ids = p[phase] if phase == "pilot" or data["pilot_gate"]["passed"] else []
        expected = {(t, n) for t in ids for n in range(p[phase + "_repeats"])}
        require(
            len(rows) == len(expected) and {(r["task"], r["repeat"]) for r in rows} == expected,
            "incomplete paired phase",
        )
        for row in rows:
            t, n = row["task"], row["repeat"]
            require(
                row["snapshot"] == manifest[t]["hash"]
                and row["order"] == list(order(phase, ids.index(t), n)),
                "incorrect task or arm sequence",
            )
            for arm in ARMS[phase]:
                a = row[arm]
                require(
                    a["run"] not in seen and a["steps"] <= p["solver_slots"] and a["calls"] >= a["steps"],
                    "reused or over-budget attempt",
                )
                seen.add(a["run"])
                allowed = {"bare": {None}, "manual": {p["manual_hash"]}, "learned": {None, LEARNED}}[arm]
                require(
                    a["applied"] in allowed
                    and a["state"] in ("COMPLETE", "FAILED", "EXHAUSTED")
                    and a["passed"] == (a["state"] == "COMPLETE"),
                    "wrong exposure or solver result",
                )
                require(
                    (arm != "manual" or a["route"] == "forced")
                    and (arm != "bare" or a["route"] == "none")
                    and (arm != "learned" or (a["applied"] is not None) == (a["route"] == "apply")),
                    "wrong routing",
                )
                if a["patch"]:
                    require(
                        set(a["probes"]) == {"public", "controls", *calibrated[t]["targets"]}
                        and all(type(v) is bool for v in a["probes"].values())
                        and all(a["probes"].values()) == a["passed"],
                        "unbound boundary diagnosis",
                    )
                else:
                    require(a["probes"] is None and not a["passed"], "missing submitted patch")
    require(
        pilot_decision(data["pilot"], p["pilot"]) == data["pilot_gate"], "incorrect repeated-rescue decision"
    )
    gate = data["gate"]
    require(bool(gate) == data["pilot_gate"]["passed"], "formal gate started outside protocol")
    prior = read(Path("docs/results/optional-control.json"))
    (prior_skill,) = [s for s in prior["skills"] if s["id"] == p["learned"]]
    expected_support = {r["task"] for r in prior["learning"] if r["run"] in prior_skill["evidence_runs"]}
    require(set(data["support"]) == (expected_support if gate else set()), "wrong original support")
    if gate:
        rows = gate["rows"]
        expected = {(t, n) for t in p["dev"] for n in range(p["dev_repeats"])} | {
            (t, 0) for t in data["support"]
        }
        require(
            len(rows) == len(expected) and expected == {(r["task_id"], r["repeat"]) for r in rows},
            "incomplete support or development gate",
        )
        dev = {(r["task"], r["repeat"]): r for r in data["dev"]}
        for r in rows:
            t = manifest[r["task_id"]]
            require(
                (t["hash"], t["source_group"], t["split"]) == (r["task_hash"], r["source_group"], r["split"])
                and r["baseline_applied_skill"] is None
                and r["candidate_applied_skill"] in (None, LEARNED)
                and skill_behavior(r, LEARNED, set()) == r["skill_behavior"],
                "wrong release provenance",
            )
            if r["split"] == "dev":
                pair = dev[r["task_id"], r["repeat"]]
                for name, arm in (("baseline", "bare"), ("candidate", "learned")):
                    require(
                        (r[name + "_run"], r[name + "_pass"], r[name + "_applied_skill"])
                        == (pair[arm]["run"], pair[arm]["passed"], pair[arm]["applied"]),
                        "changed gate comparator",
                    )
            else:
                ids = {r["baseline_run"], r["candidate_run"]}
                require(len(ids) == 2 and seen.isdisjoint(ids), "reused support attempt")
                seen.update(ids)
        computed = gate_summary(rows, p["dev_repeats"])
        require(computed == gate["decision"], "incorrect release decision")
    released = bool(gate and gate["decision"]["gate"]["passed"])
    require(
        data["published"] == released
        and data["phase"]
        == ("complete" if released else "development_rejected" if gate else "pilot_rejected"),
        "unearned release",
    )
    verify_holdout(data, p, manifest, released, seen)
    verify_billing(data, ARMS, seen)
    require(data["summary"] == summarize(data), "incorrect repair or rescue counts")
    return {
        "phase": data["phase"],
        "pilot": data["pilot_gate"],
        "summary": data["summary"],
        "published": released,
        "holdout_pairs": len(data["holdout"]),
        "costs": data["costs"],
    }


def verify_holdout(data, p, manifest, released, seen):
    held = data["holdout"]
    expected_ids = [t for f in FAMILIES for t in p["holdout"][f]] if released else []
    require(
        [r["task_id"] for r in held] == expected_ids and data["holdout_exposures"] == len(expected_ids),
        "incorrect or premature holdout",
    )
    points, accumulated = [], []
    for f in FAMILIES if released else []:
        added = [r for r in held if r["family"] == f]
        require(
            len(added) == 10 and [r["task_id"] for r in added] == p["holdout"][f], "wrong holdout stratum"
        )
        for r in added:
            require(
                r["split"] == "test"
                and r["task_hash"] == manifest[r["task_id"]]["hash"]
                and r["repeat"] == 0
                and not r["infrastructure_error"]
                and r["baseline_applied_skill"] is None
                and r["candidate_applied_skill"] in (None, LEARNED)
                and r["skill_behavior"] == skill_behavior(r, LEARNED, set()),
                "incorrect holdout trial",
            )
            for arm in ("baseline", "candidate"):
                rid = r[arm + "_run"]
                require(rid not in seen, "reused held-out observation")
                seen.add(rid)
        accumulated.extend(added)
        points.append(curve_point(accumulated, f, added))
    require(points == data["curve"], "curve without supporting paired differences")


def verify_billing(data, phase_arms, seen):
    bills = data["billing_by_run"]
    require(
        set(bills) == seen | {r["failed_run"] for r in data["transport"]}, "unreported or unbilled trials"
    )
    for phase, arms in phase_arms.items():
        for row in data[phase]:
            require(
                all(row[a]["calls"] == bills[row[a]["run"]]["calls"] for a in arms), "wrong model call count"
            )
            require(
                all(re.fullmatch("[a-f0-9]{64}", row[a]["first_solver_request"]) for a in arms),
                "missing first solver request receipt",
            )
    costs = data["costs"]
    require(
        all(type(v) is int and v >= 0 for b in bills.values() for v in b.values())
        and (
            sum(b["calls"] for b in bills.values()),
            sum(b["charged_nanos"] for b in bills.values()),
            sum(b["reserved_nanos"] for b in bills.values()),
        )
        == (
            costs["calls"],
            Decimal(str(costs["cost_credits"])) * 10**9,
            Decimal(str(costs["reserved_credits"])) * 10**9,
        ),
        "incorrect ledger total",
    )


def audit_archive(root, p, *, primary=None):
    raw = read(root / "result.json")
    require(
        read(root / "protocol.json") == p and raw["completed"] and raw["unassigned"] == 0,
        "unfinished archive",
    )
    source = Archive(root / "state/evocoder.sqlite3")
    manual = Archive(root / "manual-state/evocoder.sqlite3")
    require(
        digest(manual.get_artifact(p["manual_hash"])) == p["manual_hash"]
        and digest(source.get_artifact(LEARNED)) == LEARNED,
        "missing original procedures",
    )
    with manual.connect() as db:
        manual_ids = {r[0] for r in db.execute("SELECT id FROM runs")}
    tasks = {t.id: t for t in load_catalog(root / "dataset/manifest.json")}
    slots = {}
    for path in (root / "slots").glob("*.json"):
        intent = read(path)
        require(intent["protocol"] == digest(p) and digest(intent)[:32] == path.stem, "unbound solver slot")
        slots[path.stem] = intent
    with source.connect() as db:
        (binding,) = db.execute("SELECT root FROM ledger_binding").fetchone()
        exposures = {r[0] for r in db.execute("SELECT id FROM runs WHERE split='test'")}
        release = db.execute(
            "SELECT after_id,created FROM library_releases ORDER BY seq DESC LIMIT 1"
        ).fetchone()
    require(
        bool(release) == raw["published"]
        and source.active_library() == (raw["gate"]["library_id"] if raw["published"] else None),
        "archive disagrees with release status",
    )
    account = Archive(Path(binding) / "evocoder.sqlite3")
    cursor = read(root / "start.json")["cursor" if primary else "ledger_cursor"]
    with account.connect() as db:
        entries = [
            dict(r)
            for r in db.execute(
                "SELECT rowid,run_id,actual,reserved,state FROM calls WHERE rowid>? ORDER BY rowid", (cursor,)
            )
        ]
        exposed = {
            r[0] for r in db.execute("SELECT task_id FROM test_exposures WHERE experiment=?", (digest(p),))
        }
    ours = [r for r in entries if r["run_id"] in slots]
    require(
        ours
        and {r["run_id"] for r in ours} == set(slots)
        and all(r["run_id"] in slots for r in entries if r["rowid"] <= ours[-1]["rowid"]),
        "unattributed model calls",
    )
    scale = Decimal(10**9)
    billed = sum(r["actual"] or 0 for r in ours) / scale
    reserved = sum(r["reserved"] for r in ours if r["state"] == "reserved") / scale
    require(
        Decimal(str(raw["costs"]["cost_credits"])) == billed
        and Decimal(str(raw["costs"]["reserved_credits"])) == reserved
        and raw["costs"]["calls"] == len(ours),
        "incorrect shared-ledger cost",
    )
    call_counts = Counter(r["run_id"] for r in ours)
    by_run = defaultdict(list)
    for row in ours:
        by_run[row["run_id"]].append(row)
    limits = {rid: (rows[0]["rowid"], rows[-1]["rowid"]) for rid, rows in by_run.items()}
    options = Settings(_env_file=None, mode="mock", image=p["image"])
    checked, probe_cache = set(), {}

    def audit(rid):
        st = manual if rid in manual_ids else source
        run = st.run(rid)
        task = tasks[run["task_id"]]
        final = run["result"]
        events = st.events(rid)
        require(
            run["mode"] == "live"
            and run["purpose"] == "evaluate"
            and run["task_hash"] == slots[rid]["task_hash"] == digest(task.model_dump())
            and final["image_id"] == p["image"]
            and call_counts[rid] == final["cost"]["calls"],
            "wrong scored run or cost",
        )
        (route,) = [e["payload"] for e in events if e["kind"] == "SKILL_ROUTE"]
        require(route["applied_skill_id"] == run["skill_id"], "route differs from stored skill")
        responses = [e["payload"] for e in events if e["kind"] == "MODEL_RESPONSE"]
        require(
            any(c.get("expected_function") == "submit_action" for c in responses)
            and all(c["provider_model"].lower() == p["model"].lower() for c in responses),
            "wrong live solver",
        )
        checked.add(rid)
        return st, task, run, final, events, route

    for phase in ("dev",) if primary else ("pilot", "dev"):
        for row in raw[phase]:
            previous = 0
            for arm in row["order"]:
                a = row[arm]
                st, t, run, final, events, route = audit(a["run"])
                a["first_solver_request"] = next(
                    event["payload"]["request_body_hash"]
                    for event in events
                    if event["kind"] == "MODEL_RESPONSE"
                    and event["payload"]["expected_function"] == "submit_action"
                )
                require(previous < limits[a["run"]][0], "different actual arm order")
                previous = limits[a["run"]][1]
                require(
                    t.id == row["task"]
                    and (a["patch"], a["passed"], a["state"], a["applied"], a["route"], a["steps"])
                    == (
                        final["patch"],
                        final["passed"],
                        run["state"],
                        run["skill_id"],
                        route["decision"],
                        sum(e["kind"] == "ACTION" for e in events),
                    )
                    and call_counts[a["run"]] == a["calls"],
                    "trial differs from raw receipts",
                )
                if a["patch"]:
                    key = (t.id, a["patch"])
                    if key not in probe_cache:
                        probe_cache[key] = probe_patch(t, st.get_artifact(a["patch"]), options)
                    require(
                        a["probes"] == probe_cache[key],
                        "submitted patch does not reproduce its boundary outcomes",
                    )
    gate = raw.get("gate")
    if gate:
        saved = source.get_artifact(gate["receipt"])
        require(
            saved == {k: v for k, v in gate.items() if k != "receipt"} and gate["candidate"] == LEARNED,
            "unbound release",
        )
    inherited = {}
    if primary:
        require(raw["primary_receipt"] == primary["gate"]["receipt"], "unbound primary gate")
        original_gate = source.get_artifact(raw["primary_receipt"])
        inherited = {(r["task_id"], r["repeat"]): r for r in original_gate["rows"]}
        with source.connect() as db:
            (created,) = db.execute(
                "SELECT created FROM artifacts WHERE id=?", (raw["primary_receipt"],)
            ).fetchone()
        require(
            min(source.run(k)["created"] for k in slots if k not in manual_ids) >= created,
            "primary gate was not final",
        )
    for r in (gate["rows"] if gate else []) + raw["holdout"]:
        slot = r["task_id"], r["repeat"]
        if slot in inherited:
            require(inherited[slot] == r, "changed inherited release observation")
            continue
        for arm in ("baseline", "candidate"):
            rid = r[arm + "_run"]
            st, t, run, final, _, _ = audit(rid)
            require(
                t.id == r["task_id"]
                and (final["passed"], run["skill_id"]) == (r[arm + "_pass"], r[arm + "_applied_skill"]),
                "gate or holdout scores differ",
            )
            if t.split == "test":
                require(
                    release and gate and release[0] == gate["library_id"] and release[1] <= run["created"],
                    "holdout was attempted before activation",
                )
    recoveries = {r["failed_run"] for r in raw["transport"]}
    for rid in recoveries:
        st = manual if rid in manual_ids else source
        require(st.run(rid)["state"] == "ERROR", "transport replacement did not follow an error")
    require(checked | recoveries == set(slots), "unreported attempted slot")
    pilot_ids = [k for k, i in slots.items() if i["task"] in p.get("pilot", [])]
    dev_ids = [k for k, i in slots.items() if i["task"] in p["dev"]]
    if dev_ids and not primary:
        require(
            max(limits[k][1] for k in pilot_ids) < min(limits[k][0] for k in dev_ids),
            "development started prematurely",
        )
    require(
        exposed == {r["task_id"] for r in raw["holdout"]}
        and exposures
        == {r[a + "_run"] for r in raw["holdout"] for a in ("baseline", "candidate")}
        | {r["failed_run"] for r in raw["transport"] if r["phase"] == "holdout"},
        "undisclosed holdout exposure",
    )
    public = {
        k: raw[k]
        for k in ("protocol", "phase", "dev", "published", "curve", "holdout_exposures", "costs", "transport")
    }
    for k in ("primary_receipt", "added_decision") if primary else ("pilot", "pilot_gate"):
        public[k] = raw[k]
    public["billing_by_run"] = {
        rid: {
            "calls": len(rows),
            "charged_nanos": sum(r["actual"] or 0 for r in rows),
            "reserved_nanos": sum(r["reserved"] for r in rows if r["state"] == "reserved"),
        }
        for rid, rows in sorted(by_run.items())
    }
    public["support"] = (
        [] if not gate else sorted({r["task_id"] for r in gate["rows"] if r["split"] == "learn"})
    )
    if gate:
        known = read(Path("docs/results/optional-control.json"))
        references = set(known["skills"][0]["evidence_runs"])
        require(
            set(public["support"]) == {r["task"] for r in known["learning"] if r["run"] in references},
            "wrong learning support",
        )
    public["gate"] = (
        None
        if not gate
        else {
            "receipt": gate["receipt"],
            "library": gate["library_id"],
            "rows": [{k: r[k] for k in GATE_FIELDS} for r in gate["rows"]],
            "decision": gate_summary(gate["rows"], 2),
        }
    )
    public["holdout"] = [{k: r[k] for k in (*GATE_FIELDS, "family")} for r in raw["holdout"]]
    return public


def export(root, p):
    public = audit_archive(root, p)
    public["summary"] = summarize(public)
    verify(public, p)
    return public


def recalibrate(protocol, cases, image):
    image_id = subprocess.check_output(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image], text=True
    ).strip()
    settings = Settings(_env_file=None, mode="mock", image=image_id)
    expected = {r["task"]: r for r in protocol["calibration"]}

    def check_case(item):
        task, reference = item
        entry = expected[task.id]
        require(
            digest(task.model_dump()) == entry["snapshot"] and digest(reference) == entry["reference_patch"],
            "source or reference differs from registration",
        )
        for name, patch in (("broken", {}), ("fixed", reference)):
            require(probe_patch(task, patch, settings) == entry[name], "calibration differs on " + task.id)
        return task.id

    ids = bounded_map(check_case, cases, 4)
    require(set(ids) == set(expected) and len(ids) == len(expected), "partial calibration")
    print(
        f"Reproduced the broken and reference results on {len(ids)} registered tasks with zero model calls."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("verify", "export", "calibrate"))
    parser.add_argument("--root", type=Path, default=Path("experiments/falsy-transfer-001"))
    parser.add_argument("--registration", type=Path, default=REGISTRATION)
    parser.add_argument("--result", type=Path, default=PUBLIC)
    parser.add_argument("--image", default="python:3.12-slim")
    parser.add_argument("--cache", type=Path, default=Path("work/benchmark-repos"))
    args = parser.parse_args()
    protocol = read(args.registration)
    if args.command == "calibrate":
        recalibrate(protocol, [make_case(n, args.cache) for n in PILOT + DEV], args.image)
        raise SystemExit(0)
    if args.command == "export":
        data = export(args.root, protocol)
        dump(args.result, data)
    else:
        data = read(args.result)
    print(verify(data, protocol))
