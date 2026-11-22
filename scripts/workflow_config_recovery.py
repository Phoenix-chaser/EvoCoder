"""Collect fresh, whole configuration blocks after a transport outage; preserve the original release receipt."""

import argparse
from pathlib import Path

from evocoder.comparison import frozen_knowledge
from evocoder.contracts import digest
from evocoder.jobs import bounded_map
from report_click_procedure import read
from run_falsy_transfer import atomic_trial, require
from workflow_gate import FREEZE, direct_protection, evaluate, registered, schedule
from workflow_study import PUBLIC, RESULT, ROOT, WORKERS, session

RULE = PUBLIC.with_name("workflow-config-recovery.json")
OUTPUT = RESULT.with_name("workflow-config-recovery.json")
PRIVATE = ROOT / "config-recovery"


def policy():
    return dict(
        study="prospective-whole-block-transport-recovery-v1",
        protocol=digest(read(PUBLIC)),
        panel=digest(read(FREEZE)),
        code=digest(Path(__file__).read_text()),
        cause="GLM TLS EOF during the original configuration panel.",
        trigger="After the original evaluator has closed, select only original configuration rounds containing "
        "at least one ungraded transport-error slot. Preserve every original paid result, hold and SQL receipt.",
        design="Run all four originally assigned config programs on all four arms for every affected round, "
        "including originally successful slots. Same frozen task, candidate, settings, order, solver budget "
        "and four workers. New protocol and phase in the paid intent. Up to two whole attempts per affected "
        "block, a second only if the first has a missing transport grade. Never retry a poor patch. "
        "Retain all observations; choose the first completely observed recovery block for the descriptive view.",
        decision="Report originally complete config blocks plus separately collected complete blocks, head to "
        "head with the original evidence. This diagnostic cannot activate a library or claim any test task.",
        original_rule="Every missing original terminal score stays unknown; the original formal release receipt is immutable.",
    )


def source_rows(h, original):
    by = {}
    for row in original["rows"]:
        i = row["intent"]
        if i["phase"] == "gate" and i["task"] in h["config"]:
            by[i["task"], i["repeat"], i["arm"]] = row
    needed = [
        n for n in range(7) if any(by[t, n, a]["passed"] is None for t in h["config"] for a in h["arms"][t])
    ]
    require(
        needed
        and not original["activation"]
        and original["gate"]["report"]["gate"]["complete"] is False
        and all(
            r["passed"] is not None
            for r in original["rows"]
            if r["intent"]["phase"] == "gate" and r["intent"]["task"] not in h["config"]
        ),
        "original collection does not match this addendum",
    )
    return by, needed


def transport_error(s, row):
    error = s.store.run(row["run"])["result"].get("error") or ""
    for name in ("ConnectError", "ConnectTimeout", "ReadError", "ReadTimeout", "RemoteProtocolError"):
        if error.startswith(name + ":"):
            return name
    return None


def trace_blocks(p, h, original, blocks):
    source, needed = source_rows(h, original)
    complete = {}
    require(set(str(n + 1) for n in needed) == set(blocks), "wrong recovery rounds")
    for n in needed:
        attempts = blocks[str(n + 1)]
        require(1 <= len(attempts) <= 2, "exceeded registered recovery limit")
        seq = [(t, a) for t, a in schedule(p, h, n) if t in h["config"]]
        for k, rows in enumerate(attempts):
            expected_protocol = digest(
                dict(registration=digest(read(RULE)), receipt=original["gate"]["receipt"], round=n, attempt=k)
            )
            require(
                n not in complete
                and [(r["intent"]["task"], r["intent"]["arm"]) for r in rows] == seq
                and len(rows) == 16,
                "partial assignment or opportunistic repeat",
            )
            for r in rows:
                i = r["intent"]
                before = source[i["task"], n, i["arm"]]["intent"]
                require(
                    r["run"] == digest(i)[:32]
                    and i == dict(before, phase="config_recovery", protocol=expected_protocol)
                    and r["run"] != digest(before)[:32],
                    "reused or unmatched repair slot",
                )
            if all(isinstance(r["passed"], bool) for r in rows):
                complete[n] = rows
        require(n in complete or len(attempts) == 2, "recovery collection incomplete")
    if set(complete) != set(needed):
        return dict(affected_rounds=[n + 1 for n in needed], complete_seven=False)
    from report_workflow_revision import panel_rows, revision_comparison

    all_rows = panel_rows(original["rows"], h)
    for n, rows in complete.items():
        grouped = {(r["intent"]["task"], r["intent"]["arm"]): r for r in rows}
        for obs in all_rows:
            if obs["repeat"] == n and obs["task"] in h["config"]:
                for a in h["arms"][obs["task"]]:
                    obs[a] = grouped[obs["task"], a]
    verdict = evaluate(p, h, all_rows)
    return dict(
        affected_rounds=[n + 1 for n in needed],
        complete_seven=True,
        selected_attempts={str(n + 1): len(blocks[str(n + 1)]) for n in needed},
        direct=direct_protection(all_rows, h["child"]),
        configuration={
            a: dict(tasks={t: v["tasks"][t] for t in h["config"]}, block=v["configuration_block"])
            for a, v in verdict["comparisons"].items()
        },
        previous_vs_revised={
            t: x for t, x in revision_comparison(all_rows, h["child"]).items() if t in h["config"]
        },
    )


