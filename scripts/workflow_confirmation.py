"""One independently registered, complete configuration confirmation of the frozen workflow library."""

import argparse
import time
from contextlib import contextmanager
from pathlib import Path

import httpx

from evocoder.comparison import clone_state, frozen_knowledge
from evocoder.contracts import canonical, digest
from evocoder.jobs import bounded_map
from evocoder.store import Store
from portfolio_routing import PARENT
from report_click_procedure import Archive, read
from report_workflow_revision import (
    add_cost,
    audit_live_row,
    panel_rows,
    replay_manifest,
    replay_submissions,
    revision_comparison,
)
from run_falsy_transfer import atomic_trial, require
from run_optional_control import curve_point
from workflow_config_recovery import transport_error
from workflow_gate import FREEZE, PRINT, evaluate, schedule
from workflow_study import PUBLIC, RESULT, ROOT, WORKERS, Study, session

REG = PUBLIC.with_name("workflow-confirmation.json")
LOCK = PUBLIC.with_name("workflow-confirmation-lock.json")
OUT = RESULT.with_name("workflow-confirmation.json")
DIR = Path("experiments/workflow-confirmation-001")


def freeze():
    policy, p, h, original = read(REG), read(PUBLIC), read(FREEZE), read(RESULT)
    require(
        digest(policy) == "cc0a46e4e82056f5ae1eb317f6188a905fd8974f882418a4a56df255cc3a6025",
        "registration anchor",
    )
    require(
        policy["base"] == digest(p)
        and policy["panel"] == digest(h)
        and policy["original_receipt"] == original["gate"]["receipt"]
        and policy["candidate"] == h["child"]
        and not original["activation"]
        and not original["holdout_exposures"]
        and not original["gate"]["report"]["gate"]["complete"],
        "confirmation eligibility differs",
    )
    original_rows = panel_rows(original["rows"], h)
    reuse = [r for r in original_rows if r["task"] not in h["config"]]
    require(
        all(r[a]["passed"] is not None for r in reuse for a in h["arms"][r["task"]]),
        "incomplete non-configuration evidence",
    )
    code = {
        f: digest(Path(__file__).with_name(f).read_text())
        for f in (
            "workflow_confirmation.py",
            "workflow_config_recovery.py",
            "workflow_gate.py",
            "workflow_study.py",
        )
    }
    fixed = dict(
        policy=digest(policy),
        code=code,
        original_archive=digest(original),
        original_receipt=original["gate"]["receipt"],
        unchanged_nonconfig=digest(reuse),
        candidate=h["child"],
        expected_config_solves=112,
        order="Identical original counterbalanced config order. Fresh protocol and call identities per block.",
        held_snapshots=digest(p["holdout_hashes"]),
    )
    atomic_trial(LOCK, fixed)
    atomic_trial(DIR / "lock.json", fixed)
    print("confirmation frozen", digest(fixed), flush=True)


class Confirmation(Study):
    def activation(self):
        a = read(DIR / "activation.json")
        report = self.store.get_artifact(a["receipt"])
        expected = sorted([PARENT, read(LOCK)["candidate"]])
        require(
            a["protocol"] == self.protocol == report["protocol"]
            and report["gate"]["passed"] is True
            and a["library"] == self.store.active_library() == report["library_id"]
            and self.store.library_members(a["library"], mode="live", require_published=True)
            == a["members"]
            == expected
            and a["knowledge"] == frozen_knowledge(self.store)
            and a["transaction"]["evaluation_id"] == a["receipt"],
            "held-out access requires this confirmation's CAS release",
        )
        return a


@contextmanager
def campaign():
    with session() as old:
        h = read(FREEZE)
        lock = read(LOCK)
        original = read(RESULT)
        require(
            lock == read(DIR / "lock.json")
            and lock["policy"] == digest(read(REG))
            and lock["original_archive"] == digest(original)
            and lock["original_receipt"] == original["gate"]["receipt"]
            and all(digest(Path(__file__).with_name(f).read_text()) == d for f, d in lock["code"].items()),
            "confirmation drift",
        )
        state = DIR / "state"
        if state.exists():
            db = Store(state, ledger=old.ledger)
        else:
            require(
                old.store.active_library() is None and frozen_knowledge(old.store) == h["knowledge"],
                "original deployment diverged",
            )
            with old.ledger.connect() as conn:
                exposures = {r[0] for r in conn.execute("SELECT task_id FROM test_exposures")}
                require(
                    not exposures.intersection(old.p["holdout_hashes"]), "test exposed before confirmation"
                )
                cursor = conn.execute("SELECT coalesce(max(rowid),0) FROM calls").fetchone()[0]
            atomic_trial(DIR / "anchor.json", dict(protocol=digest(lock), cursor=cursor))
            db = clone_state(Archive(ROOT / "state/evocoder.sqlite3"), state, old.ledger)
        s = Confirmation(old.p, old.cfg, old.ledger, db)
        s.protocol = digest(lock)
        yield s, h, original


