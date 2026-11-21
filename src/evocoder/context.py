"""Deterministic public-history projection. Full executed actions remain in the event log."""

from __future__ import annotations

import copy

from evocoder.contracts import digest


def clip(text, limit):
    if len(text.encode()) <= limit:
        return text
    marker = "\n[truncated public history; sha256=" + digest(text) + "]\n"
    half = max(0, (limit - len(marker.encode())) // 2)
    return (
        text.encode()[:half].decode("utf-8", "ignore")
        + marker
        + text.encode()[-half:].decode("utf-8", "ignore")
    )


def compact(data: dict, level: int) -> dict:
    result = copy.deepcopy(data)
    history = result.get("history", [])
    original = data.get("history", [])
    if level == 3 and len(history) > 4:
        history = history[-4:]
    for index, item in enumerate(history):
        recent = index >= len(history) - 2
        if level == 1 and recent:
            continue
        action = item.get("action", {})
        for key in ("content", "old_text", "new_text"):
            if isinstance(action.get(key), str):
                action[key] = clip(action[key], 4000 if recent else 800)
        observation = item.get("observation", {})
        if isinstance(observation.get("output"), str):
            observation["output"] = clip(observation["output"], 3000 if recent else 1200)
    result["history"] = history
    result["history_compaction"] = {
        "policy": "bounded_public_history_v1",
        "level": level,
        "original_entries": len(original),
        "visible_entries": len(history),
        "original_hash": digest(original),
        "instruction": "Some old action text/observations were truncated. Do not execute displayed historical snippets; reread source when needed.",
    }
    return result
