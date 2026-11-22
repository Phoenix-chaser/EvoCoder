"""Learning-only selection between two independent grounded revisions of the same object skill."""

import argparse
from pathlib import Path

from evocoder.contracts import digest
from evocoder.errors import ProposalRejected
from evocoder.evolution import Evolution
from evocoder.jobs import bounded_map
from portfolio_routing import CHILD, PARENT
from report_click_procedure import read
from run_falsy_transfer import atomic_trial, require
from workflow_learning import PublicContractProposer
from workflow_study import FLAGS, LEARN, PUBLIC, ROOT, WAKES, WORKERS, session
from workflow_wake_extension import effective_learning

RULE = PUBLIC.with_name("workflow-candidate-selection.json")
CHOICE = ROOT / "candidate-selection.json"


def rule():
    return dict(
        base=digest(read(PUBLIC)),
        code=digest(Path(__file__).read_text()),
        stage="learn_only",
        candidate=effective_learning()["proposal"]["child"],
        inspect="Two new normal portfolio repairs on each of four learning tasks. One additional proposal may be generated "
        "only if the first revision still selects itself for a pure boolean or its submitted wake path fails. "
        "No candidate has seen a development program. The root/parent-only source screening is independent.",
        proposal="One sibling of the same original object specialization. Use a submitted old-child failure, old boolean "
        "and wake observations, and actual first-revision learning feedback. Supply the public job of the general skill "
        "and the intended division between scalar options and caller-owned instances. This is explicit routing supervision. "
        "GLM writes the procedure and conditions. Up to two calls for a settled invalid proposal, no hand editing.",
        select="If a grounded sibling is produced, run both versions fresh, two normal repetitions on all four learning "
        "programs, equal 15 solver actions. Pick it only for strictly fewer pure boolean selections, no loss of total wake "
        "passes and no loss of overall passes. All attempts must be graded. Otherwise retain the first proposal. This "
        "selects from learning data only. The chosen version and full gate driver must then be frozen before development.",
    )


class RoleAwareProposer(PublicContractProposer):
    def __init__(self, s, rows):
        super().__init__(s, rows)
        root = s.store.get_artifact(PARENT)
        self.descriptions = {k: root[k] for k in ("applicability", "exclusions")}
        self.targets = {
            r["run"]: (
                "value option belongs to the configuration skill or skip"
                if r["intent"]["task"] in FLAGS
                else "caller-owned collaborator; test observable use after restoring on a different consumer"
            )
            for r in rows
        }

    def request(self, schema, system, data, run_id):
        prompt = system + (
            "\nThis is a two-member library. Learning labels distinguish the value-config and caller-instance roles. "
            "A terminal pass does not make a wrong role assignment a correct route. The sample may refer to an "
            "object fixture that merely supplies the default while the caller-supplied option itself is scalar. "
            "Use the observed routes, submitted patches and public contracts to infer discriminating positive "
            "conditions, exclusions and verification steps for the object specialization. Keep valid caller-owned "
            "callbacks in scope. Draft the new content yourself. Cite the first candidate's actual learning observation as well."
        )
        return super().request(
            schema,
            prompt,
            {**data, "unchanged_peer_scope": self.descriptions, "learning_role_feedback": self.targets},
            run_id,
        )


def generate(s, refs):
    engine = Evolution(
        s.cfg,
        s.store,
        model=RoleAwareProposer(s, refs),
        feedback_policy="learn_outcome",
        proposal_policy="grounded_scope",
        grounding_tasks={t: s.new[t][0] for t in LEARN},
    )
    ids = [r["run"] for r in refs]
    e, _ = engine.freeze(ids, CHILD)
    while True:
        with s.store.connect() as db:
            attempts = [
                r[0]
                for r in db.execute(
                    "SELECT run_id FROM events WHERE kind='PROPOSAL_START' AND "
                    "json_extract(payload,'$.evidence_hash')=? ORDER BY rowid",
                    (e,),
                )
            ]
        for rid in attempts:
            events = s.store.events(rid)
            if events[-1]["kind"] == "PROPOSED":
                key = events[-1]["payload"]["skill_id"]
                art = s.store.get_artifact(key)
                receipt = next(x["payload"]["artifact"] for x in events if x["kind"] == "SUMMARY_VERIFIED")
                keep = set(ids[:3]) <= set(art["evidence_refs"]) and all(
                    art[k] != s.store.get_artifact(CHILD)[k] for k in ("steps", "applicability")
                )
                return dict(
                    child=key if keep else None,
                    proposed=key,
                    evidence=e,
                    receipt=receipt,
                    attempts=attempts,
                    refs=ids,
                    required=ids[:3],
                    corroborated=keep,
                )
            require(
                events[-1]["kind"] == "PROPOSAL_ERROR" and not s.store.costs([rid])["reserved_credits"],
                "unsettled proposal",
            )
        if len(attempts) >= 2:
            return dict(child=None, evidence=e, refs=ids, attempts=attempts, reason="two rejected proposals")
        try:
            engine.propose(ids, CHILD)
        except ProposalRejected:
            continue