def verify(d, p, h, original):
    from report_workflow_revision import add_cost, replay_manifest

    require(
        d["registration"] == digest(read(RULE))
        and read(RULE) == policy()
        and d["original"] == digest(original)
        and d["receipt"] == original["gate"]["receipt"]
        and d["holdout_exposures"] == 0
        and not d["release_transactions"],
        "changed original release or recovery registration",
    )
    require(d["comparison"] == trace_blocks(p, h, original, d["blocks"]), "incorrect descriptive panel")
    rows = [r for attempts in d["blocks"].values() for block in attempts for r in block]
    require(
        len({r["run"] for r in rows}) == len(rows)
        and not {r["run"] for r in rows}.intersection(r["run"] for r in original["rows"])
        and add_cost([r["cost"] for r in rows]) == d["billing"]
        and d["patch_replay"] == replay_manifest(rows),
        "incorrect recovery billing, duplicate observation or patched grade",
    )
    return d["comparison"]["complete_seven"]


def run(s):
    from report_workflow_revision import audit_live_row, replay_submissions

    h = registered(s)
    orig = read(RESULT)
    source, needed = source_rows(h, orig)
    require(
        read(RULE) == policy()
        and s.store.active_library() is None
        and frozen_knowledge(s.store) == h["knowledge"]
        and s.store.get_artifact(orig["gate"]["receipt"]) == orig["gate"]["report"],
        "original study changed",
    )
    require(
        all(transport_error(s, r) for r in source.values() if r["passed"] is None),
        "ungraded cause outside registration",
    )
    blocks = {}
    for n in needed:
        seq = [(t, a) for t, a in schedule(s.p, h, n) if t in h["config"]]
        attempts = []
        for k in range(2):
            s.protocol = digest(
                dict(registration=digest(read(RULE)), receipt=orig["gate"]["receipt"], round=n, attempt=k)
            )
            rows = bounded_map(
                lambda job: s.solve("config_recovery", job[0], n, job[1], h["child"]), seq, WORKERS
            )
            require(
                all(r["passed"] is not None or transport_error(s, r) for r in rows),
                "non-transport incident in new block",
            )
            atomic_trial(PRIVATE / f"block-{n + 1}-{k + 1}.json", rows)
            attempts.append(rows)
            if all(r["passed"] is not None for r in rows):
                break
        blocks[str(n + 1)] = attempts
    all_rows = [r for attempts in blocks.values() for block in attempts for r in block]
    for row in all_rows:
        audit_live_row(s, row)
    require(
        s.store.active_library() is None and frozen_knowledge(s.store) == h["knowledge"],
        "diagnostic changed a release",
    )
    with s.ledger.connect() as db:
        exposed = {r[0] for r in db.execute("SELECT task_id FROM test_exposures")} & set(
            s.p["holdout_hashes"]
        )
    with s.store.connect() as db:
        releases = list(
            db.execute("SELECT seq FROM library_releases WHERE evaluation_id=?", (orig["gate"]["receipt"],))
        )
    d = dict(
        schema=1,
        registration=digest(read(RULE)),
        original=digest(orig),
        receipt=orig["gate"]["receipt"],
        blocks=blocks,
        holdout_exposures=len(exposed),
        release_transactions=[r[0] for r in releases],
        comparison=trace_blocks(s.p, h, orig, blocks),
        billing=s.store.costs([r["run"] for r in all_rows]),
        patch_replay=replay_submissions(s, all_rows),
    )
    verify(d, s.p, h, orig)
    atomic_trial(OUTPUT, d)
    print(
        "diagnostic only",
        d["comparison"]["complete_seven"],
        d["comparison"]["affected_rounds"],
        d["billing"],
        flush=True,
    )


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("command", choices=["register", "run", "verify"])
    args = cli.parse_args()
    if args.command == "register":
        require(not (PRIVATE / "block-6-1.json").exists() and not OUTPUT.exists(), "recovery already started")
        atomic_trial(RULE, policy())
        print("registered", digest(policy()))
    elif args.command == "run":
        with session() as study:
            run(study)
    else:
        print(
            "verified whole-block diagnostic", verify(read(OUTPUT), read(PUBLIC), read(FREEZE), read(RESULT))
        )
