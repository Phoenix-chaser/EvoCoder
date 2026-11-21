"""Audit outstanding charges from terminal runs on the same ledger."""

import json
import sqlite3
from collections import defaultdict
from contextlib import closing
from pathlib import Path

from evocoder.contracts import digest
from evocoder.store import Store, nanos


def open_calls(ledger: Store) -> list[dict]:
    with ledger.connect() as db:
        return [
            dict(row)
            for row in db.execute("SELECT id,run_id,reserved FROM calls WHERE state='reserved' ORDER BY id")
        ]


def audit_historical_reservations(ledger: Store, origins: list[Path]) -> dict:
    """Identify old unknown charges without modifying their call or budget rows."""
    pending = defaultdict(list)
    for call in open_calls(ledger):
        pending[call["run_id"]].append(call)
    proved = {}
    paths = sorted({path.resolve() for path in origins})
    for root in paths:
        protocol = json.loads((root / "protocol.json").read_text())
        result = json.loads((root / "result.json").read_text())
        if not (root / "started.json").is_file() or result["completed"] or result["protocol"] != protocol:
            raise ValueError("reservation origin must be a stopped comparison with its original protocol")
        for arm in ("common", "B", "seed", "C", "D"):
            path = root / arm / "evocoder.sqlite3"
            if not path.is_file():
                continue
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
                binding = db.execute("SELECT root FROM ledger_binding WHERE singleton=1").fetchone()
                if binding != (str(ledger.root),):
                    raise ValueError("reservation origin binds to a different ledger")
                if db.execute("SELECT 1 FROM runs WHERE state='RUNNING' LIMIT 1").fetchone():
                    raise ValueError("reservation origin still has running work")
                for run_id, calls in pending.items():
                    row = db.execute("SELECT state,result FROM runs WHERE id=?", (run_id,)).fetchone()
                    if row is None or row[0] not in {"ERROR", "INTERRUPTED"} or not row[1]:
                        continue
                    reported = json.loads(row[1]).get("cost", {}).get("reserved_" + ledger.unit, 0)
                    if sum(call["reserved"] for call in calls) <= nanos(reported):
                        proved.setdefault(run_id, {
                            "arm": arm,
                            "origin": str(root),
                            "result_hash": digest(result),
                        })
    if pending.keys() - proved.keys():
        raise ValueError("ledger has unsettled calls without evidence from stopped comparisons")
    return {
        "origins": [str(path) for path in paths],
        "calls": [{**call, **proved[run_id]} for run_id, calls in pending.items() for call in calls],
    }
