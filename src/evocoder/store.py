from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from decimal import Decimal, ROUND_CEILING
from pathlib import Path

from evocoder.contracts import canonical, digest


def uid() -> str:
    return uuid.uuid4().hex


def dollars(value: int) -> float:
    return value / 1_000_000_000


def nanos(value) -> int:
    return int((Decimal(str(value)) * 1_000_000_000).to_integral_value(rounding=ROUND_CEILING))


class Store:
    """Append-only events/artifacts; activation and accounting use SQLite write transactions."""

    def __init__(self, root: Path, ledger: Store | None = None, *, unit: str | None = None):
        self.root = root.resolve()
        self.unit = unit or (ledger.unit if ledger else "usd")
        if self.unit not in {"usd", "credits"} or (ledger and ledger.unit != self.unit):
            raise ValueError("knowledge and accounting ledger units must match")
        if ledger and ledger.root == self.root:
            raise ValueError("knowledge state and external ledger must differ")
        if ledger and ledger.ledger:
            raise ValueError("external ledger must be a root ledger")
        self.ledger = ledger
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "evocoder.sqlite3"
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, task_id TEXT, split TEXT, task_hash TEXT,
                    skill_id TEXT, mode TEXT, purpose TEXT, state TEXT, created REAL,
                    result TEXT, config TEXT);
                CREATE TABLE IF NOT EXISTS events (
                    run_id TEXT, seq INTEGER, kind TEXT, payload TEXT,
                    PRIMARY KEY (run_id,seq));
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY, kind TEXT, body TEXT, created REAL);
                CREATE TABLE IF NOT EXISTS skills (
                    id TEXT PRIMARY KEY, parent TEXT, evidence_hash TEXT, status TEXT);
                CREATE TABLE IF NOT EXISTS evaluations (
                    id TEXT PRIMARY KEY, skill_id TEXT, baseline_id TEXT, report TEXT, passed INTEGER);
                CREATE TABLE IF NOT EXISTS active (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), skill_id TEXT);
                CREATE TABLE IF NOT EXISTS releases (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, before_id TEXT, after_id TEXT,
                    evaluation_id TEXT, reason TEXT, created REAL);
                CREATE TABLE IF NOT EXISTS budget (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), cap INTEGER,
                    spent INTEGER, reserved INTEGER, blocked INTEGER);
                CREATE TABLE IF NOT EXISTS calls (
                    id TEXT PRIMARY KEY, run_id TEXT, reserved INTEGER, actual INTEGER,
                    state TEXT, usage TEXT);
                CREATE TABLE IF NOT EXISTS budget_grants (
                    id TEXT PRIMARY KEY, before_cap INTEGER, after_cap INTEGER, reason TEXT, created REAL);
                CREATE TABLE IF NOT EXISTS budget_holds (
                    id TEXT PRIMARY KEY, amount INTEGER, reason TEXT, created REAL);
                CREATE TABLE IF NOT EXISTS test_exposures (
                    task_hash TEXT, task_id TEXT, experiment TEXT, created REAL,
                    PRIMARY KEY(task_hash, experiment));
                CREATE TABLE IF NOT EXISTS ledger_binding (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), root TEXT);
                CREATE TABLE IF NOT EXISTS accounting_unit (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), unit TEXT);
                CREATE TABLE IF NOT EXISTS library_active (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), library_id TEXT);
                CREATE TABLE IF NOT EXISTS library_evaluations (
                    id TEXT PRIMARY KEY, library_id TEXT, baseline_id TEXT, report TEXT, passed INTEGER);
                CREATE TABLE IF NOT EXISTS library_releases (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, before_id TEXT, after_id TEXT,
                    evaluation_id TEXT, reason TEXT, created REAL);
            """)
            db.execute("INSERT OR IGNORE INTO active VALUES(1,NULL)")
            db.execute("INSERT OR IGNORE INTO library_active VALUES(1,NULL)")
            currency = db.execute("SELECT unit FROM accounting_unit WHERE singleton=1").fetchone()
            if currency and currency[0] != self.unit:
                raise ValueError("refusing to reinterpret an existing ledger in another unit")
            if not currency:
                if self.unit != "usd" and db.execute("SELECT COUNT(*) FROM calls").fetchone()[0]:
                    raise ValueError("legacy USD ledger cannot become a credits ledger")
                db.execute("INSERT INTO accounting_unit VALUES(1,?)", (self.unit,))
            binding = db.execute("SELECT root FROM ledger_binding WHERE singleton=1").fetchone()
            if binding and (not ledger or binding[0] != str(ledger.root)):
                raise ValueError("state requires its original shared ledger; refusing a new budget")
            if ledger:
                db.execute("INSERT OR IGNORE INTO ledger_binding VALUES(1,?)", (str(ledger.root),))

    @contextmanager
    def connect(self, write=False):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def artifact(self, kind: str, body: dict) -> str:
        key = digest(body)
        with self.connect(write=True) as db:
            db.execute(
                "INSERT OR IGNORE INTO artifacts VALUES(?,?,?,?)", (key, kind, canonical(body), time.time())
            )
        return key

    def get_artifact(self, key: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT body FROM artifacts WHERE id=?", (key,)).fetchone()
        if not row:
            raise ValueError("unknown artifact")
        body = json.loads(row[0])
        if digest(body) != key:
            raise ValueError("artifact integrity mismatch")
        return body

    def event(self, run_id: str, kind: str, payload: dict):
        with self.connect(write=True) as db:
            seq = db.execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            db.execute("INSERT INTO events VALUES(?,?,?,?)", (run_id, seq, kind, canonical(payload)))

    def events(self, run_id: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT seq,kind,payload FROM events WHERE run_id=? ORDER BY seq", (run_id,))
            return [{"seq": r[0], "kind": r[1], "payload": json.loads(r[2])} for r in rows]

    def run(self, run_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise ValueError("unknown run")
        result = dict(row)
        result["result"] = json.loads(result["result"]) if result["result"] else None
        result["config"] = json.loads(result["config"])
        return result

    def finish(self, run_id: str, state: str, result: dict):
        with self.connect(write=True) as db:
            db.execute("UPDATE runs SET state=?,result=? WHERE id=?", (state, canonical(result), run_id))

    def active(self) -> str | None:
        with self.connect() as db:
            return db.execute("SELECT skill_id FROM active WHERE singleton=1").fetchone()[0]

    def activate(
        self,
        skill_id: str | None,
        evaluation_id: str | None,
        expected: str | None,
        reason="evaluation promotion",
    ):
        with self.connect(write=True) as db:
            current = db.execute("SELECT skill_id FROM active WHERE singleton=1").fetchone()[0]
            if current != expected:
                raise ValueError("active version changed; compare-and-swap rejected")
            if skill_id:
                row = db.execute(
                    "SELECT passed,report FROM evaluations WHERE id=? AND skill_id=?",
                    (evaluation_id, skill_id),
                ).fetchone()
                if not row or not row[0]:
                    raise ValueError("activation requires an evaluator-issued passing receipt")
                report = json.loads(row[1])
                if reason != "rollback" and report["active_at_evaluation"] != current:
                    raise ValueError("evaluation is stale for the current active version")
            db.execute("UPDATE active SET skill_id=? WHERE singleton=1", (skill_id,))
            db.execute(
                "INSERT INTO releases(before_id,after_id,evaluation_id,reason,created) VALUES(?,?,?,?,?)",
                (current, skill_id, evaluation_id, reason, time.time()),
            )

    def active_library(self) -> str | None:
        with self.connect() as db:
            return db.execute("SELECT library_id FROM library_active WHERE singleton=1").fetchone()[0]

    def library_members(self, library_id: str, *, mode: str, require_published=False) -> list[str]:
        body = self.get_artifact(library_id)
        if (
            set(body) != {"schema", "kind", "mode", "members"}
            or body.get("schema") != 1
            or body.get("kind") != "skill_library"
        ):
            raise ValueError("invalid library manifest")
        members = body["members"]
        if body["mode"] != mode:
            raise ValueError("library mode differs from runner mode")
        if (
            not isinstance(members, list)
            or not 1 <= len(members) <= 8
            or any(not isinstance(m, str) for m in members)
            or members != sorted(set(members))
        ):
            raise ValueError("library requires 1..8 sorted unique skill IDs")
        with self.connect() as db:
            if any(not db.execute("SELECT 1 FROM skills WHERE id=?", (m,)).fetchone() for m in members):
                raise ValueError("library member has no proposal provenance")
            if (
                require_published
                and not db.execute(
                    "SELECT 1 FROM library_releases WHERE after_id=?", (library_id,)
                ).fetchone()
            ):
                raise ValueError("library has not been published")
        for member in members:
            self.get_artifact(member)
        return members

    def activate_library(
        self, library_id: str | None, receipt: str | None, expected: str | None, *, mode: str, rollback=False
    ):
        if library_id:
            self.library_members(library_id, mode=mode)
        with self.connect(write=True) as db:
            current = db.execute("SELECT library_id FROM library_active WHERE singleton=1").fetchone()[0]
            if current != expected:
                raise ValueError("library changed; compare-and-swap rejected")
            if current and self.get_artifact(current)["mode"] != mode:
                raise ValueError("current library mode differs from publication mode")
            if rollback:
                if (
                    library_id
                    and not db.execute(
                        "SELECT 1 FROM library_releases WHERE after_id=?", (library_id,)
                    ).fetchone()
                ):
                    raise ValueError("rollback target was never published")
            else:
                row = db.execute(
                    "SELECT * FROM library_evaluations WHERE id=? AND library_id=? AND passed=1",
                    (receipt, library_id),
                ).fetchone()
                if not row:
                    raise ValueError("library publication requires an evaluator-issued passing receipt")
                report = json.loads(row["report"])
                if (
                    digest(report) != receipt
                    or report["mode"] != mode
                    or report.get("kind") != "library_evaluation"
                    or report.get("library_id") != library_id
                    or report.get("gate", {}).get("passed") is not True
                ):
                    raise ValueError("library receipt integrity or mode mismatch")
                if report["baseline_library"] != current:
                    raise ValueError("library evaluation is stale")
            db.execute("UPDATE library_active SET library_id=? WHERE singleton=1", (library_id,))
            db.execute(
                "INSERT INTO library_releases(before_id,after_id,evaluation_id,reason,created) VALUES(?,?,?,?,?)",
                (
                    current,
                    library_id,
                    receipt,
                    "rollback" if rollback else "evaluation promotion",
                    time.time(),
                ),
            )

    def configure_budget(self, cap: int):
        if self.ledger:
            return self.ledger.configure_budget(cap)
        if type(cap) is not int or cap <= 0:
            raise ValueError("positive accounting cap required")
        with self.connect(write=True) as db:
            db.execute("INSERT OR IGNORE INTO budget VALUES(1,?,0,0,0)", (cap,))
            if db.execute("SELECT cap FROM budget WHERE singleton=1").fetchone()[0] != cap:
                raise ValueError("budget cap is immutable for this state directory")

    def reserve(self, run_id: str, cap: int, maximum: int) -> str:
        if self.ledger:
            return self.ledger.reserve(run_id, cap, maximum)
        if type(cap) is not int or type(maximum) is not int or cap <= 0 or maximum <= 0:
            raise ValueError("cap and reservation must be positive integer nanodollars")
        call_id = uid()
        with self.connect(write=True) as db:
            db.execute("INSERT OR IGNORE INTO budget VALUES(1,?,0,0,0)", (cap,))
            row = db.execute("SELECT * FROM budget WHERE singleton=1").fetchone()
            if row["cap"] != cap:
                raise ValueError("budget cap is immutable for this state directory")
            held = db.execute("SELECT COALESCE(SUM(amount),0) FROM budget_holds").fetchone()[0]
            if row["blocked"] or row["spent"] + row["reserved"] + held + maximum > cap:
                raise ValueError("cumulative live budget exhausted")
            db.execute("UPDATE budget SET reserved=reserved+? WHERE singleton=1", (maximum,))
            db.execute("INSERT INTO calls VALUES(?,?,?,NULL,'reserved',NULL)", (call_id, run_id, maximum))
        return call_id

    def settle(self, call_id: str, actual: int, usage: dict):
        if self.ledger:
            return self.ledger.settle(call_id, actual, usage)
        if type(actual) is not int or actual < 0:
            raise ValueError("settlement must be nonnegative integer nanodollars")
        with self.connect(write=True) as db:
            row = db.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()
            if not row or row["state"] != "reserved":
                raise ValueError("call is not an open reservation")
            db.execute(
                "UPDATE calls SET actual=?,state='settled',usage=? WHERE id=?",
                (actual, canonical(usage), call_id),
            )
            db.execute(
                "UPDATE budget SET reserved=reserved-?,spent=spent+?,"
                "blocked=MAX(blocked,?) WHERE singleton=1",
                (row["reserved"], actual, int(actual > row["reserved"])),
            )

    def costs(self, run_ids: list[str]) -> dict:
        if self.ledger:
            return self.ledger.costs(run_ids)
        if not run_ids:
            return {"cost_" + self.unit: 0, "reserved_" + self.unit: 0, "calls": 0}
        with self.connect() as db:
            rows = db.execute(
                f"SELECT * FROM calls WHERE run_id IN ({','.join('?' for _ in run_ids)})", run_ids
            ).fetchall()
        return {
            "cost_" + self.unit: dollars(sum(r["actual"] or 0 for r in rows)),
            "reserved_" + self.unit: dollars(sum(r["reserved"] for r in rows if r["state"] == "reserved")),
            "calls": len(rows),
        }

    def budget(self) -> dict:
        if self.ledger:
            return self.ledger.budget()
        with self.connect() as db:
            row = db.execute("SELECT * FROM budget WHERE singleton=1").fetchone()
            held = db.execute("SELECT COALESCE(SUM(amount),0) FROM budget_holds").fetchone()[0]
        if not row:
            return {"configured": False, "spent_" + self.unit: 0, "reserved_" + self.unit: 0}
        return {
            "configured": True,
            "cap_" + self.unit: None
            if self.unit == "credits" and row["cap"] == (1 << 63) - 1
            else dollars(row["cap"]),
            "spent_" + self.unit: dollars(row["spent"]),
            "reserved_" + self.unit: dollars(row["reserved"]),
            "held_" + self.unit: dollars(held),
            "blocked": bool(row["blocked"]),
        }

    def hold_budget(self, hold_id: str, amount: int, reason: str):
        """Conservative non-call allowance; never fabricate a provider request or rewrite usage."""
        if self.ledger:
            return self.ledger.hold_budget(hold_id, amount, reason)
        if type(amount) is not int or amount <= 0 or not hold_id or not reason.strip():
            raise ValueError("positive allowance and reason required")
        with self.connect(write=True) as db:
            old = db.execute("SELECT amount,reason FROM budget_holds WHERE id=?", (hold_id,)).fetchone()
            if old and tuple(old) != (amount, reason):
                raise ValueError("allowance ID reused with different details")
            db.execute(
                "INSERT OR IGNORE INTO budget_holds VALUES(?,?,?,?)", (hold_id, amount, reason, time.time())
            )

    def grant_budget(self, grant_id: str, additional: int, expected_cap: int, reason: str):
        """Explicit authorization only; preserve spend and the complete call history."""
        if self.ledger:
            return self.ledger.grant_budget(grant_id, additional, expected_cap, reason)
        if type(additional) is not int or additional <= 0 or not grant_id or not reason.strip():
            raise ValueError("positive grant and authorization reason required")
        with self.connect(write=True) as db:
            previous = db.execute("SELECT * FROM budget_grants WHERE id=?", (grant_id,)).fetchone()
            if previous:
                if (previous["before_cap"], previous["after_cap"], previous["reason"]) != (
                    expected_cap,
                    expected_cap + additional,
                    reason,
                ):
                    raise ValueError("grant ID reused with different authorization")
                return
            row = db.execute("SELECT * FROM budget WHERE singleton=1").fetchone()
            if not row or row["cap"] != expected_cap or row["reserved"] or row["blocked"]:
                raise ValueError("budget changed, unsettled or blocked; cannot grant")
            db.execute("UPDATE budget SET cap=cap+? WHERE singleton=1", (additional,))
            db.execute(
                "INSERT INTO budget_grants VALUES(?,?,?,?,?)",
                (grant_id, expected_cap, expected_cap + additional, reason, time.time()),
            )

    def claim_test(self, task_id: str, task_hash: str, experiment: str):
        if self.ledger:
            return self.ledger.claim_test(task_id, task_hash, experiment)
        with self.connect(write=True) as db:
            old = db.execute(
                "SELECT experiment FROM test_exposures WHERE (task_hash=? OR task_id=?) AND experiment<>?",
                (task_hash, task_id, experiment),
            ).fetchone()
            if old:
                raise ValueError("test task was exposed by a different experiment; cannot call it unseen")
            db.execute(
                "INSERT OR IGNORE INTO test_exposures VALUES(?,?,?,?)",
                (task_hash, task_id, experiment, time.time()),
            )
