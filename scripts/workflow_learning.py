"""Revise an object specialization from independent learning runs, then screen callback sources."""

from evocoder.contracts import Skill, digest
from evocoder.errors import ProposalRejected
from evocoder.evolution import Evolution
from evocoder.jobs import bounded_map
from evocoder.model import Model
from evocoder.route_shape import public_test_shape
from portfolio_routing import CHILD, PARENT
from report_click_procedure import read
from run_falsy_transfer import atomic_trial, require
from workflow_study import FAMILIES, FLAGS, LEARN, ROOT, WAKES, WORKERS


def witnesses(rows):
    flags = [
        r
        for r in rows
        if r["intent"]["task"] in FLAGS
        and r["intent"]["arm"] == "previous"
        and r["selected"] == CHILD
        and r["patch"]
        and r["passed"] is not None
    ]
    wake = [
        r
        for r in rows
        if r["intent"]["task"] in WAKES
        and r["selected"] == CHILD
        and r["patch"]
        and r["passed"] is False
        and r["public_feedback"]
        and not r["public_feedback"]["passed"]
        and r["probes"]["public"]
        and not r["probes"]["resumed"]
    ]
    return flags, wake


def select(rows):
    flags, wake = witnesses(rows)
    if not flags or not wake:
        return []
    failed = [*wake, *flags]
    refs = [flags[0], wake[0]]
    for witness in list(refs):
        other = next(
            (
                r
                for r in rows
                if r["intent"]["task"] == witness["intent"]["task"]
                and r["passed"]
                and r["run"] != witness["run"]
                and r["patch"]
                and (
                    r["selected"] != CHILD if r["intent"]["task"] in FLAGS else r["public_feedback"]["passed"]
                )
            ),
            None,
        )
        if other:
            refs.append(other)
    seen = {r["intent"]["task"] for r in refs}
    for r in [*failed, *rows]:
        if r["intent"]["task"] not in seen and r["patch"] and r["public_feedback"]:
            refs.append(r)
            seen.add(r["intent"]["task"])
    return refs[:6]


class PublicContractProposer(Model):
    """Present public-contract replay results, bound to the exact learning patch and route."""

    def __init__(self, study, rows):
        super().__init__(study.cfg, study.store)
        self.observations = {}
        for r in rows:
            tid = r["intent"]["task"]
            t, _, program = study.new[tid]
            self.observations[r["run"]] = dict(
                contract=t.files["README.md"],
                issue=t.issue,
                shapes=public_test_shape(t),
                route=r["route"],
                router_reason=r["reason"],
                intervention=r["intent"]["fixed"],
                patch=r["patch"],
                public_program=program,
                independent_execution=r["public_feedback"],
            )
        self.roles = [r["run"] for r in rows[:2]]

    def request(self, schema, system, data, run_id):
        require(
            schema is Skill and set(self.observations) == set(data["allowed_evidence_refs"]),
            "incorrect learning projection",
        )
        prompt = system + (
            "\nReview both contrasting public contract observations in required_contrasts and cite both run IDs. "
            "Their results describe the submitted patches. A router selecting a specialized skill is an observation "
            "even when the final patch passed. Distinguish what the public assertion compares (a value or an instance), "
            "and compare first and later invocations, including fallback behavior. Infer your own reusable scope and "
            "ordered checks; no task names or test-specific fixes in the procedure. The semantic router sees only "
            "applicability and exclusions. Use compact fields within the solver memory budget."
        )
        self.store.event(
            run_id,
            "PUBLIC_CONTRACT_EVIDENCE",
            dict(observations=digest(self.observations), required=self.roles, prompt=digest(prompt)),
        )
        return super().request(
            schema,
            prompt,
            dict(data, public_contract_checks=self.observations, required_contrasts=self.roles),
            run_id,
        )