def healthy(cfg):
    for n in range(4):
        try:
            # HEAD reaches TLS/HTTP without a credential, model request or ledger reservation.
            with httpx.Client(timeout=6, follow_redirects=False) as client:
                response = client.head(cfg.base_url.rstrip("/") + "/chat/completions")
            if response.status_code < 500:
                return
        except httpx.TransportError:
            pass
        if n < 3:
            time.sleep(5)
    raise RuntimeError("provider transport health check failed before dispatch; resume this original batch")


def collect(s, h, n):
    require(
        0 <= n < 7 and frozen_knowledge(s.store) == h["knowledge"] and s.store.active_library() is None,
        "wrong confirmation deployment",
    )
    if n:
        require((DIR / f"round-{n - 1}.json").exists(), "previous complete block missing")
    base = s.protocol
    path = DIR / f"round-{n}.json"
    if path.exists():
        return read(path)
    seq = [(t, a) for t, a in schedule(s.p, h, n) if t in h["config"]]
    attempts = []
    errors = {}
    try:
        for k in range(2):
            healthy(s.cfg)
            s.protocol = digest(dict(confirmation=base, round=n, attempt=k))
            rows = bounded_map(
                lambda job: s.solve("confirm_config", job[0], n, job[1], h["child"]), seq, WORKERS
            )
            attempts.append(rows)
            unresolved = [r for r in rows if r["passed"] is None]
            for r in unresolved:
                errors[r["run"]] = transport_error(s, r) or "other"
            if not unresolved or any(errors[r["run"]] == "other" for r in unresolved):
                break
        used = len(attempts) - 1
        result = dict(confirmation=base, round=n, attempts=attempts, chosen=used, errors=errors)
        atomic_trial(path, result)
        print(
            "confirmation config",
            n + 1,
            "attempts",
            len(attempts),
            "grades",
            sum(r["passed"] is not None for r in attempts[used]),
            flush=True,
        )
        return result
    finally:
        s.protocol = base


def observed_veto(p, h, original_rows, child):
    reasons = []
    for t in h["config"]:
        local = [r for r in original_rows if r["task"] == t]
        for base in ("bare", "parent"):
            pairs = [
                (r[base], r["revised"])
                for r in local
                if r[base]["passed"] is not None and r["revised"]["passed"] is not None
            ]
            changes = [
                int(c["passed"]) - int(b["passed"])
                for b, c in pairs
                if b["first_solver_request"] != c["first_solver_request"]
            ]
            if (
                sum(changes) < 0
                or sum(b["passed"] and not c["passed"] for b, c in pairs) >= p["noise"]["raw_loss_veto"]
            ):
                reasons.append([t, base])
        if t == PRINT:
            pairs = [
                (r["previous"], r["revised"])
                for r in local
                if all(r[a]["passed"] is not None for a in ("previous", "revised"))
            ]
            from portfolio_routing import CHILD

            if sum(c["selected"] == child for _, c in pairs) > sum(
                b["selected"] == CHILD for b, _ in pairs
            ) or sum((c["probes"] or {}).get("controls") is True for _, c in pairs) < sum(
                (b["probes"] or {}).get("controls") is True for b, _ in pairs
            ):
                reasons.append([t, "previous"])
    return reasons


