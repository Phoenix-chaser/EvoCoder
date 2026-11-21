"""Paired task means and cluster bootstrap; repeated solves are not independent tasks."""

from __future__ import annotations

import random
from collections import defaultdict

ARMS = ("A", "B", "C", "D")


def summarize(
    rows: list[dict],
    expected_tasks: list[str],
    repeats: int,
    *,
    seed=20261117,
    bootstrap_samples=4000,
    cost_unit="usd",
) -> dict:
    if repeats < 1 or len(set(expected_tasks)) != len(expected_tasks):
        raise ValueError("positive repeats and unique task IDs required")
    if cost_unit not in {"usd", "credits"} or any(r.get("cost_unit", "usd") != cost_unit for r in rows):
        raise ValueError("evaluation cannot mix accounting units")
    cost_key = "cost_" + cost_unit
    cells = {}
    for row in rows:
        key = (row["task_id"], row["repeat"], row["arm"])
        if key in cells:
            raise ValueError("duplicate paired evaluation cell")
        if row["arm"] not in ARMS or row["task_id"] not in expected_tasks or not 0 <= row["repeat"] < repeats:
            raise ValueError("unexpected evaluation cell")
        cells[key] = row
    expected = {(task, repeat, arm) for task in expected_tasks for repeat in range(repeats) for arm in ARMS}
    complete = set(cells) == expected
    paired = [
        task for task in expected_tasks if all((task, r, a) in cells for r in range(repeats) for a in ARMS)
    ]
    scores = {}
    for arm in ARMS:
        actual = [r for r in rows if r["arm"] == arm]
        passed = sum(bool(r["passed"]) for r in actual)
        cost = sum(r[cost_key] for r in actual)
        scores[arm] = {
            "passed": passed,
            "completed_cells": len(actual),
            "planned_cells": len(expected_tasks) * repeats,
            "success_rate_observed": passed / len(actual) if actual else None,
            cost_key: cost,
            "cost_per_success_" + cost_unit: cost / passed if passed else None,
            "execution_errors": sum(r["state"] in {"ERROR", "INTERRUPTED"} for r in actual),
        }
    differences = {}
    paired_task_rows = []
    for task in paired:
        if len({cells[(task, r, a)]["source_group"] for r in range(repeats) for a in ARMS}) != 1:
            raise ValueError("paired task source group changed across arms or repeats")
        means = {a: sum(bool(cells[(task, r, a)]["passed"]) for r in range(repeats)) / repeats for a in ARMS}
        group = cells[(task, 0, "A")]["source_group"]
        paired_task_rows.append({"task_id": task, "source_group": group, "means": means})
    for other in ("A", "B", "C"):
        clusters = defaultdict(list)
        for row in paired_task_rows:
            clusters[row["source_group"]].append(row["means"]["D"] - row["means"][other])
        cluster_means = [sum(v) / len(v) for _, v in sorted(clusters.items())]
        estimate = sum(cluster_means) / len(cluster_means) if cluster_means else None
        interval = None
        if len(cluster_means) >= 2:
            rng = random.Random(seed)
            samples = sorted(
                sum(rng.choices(cluster_means, k=len(cluster_means))) / len(cluster_means)
                for _ in range(bootstrap_samples)
            )
            interval = [
                samples[int(0.025 * bootstrap_samples)],
                samples[min(bootstrap_samples - 1, int(0.975 * bootstrap_samples))],
            ]
        differences["D_vs_" + other] = {
            "paired_cluster_macro_difference": estimate,
            "ci95": interval,
            "clusters": len(cluster_means),
            "improved_tasks": [r["task_id"] for r in paired_task_rows if r["means"]["D"] > r["means"][other]],
            "regressed_tasks": [
                r["task_id"] for r in paired_task_rows if r["means"]["D"] < r["means"][other]
            ],
        }
    primary = differences["D_vs_C"]
    valid_complete = complete and not any(score["execution_errors"] for score in scores.values())
    return {
        "complete": complete,
        "valid_complete": valid_complete,
        "cost_unit": cost_unit,
        "expected_cells": len(expected),
        "observed_cells": len(cells),
        "paired_tasks": len(paired),
        "distinct_tasks": len(expected_tasks),
        "repeats": repeats,
        "scores": scores,
        "contrasts": differences,
        "primary_contrast": "D_vs_C",
        "supports_positive_primary_effect": bool(
            valid_complete and primary["clusters"] >= 20 and primary["ci95"] and primary["ci95"][0] > 0
        ),
        "minimum_primary_clusters": 20,
        "interpretation": "positive claims require a complete grid without execution errors",
        "bootstrap": {
            "seed": seed,
            "samples": bootstrap_samples,
            "unit": "source_group; repeated solves averaged within task",
        },
    }