def proposal(s, refs):
    model = PublicContractProposer(s, refs)
    engine = Evolution(
        s.cfg,
        s.store,
        model=model,
        feedback_policy="learn_outcome",
        proposal_policy="grounded_scope",
        grounding_tasks={t: s.new[t][0] for t in LEARN},
    )
    ids = [r["run"] for r in refs]
    frozen, _ = engine.freeze(ids, CHILD)
    while True:
        with s.store.connect() as db:
            attempts = [
                r[0]
                for r in db.execute(
                    "SELECT run_id FROM events WHERE kind='PROPOSAL_START' "
                    "AND json_extract(payload,'$.evidence_hash')=? ORDER BY rowid",
                    (frozen,),
                )
            ]
        for rid in attempts:
            ev = s.store.events(rid)
            if ev[-1]["kind"] == "PROPOSED":
                child = ev[-1]["payload"]["skill_id"]
                receipt = next(e["payload"]["artifact"] for e in ev if e["kind"] == "SUMMARY_VERIFIED")
                artifact = s.store.get_artifact(child)
                corroborated = (
                    set(ids[:2]) <= set(artifact["evidence_refs"])
                    and artifact["parent"] == CHILD
                    and all(artifact[k] != s.store.get_artifact(CHILD)[k] for k in ("applicability", "steps"))
                )
                return dict(
                    child=child if corroborated else None,
                    proposed=child,
                    evidence=frozen,
                    receipt=receipt,
                    attempts=attempts,
                    refs=ids,
                    required=ids[:2],
                    corroborated=corroborated,
                )
            require(
                ev[-1]["kind"] == "PROPOSAL_ERROR" and not s.store.costs([rid])["reserved_credits"],
                "unsettled proposal cannot be repeated",
            )
        if len(attempts) >= 2:
            return dict(
                child=None, evidence=frozen, attempts=attempts, refs=ids, reason="two rejected proposals"
            )
        try:
            engine.propose(ids, CHILD)
        except ProposalRejected:
            continue


def learn(s):
    path = ROOT / "learning.json"
    if path.exists():
        return read(path)

    def run(jobs):
        return bounded_map(lambda j: s.solve("learn", *j), jobs, WORKERS)

    rows = run(
        [
            (t, n, a)
            for n in range(4)
            for t in LEARN
            for a in (("previous", "fixed") if t in WAKES and n < 2 else ("previous",))
        ]
    )
    for n in range(4, 8):
        flags, wake = witnesses(rows)
        if flags and wake:
            break
        jobs = [(t, n, "previous") for t in FLAGS] if not flags else []
        if not wake:
            jobs.extend((t, n, a) for t in WAKES for a in ("previous", "fixed"))
        rows.extend(run(jobs))
    chosen = select(rows)
    rev = (
        proposal(s, chosen)
        if chosen
        else dict(child=None, evidence=None, attempts=[], refs=[], reason="missing learning contrast")
    )
    flags, wake = witnesses(rows)
    result = dict(
        protocol=s.protocol,
        runs=[r["run"] for r in rows],
        flag_misroutes=[r["run"] for r in flags],
        wake_failures=[r["run"] for r in wake],
        proposal=rev,
    )
    atomic_trial(path, result)
    print("learning", len(rows), len(flags), len(wake), rev, flush=True)
    return result


def missed(r):
    probes = r["probes"] or {}
    return bool(
        r["passed"] is False
        and r["patch"]
        and probes.get("public")
        and probes.get("controls")
        and any(not val for k, val in probes.items() if k not in {"public", "controls"})
    )


def screen(s):
    learn_receipt = read(ROOT / "learning.json")
    require(learn_receipt["protocol"] == s.protocol, "independent learning must finish first")
    families = []
    selected = None
    for family in FAMILIES:
        rows = bounded_map(
            lambda j: s.solve("screen", j[0], j[1], "parent"),
            [(t, n) for n in range(5) for t in family],
            WORKERS,
        )
        counts = {t: sum(missed(r) for r in rows if r["intent"]["task"] == t) for t in family}
        eligible = all(x >= 3 for x in counts.values()) and all(r["passed"] is not None for r in rows)
        families.append(
            dict(
                implementations=list(family), misses=counts, runs=[r["run"] for r in rows], eligible=eligible
            )
        )
        if eligible:
            selected = family[1]
            break
    result = dict(
        protocol=s.protocol,
        learning=digest(learn_receipt),
        families=families,
        selected=selected,
        parent=PARENT,
    )
    atomic_trial(ROOT / "screen.json", result)
    print("screen", result, flush=True)
    return result