def matrix(p, h, original, blocks, lock):
    source = panel_rows(original["rows"], h)
    unchanged = [r for r in source if r["task"] not in h["config"]]
    require(
        digest(unchanged) == lock["unchanged_nonconfig"] and len(blocks) == 7,
        "changed object or support evidence",
    )
    seen = {r["run"] for r in original["rows"]}
    extra = []
    chosen = {}
    for n, block in enumerate(blocks):
        seq = [(t, a) for t, a in schedule(p, h, n) if t in h["config"]]
        require(
            block["confirmation"] == digest(lock)
            and block["round"] == n
            and 1 <= len(block["attempts"]) <= 2
            and block["chosen"] == len(block["attempts"]) - 1,
            "different block allocation",
        )
        for k, rows in enumerate(block["attempts"]):
            require(
                [(r["intent"]["task"], r["intent"]["arm"]) for r in rows] == seq and len(rows) == 16,
                "not a whole block",
            )
            rid_protocol = digest(dict(confirmation=digest(lock), round=n, attempt=k))
            for r in rows:
                i = r["intent"]
                before = next(x for x in source if x["task"] == i["task"] and x["repeat"] == n)[i["arm"]]
                require(
                    i == dict(before["intent"], protocol=rid_protocol, phase="confirm_config")
                    and r["run"] == digest(i)[:32]
                    and r["run"] not in seen,
                    "foreign or reused paid slot",
                )
                seen.add(r["run"])
                extra.append(r)
                if k == block["chosen"]:
                    chosen[i["task"], n, i["arm"]] = r
            if k < block["chosen"]:
                unknown = [r for r in rows if r["passed"] is None]
                require(
                    unknown
                    and all(
                        block["errors"].get(r["run"])
                        in (
                            "ConnectError",
                            "ConnectTimeout",
                            "ReadError",
                            "ReadTimeout",
                            "RemoteProtocolError",
                        )
                        for r in unknown
                    ),
                    "repeat without a transport interruption",
                )
        require(
            set(block["errors"]) == {r["run"] for a in block["attempts"] for r in a if r["passed"] is None},
            "different missing outcomes",
        )
    combined = []
    for r in source:
        t, n = r["task"], r["repeat"]
        combined.append(
            dict(task=t, repeat=n, **{a: chosen[t, n, a] for a in h["arms"][t]}) if t in h["config"] else r
        )
    decision = evaluate(p, h, combined)
    reasons = observed_veto(p, h, source, h["child"])
    decision["original_observed_veto"] = reasons
    decision["passed"] = decision["passed"] and not reasons
    return combined, extra, decision


def receipts(s, rows):
    anchor = read(DIR / "anchor.json")["cursor"]
    for r in rows:
        audit_live_row(s, r)
        rid = r["run"]
        with s.ledger.connect() as db:
            paid = list(db.execute("SELECT rowid,id,state FROM calls WHERE run_id=?", (rid,)))
        ev = s.store.events(rid)
        replies = [e["payload"] for e in ev if e["kind"] == "MODEL_RESPONSE"]
        require(
            paid
            and min(x["rowid"] for x in paid) > anchor
            and len(paid) == r["cost"]["calls"]
            and {x["id"] for x in paid if x["state"] == "settled"} == {x["call_id"] for x in replies}
            and all(x["provider_model"].lower() == s.cfg.model.lower() for x in replies),
            "unbound provider/billing evidence",
        )


def decide(s, h, original):
    lock = read(LOCK)
    blocks = [read(DIR / f"round-{n}.json") for n in range(7)]
    combined, extra, decision = matrix(s.p, h, original, blocks, lock)
    receipts(s, extra)
    members = sorted([PARENT, h["child"]])
    key = s.store.artifact(
        "skill_library",
        dict(schema=2, kind="skill_library", mode="live", members=members, route_context="public_shape"),
    )
    record = dict(
        schema=2,
        kind="library_evaluation",
        mode="live",
        candidate=h["child"],
        protocol=s.protocol,
        original_receipt=lock["original_receipt"],
        baseline_library=None,
        baseline_members=[],
        members=members,
        library_id=key,
        observations=digest(combined),
        fresh_attempts=digest(blocks),
        gate=decision,
    )
    rid = s.store.artifact("library_evaluation", record)
    with s.store.connect(write=True) as db:
        db.execute(
            "INSERT OR IGNORE INTO library_evaluations VALUES(?,?,?,?,?)",
            (rid, key, None, canonical(record), int(decision["passed"])),
        )
        require(
            tuple(db.execute("SELECT report,passed FROM library_evaluations WHERE id=?", (rid,)).fetchone())
            == (canonical(record), int(decision["passed"])),
            "different SQL confirmation",
        )
    atomic_trial(DIR / "gate.json", dict(receipt=rid, report=record))
    if decision["passed"]:
        if s.store.active_library() is None:
            require(frozen_knowledge(s.store) == h["knowledge"], "state drift before CAS")
            s.store.activate_library(key, rid, expected=None, mode="live")
        with s.store.connect() as db:
            tx = dict(
                db.execute(
                    "SELECT seq,before_id,after_id,evaluation_id FROM library_releases WHERE after_id=? AND evaluation_id=?",
                    (key, rid),
                ).fetchone()
            )
        require(tx["before_id"] is None and tx["after_id"] == key, "unexpected deployment transition")
        atomic_trial(
            DIR / "activation.json",
            dict(
                protocol=s.protocol,
                library=key,
                members=members,
                receipt=rid,
                transaction=tx,
                knowledge=frozen_knowledge(s.store),
            ),
        )
        s.activation()
    else:
        require(s.store.active_library() is None, "rejected library active")
    print("confirmation decision", decision["passed"], rid, flush=True)


