"""Frozen public-execution retrieval; no evaluator outputs enter the pool."""

from __future__ import annotations

import math
import re
from collections import Counter

from evocoder.contracts import Task, canonical, digest
from evocoder.store import Store


def public_document(store: Store, run_id: str, mode: str) -> dict:
    run = store.run(run_id)
    if (
        run["mode"] != mode
        or run["split"] != "learn"
        or run["purpose"] != "solve"
        or run["state"] not in {"COMPLETE", "FAILED", "EXHAUSTED"}
    ):
        raise ValueError("experience requires completed same-mode learn/solve runs")
    events = store.events(run_id)
    task = next(e["payload"]["task"] for e in events if e["kind"] == "START")
    observations = []
    for e in events:
        if e["kind"] not in {"OBSERVATION", "PUBLIC_VERIFY"}:
            continue
        action, result = e["payload"].get("action", {}), e["payload"].get("observation", {})
        if action.get("tool") in {"test", "command"}:
            observations.append(
                {
                    "tool": action["tool"],
                    "exit_code": result.get("exit_code"),
                    "output": result.get("output", "")[:1800],
                }
            )
    patch = next((e["payload"].get("diff", "") for e in reversed(events) if e["kind"] == "PATCH"), "")
    return {
        "run_id": run_id,
        "issue": task["issue"],
        "observations": observations[-2:],
        "patch_diff": patch[:4000],
    }


def freeze_pool(store: Store, run_ids: list[str], mode: str) -> str:
    if not run_ids or len(run_ids) != len(set(run_ids)):
        raise ValueError("experience pool requires unique learning runs")
    return store.artifact(
        "experience_pool",
        {
            "kind": "experience_pool",
            "mode": mode,
            "documents": [public_document(store, r, mode) for r in run_ids],
        },
    )


def retrieve(store: Store, pool_id: str, task: Task, mode: str, byte_limit: int, top_k=3) -> dict:
    pool = store.get_artifact(pool_id)
    if pool.get("kind") != "experience_pool" or pool.get("mode") != mode:
        raise ValueError("invalid experience pool or mode")
    documents = pool["documents"]
    for doc in documents:
        # Bind the frozen content to completed public executions, not just arbitrary artifact text.
        if public_document(store, doc["run_id"], mode) != doc:
            raise ValueError("experience document differs from its public execution")
        if store.run(doc["run_id"])["task_id"] == task.id and task.split != "learn":
            raise ValueError("target task appears in historical experience")

    def tokenize(text):
        return Counter(re.findall(r"[a-z][a-z0-9_]{1,}", text.lower()))

    counts = [tokenize(d["issue"]) for d in documents]
    df = Counter(term for count in counts for term in count)
    query = tokenize(task.issue)
    scores = []
    for doc, count in zip(documents, counts, strict=True):
        score = sum(
            (1 + math.log(count[t])) * (math.log((1 + len(documents)) / (1 + df[t])) + 1)
            for t in query
            if t in count
        )
        if score > 0:
            scores.append((-score, doc["run_id"], doc))
    items = []
    for _, _, doc in sorted(scores)[:top_k]:
        item = {**doc, "issue": doc["issue"][:1800], "patch_diff": doc["patch_diff"][:2200]}
        if len(canonical({"historical_experience": [*items, item]}).encode()) <= byte_limit:
            items.append(item)
    return {
        "pool_id": pool_id,
        "query_hash": digest(task.issue),
        "documents": items,
        "retrieval": "issue_tf_idf_v1",
        "bytes": len(canonical({"historical_experience": items}).encode()),
    }