def score(rows, key):
    return dict(
        boolean_misroutes=sum(r["selected"] == key for r in rows if r["intent"]["task"] in FLAGS),
        wake_pass=sum(r["passed"] is True for r in rows if r["intent"]["task"] in WAKES),
        total_pass=sum(r["passed"] is True for r in rows),
        complete=all(r["passed"] is not None for r in rows),
    )


def choose(first, alternate):
    return (
        first["complete"]
        and alternate["complete"]
        and alternate["boolean_misroutes"] < first["boolean_misroutes"]
        and alternate["wake_pass"] >= first["wake_pass"]
        and alternate["total_pass"] >= first["total_pass"]
    )


def run(s):
    learn = effective_learning()
    primary = learn["proposal"]["child"]
    require(
        read(RULE) == rule() and primary and not (ROOT / "panel.json").exists(),
        "development already started or registration differs",
    )
    if CHOICE.exists():
        return read(CHOICE)
    base = s.protocol

    def trials(label, keys):
        s.protocol = digest(dict(base=base, selection=digest(rule()), stage=label))
        jobs = [
            (t, n, k)
            for n in range(2)
            for i, t in enumerate(LEARN)
            for k in (keys if (i + n) % 2 == 0 else list(reversed(keys)))
        ]
        return bounded_map(lambda j: s.solve("learn", j[0], j[1], "revised", j[2]), jobs, WORKERS)

    try:
        audit = trials("initial-scope-audit", [primary])
        failure = next(
            (r for r in audit if r["intent"]["task"] in FLAGS and r["selected"] == primary and r["patch"]),
            None,
        )
        if not failure:
            failure = next(
                (
                    r
                    for r in audit
                    if r["intent"]["task"] in WAKES
                    and r["selected"] == primary
                    and r["passed"] is False
                    and r["public_feedback"]
                    and not r["public_feedback"]["passed"]
                ),
                None,
            )
        rev = None
        matched = []
        scores = {}
        chosen = primary
        if failure:
            flags = [read(ROOT / "rows" / (k + ".json")) for k in learn["flag_misroutes"]]
            flag = next((r for r in flags if r["passed"] is False), flags[0])
            wake = read(ROOT / "rows" / (learn["wake_failures"][0] + ".json"))
            refs = [flag, wake, failure]
            contrast = next(
                (
                    r
                    for r in audit
                    if r["intent"]["task"] in WAKES and r["passed"] and r["public_feedback"]["passed"]
                ),
                None,
            )
            if contrast:
                refs.append(contrast)
            rev = generate(s, refs)
            if rev["child"]:
                alt = rev["child"]
                matched = trials("matched-learning", [primary, alt])
                scores = {
                    k: score([r for r in matched if k in r["intent"]["pool"]], k) for k in (primary, alt)
                }
                if choose(scores[primary], scores[alt]):
                    chosen = alt
        result = dict(
            protocol=base,
            learning=digest(learn),
            rule=digest(rule()),
            primary=primary,
            selected=chosen,
            audit=[r["run"] for r in audit],
            trigger=failure["run"] if failure else None,
            proposal=rev,
            matched=[r["run"] for r in matched],
            scores=scores,
        )
        atomic_trial(CHOICE, result)
        print("candidate selection", chosen, scores, flush=True)
        return result
    finally:
        s.protocol = base


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["register", "run"])
    arg = parser.parse_args()
    if arg.action == "register":
        require(not (ROOT / "panel.json").exists() and not CHOICE.exists(), "candidate already evaluated")
        atomic_trial(RULE, rule())
        print("registered", digest(rule()))
    else:
        with session() as study:
            run(study)