def held(s, h):
    s.activation()
    groups = []
    for f, ids in s.p["holdout"].items():
        healthy(s.cfg)
        jobs = [
            (t, a)
            for i, t in enumerate(ids)
            for a in (("bare", "revised") if i % 2 == 0 else ("revised", "bare"))
        ]
        rows = bounded_map(lambda j: s.solve("holdout", j[0], 0, j[1], h["child"]), jobs, WORKERS)
        receipts(s, rows)
        groups.append(dict(family=f, rows=rows))
        atomic_trial(DIR / ("test-" + f + ".json"), groups[-1])
    s.activation()
    return groups


def diversity(p, activation, groups):
    if not activation:
        require(not groups, "holdout opened without separate live receipt")
        return [], []
    require([g["family"] for g in groups] == list(p["holdout"]), "incomplete test strata")
    pairs = []
    curve = []
    for group in groups:
        family = group["family"]
        local = []
        rows = group["rows"]
        require(len(rows) == 20, "wrong holdout block")
        for t in p["holdout"][family]:
            arms = {r["intent"]["arm"]: r for r in rows if r["intent"]["task"] == t}
            for name in ("bare", "revised"):
                r = arms[name]
                i = r["intent"]
                require(
                    r["run"] == digest(i)[:32]
                    and i["protocol"] == activation["protocol"]
                    and i["snapshot"] == p["holdout_hashes"][t]
                    and i["phase"] == "holdout"
                    and i["purpose"] == "evaluate"
                    and i["repeat"] == 0
                    and not i["fixed"]
                    and i["pool"] == ([] if name == "bare" else activation["members"])
                    and i["library"] == (activation["library"] if name == "revised" else None),
                    "wrong holdout treatment",
                )
            local.append(
                dict(
                    task=t,
                    family=family,
                    baseline_run=arms["bare"]["run"],
                    candidate_run=arms["revised"]["run"],
                    baseline_pass=arms["bare"]["passed"],
                    candidate_pass=arms["revised"]["passed"],
                )
            )
        pairs.extend(local)
        if all(r[c] is not None for r in pairs for c in ("baseline_pass", "candidate_pass")):
            curve.append(curve_point(pairs, family, local))
    return pairs, curve if len(curve) == 3 else []


