"""SQLite log of every screened call: what the caller said, the verdict, the outcome."""

import sqlite3
from pathlib import Path


class CallLog:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS calls (
                sid TEXT PRIMARY KEY, ts REAL, from_number TEXT, transcript TEXT,
                confidence REAL, decision TEXT, category TEXT, caller TEXT, summary TEXT,
                mode TEXT, error TEXT, outcome TEXT, recording_url TEXT)"""
        )
        # Added after the first deploys, so patch older databases in place.
        if "wrong" not in {r[1] for r in self.db.execute("PRAGMA table_info(calls)")}:
            self.db.execute("ALTER TABLE calls ADD COLUMN wrong INTEGER DEFAULT 0")
        self.db.commit()

    def upsert(self, sid: str, **fields):
        cols = ["sid", *fields]
        self.db.execute(
            f"INSERT INTO calls ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
            f"ON CONFLICT(sid) DO UPDATE SET {', '.join(f'{c}=excluded.{c}' for c in fields)}",
            [sid, *fields.values()],
        )
        self.db.commit()

    def get(self, sid: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM calls WHERE sid = ?", [sid]).fetchone()

    @staticmethod
    def _source_filter(source: str, test_numbers: tuple[str, ...]) -> tuple[str, list]:
        """SQL condition for source "test" (from a test number), "real" (not), or "" (all)."""
        if source not in ("test", "real"):
            return "1", []
        if not test_numbers:  # "x NOT IN (NULL)" would match nothing
            return ("0" if source == "test" else "1"), []
        marks = ",".join("?" * len(test_numbers)) or "NULL"
        op = "IN" if source == "test" else "NOT IN"
        return f"COALESCE(from_number, '') {op} ({marks})", list(test_numbers)

    def recent(self, limit: int = 200, decision: str = "", source: str = "",
               test_numbers: tuple[str, ...] = ()) -> list[sqlite3.Row]:
        where, args = self._source_filter(source, test_numbers)
        if decision:
            where, args = f"{where} AND decision = ?", args + [decision]
        return self.db.execute(f"SELECT * FROM calls WHERE {where} ORDER BY ts DESC LIMIT ?",
                               args + [limit]).fetchall()

    def counts(self, source: str = "", test_numbers: tuple[str, ...] = ()) -> dict[str, int]:
        where, args = self._source_filter(source, test_numbers)
        rows = self.db.execute(
            "SELECT decision, COUNT(*) n, SUM(COALESCE(wrong, 0)) w FROM calls "
            f"WHERE {where} GROUP BY decision", args)
        out = {}
        for r in rows:
            out[r["decision"] or "pending"] = r["n"]
            out[f"{r['decision'] or 'pending'}_wrong"] = r["w"] or 0
        return out