def verify(d):
    p, h, lock, orig = read(PUBLIC), read(FREEZE), read(LOCK), read(RESULT)
    require(
        lock["policy"] == digest(read(REG))
        and lock["original_archive"] == digest(orig) == d["original_archive"]
        and d["protocol"] == digest(lock)
        and all(digest(Path(__file__).with_name(f).read_text()) == v for f, v in lock["code"].items()),
        "incorrect public confirmation registration",
    )
    combined, extra, verdict = matrix(p, h, orig, d["blocks"], lock)
    record = d["gate"]["report"]
    require(
        record["gate"] == verdict
        and record["protocol"] == d["protocol"]
        and record["original_receipt"] == orig["gate"]["receipt"]
        and record["observations"] == digest(combined)
        and record["fresh_attempts"] == digest(d["blocks"])
        and d["gate"]["receipt"] == digest(record)
        and record["members"] == sorted([PARENT, h["child"]])
        and record["library_id"]
        == digest(
            dict(
                schema=2,
                kind="skill_library",
                mode="live",
                members=record["members"],
                route_context="public_shape",
            )
        )
        and d["revision_comparison"] == revision_comparison(combined, h["child"]),
        "invalid new evaluation receipt",
    )
    a = d["activation"]
    require(
        bool(a) == verdict["passed"] and d["release_transactions"] == ([a["transaction"]] if a else []),
        "different activation",
    )
    if a:
        require(
            a["protocol"] == d["protocol"]
            and a["library"] == record["library_id"] == a["transaction"]["after_id"]
            and a["receipt"] == d["gate"]["receipt"] == a["transaction"]["evaluation_id"]
            and a["transaction"]["before_id"] is None,
            "wrong CAS",
        )
    pairs, curve = diversity(p, a, d["test_groups"])
    require(
        pairs == d["holdout"] and curve == d["curve"] and d["exposures"] == (30 if a else 0),
        "wrong measured diversity or claims",
    )
    tests = [r for g in d["test_groups"] for r in g["rows"]]
    require(
        len({r["run"] for r in [*extra, *tests]}) == len(extra) + len(tests)
        and add_cost([r["cost"] for r in extra + tests]) == d["billing"]
        and replay_manifest(extra + tests) == d["patch_replay"],
        "billing or patch replay mismatch",
    )
    return dict(
        passed=verdict["passed"],
        fresh_config=len(extra),
        held_solves=len(tests),
        exposures=d["exposures"],
        billing=d["billing"],
    )


def export(s, h, original):
    blocks = [read(DIR / f"round-{n}.json") for n in range(7)]
    combined, extra, _ = matrix(s.p, h, original, blocks, read(LOCK))
    gate = read(DIR / "gate.json")
    a = s.activation() if (DIR / "activation.json").exists() else None
    with s.store.connect() as db:
        sql = db.execute(
            "SELECT report,passed FROM library_evaluations WHERE id=?", (gate["receipt"],)
        ).fetchone()
        require(
            tuple(sql) == (canonical(gate["report"]), int(gate["report"]["gate"]["passed"])),
            "SQL verdict changed",
        )
        tx = [
            dict(r)
            for r in db.execute(
                "SELECT seq,before_id,after_id,evaluation_id FROM library_releases WHERE evaluation_id=?",
                (gate["receipt"],),
            )
        ]
    groups = [read(DIR / ("test-" + f + ".json")) for f in s.p["holdout"]] if a else []
    pairs, curve = diversity(s.p, a, groups)
    testrows = [r for g in groups for r in g["rows"]]
    with s.ledger.connect() as db:
        claims = {
            r["task_id"]: r["task_hash"]
            for r in db.execute(
                "SELECT task_id,task_hash FROM test_exposures WHERE experiment=?", (s.protocol,)
            )
        }
    require(
        claims == (s.p["holdout_hashes"] if a else {})
        and s.store.active_library() == (a["library"] if a else None),
        "wrong deployment or exposures",
    )
    receipts(s, extra + testrows)
    d = dict(
        schema=1,
        protocol=s.protocol,
        original_archive=digest(original),
        blocks=blocks,
        gate=gate,
        activation=a,
        release_transactions=tx,
        test_groups=groups,
        holdout=pairs,
        curve=curve,
        exposures=len(claims),
        revision_comparison=revision_comparison(combined, h["child"]),
        billing=s.store.costs([r["run"] for r in extra + testrows]),
        patch_replay=replay_submissions(s, extra + testrows),
    )
    summary = verify(d)
    atomic_trial(OUT, d)
    print("confirmation closed", summary, flush=True)


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("command", choices=["freeze", "round", "decide", "holdout", "export", "verify"])
    cli.add_argument("--round", type=int, choices=range(1, 8))
    args = cli.parse_args()
    if args.command == "freeze":
        freeze()
    elif args.command == "verify":
        print(verify(read(OUT)))
    else:
        with campaign() as (study, panel, original):
            if args.command == "round":
                require(args.round is not None, "supply round")
                collect(study, panel, args.round - 1)
            elif args.command == "decide":
                decide(study, panel, original)
            elif args.command == "holdout":
                held(study, panel)
            else:
                export(study, panel, original)
