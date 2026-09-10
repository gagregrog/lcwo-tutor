#!/usr/bin/env python3
"""
lcwo.py - grade LCWO (Learn CW Online) practice assignments.

Hierarchy:  group  >  session  >  run

  group    a batch of 2-3 sessions sharing a mode / assignment / speed
  session  one LCWO lesson = one audio clip
  run      one attempt at that clip. Runs 1..N-1 are your raw answers with no
           answer key. The final run arrives as LCWO's results table, whose
           "Sent Group" column is the key for the whole session and whose
           "Received Group" column is that final run's answers. It grades every
           earlier run retroactively and closes the session.

Usage:
    python3 lcwo.py                 record (interactive)
    python3 lcwo.py report          build the HTML report
    python3 lcwo.py groups          list groups
    python3 lcwo.py trouble         trouble letters, all time
    python3 lcwo.py selftest        run the built-in checks
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import webbrowser
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import escape
from pathlib import Path

APP_DIR = Path(os.environ.get("LCWO_HOME", Path(__file__).resolve().parent))
DB_PATH = Path(os.environ.get("LCWO_DB", APP_DIR / "lcwo.db"))
REPORT_DIR = Path(os.environ.get("LCWO_REPORTS", APP_DIR / "reports"))

# Extend this list as new LCWO drills get added.
MODES = [
    ("letters", "Letters"),
    ("code_group", "Code group"),
    ("custom", "Error practice (custom characters)"),
]

MISS_KINDS = ("missed", "wrong", "transposed")
SOFT_RUN_LIMIT = 3  # you said 1-3; past this we just nudge
SOFT_SESSION_LIMIT = 3


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def fmt_ts(iso: str | None) -> str:
    if not iso:
        return "-"
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso


# --------------------------------------------------------------------------
# grading engine (pure functions, no I/O)
# --------------------------------------------------------------------------


@dataclass
class CharCell:
    """One character position within one group."""

    pos: int
    sent: str
    recv: str
    kind: str  # correct | missed | wrong | transposed | extra

    @property
    def is_miss(self) -> bool:
        return self.kind in MISS_KINDS


@dataclass
class GroupCell:
    idx: int
    sent: str
    recv: str
    cells: list[CharCell]
    transposed: bool
    reported_errors: int | None = None

    @property
    def wrong(self) -> int:
        return sum(1 for c in self.cells if c.is_miss)

    @property
    def clean(self) -> bool:
        return self.wrong == 0 and not any(c.kind == "extra" for c in self.cells)

    @property
    def disagrees_with_lcwo(self) -> bool:
        return self.reported_errors is not None and self.reported_errors != self.wrong


@dataclass
class RunGrade:
    groups: list[GroupCell] = field(default_factory=list)

    @property
    def total_chars(self) -> int:
        return sum(len(g.sent) for g in self.groups)

    @property
    def wrong_chars(self) -> int:
        return sum(g.wrong for g in self.groups)

    @property
    def right_chars(self) -> int:
        return self.total_chars - self.wrong_chars

    @property
    def total_groups(self) -> int:
        return sum(1 for g in self.groups if g.sent)

    @property
    def clean_groups(self) -> int:
        return sum(1 for g in self.groups if g.sent and g.clean)

    @property
    def wrong_groups(self) -> int:
        return self.total_groups - self.clean_groups

    @property
    def pct_wrong(self) -> float:
        return 100.0 * self.wrong_chars / self.total_chars if self.total_chars else 0.0

    @property
    def pct_right(self) -> float:
        return 100.0 - self.pct_wrong if self.total_chars else 0.0

    @property
    def transposed_groups(self) -> list[GroupCell]:
        return [g for g in self.groups if g.transposed]

    def miss_counts(self) -> Counter:
        """char -> number of times it was sent but not copied correctly."""
        c: Counter = Counter()
        for g in self.groups:
            for cell in g.cells:
                if cell.is_miss:
                    c[cell.sent] += 1
        return c

    def miss_breakdown(self) -> dict[str, Counter]:
        """char -> Counter of miss kinds."""
        out: dict[str, Counter] = defaultdict(Counter)
        for g in self.groups:
            for cell in g.cells:
                if cell.is_miss:
                    out[cell.sent][cell.kind] += 1
        return out

    def sent_counts(self) -> Counter:
        c: Counter = Counter()
        for g in self.groups:
            for ch in g.sent:
                c[ch] += 1
        return c

    def confusions(self) -> Counter:
        """(sent, heard) -> count, for genuine substitutions only."""
        c: Counter = Counter()
        for g in self.groups:
            for cell in g.cells:
                if cell.kind == "wrong":
                    c[(cell.sent, cell.recv)] += 1
        return c


def grade_group(idx: int, sent: str, recv: str, reported: int | None = None) -> GroupCell:
    sent = (sent or "").upper()
    recv = (recv or "").upper()

    # A transposition is the same characters in the wrong order - you knew them
    # all, you just wrote them down swapped. Worth separating from a real miss.
    transposed = (
        len(sent) >= 2
        and len(recv) == len(sent)
        and sent != recv
        and "." not in recv
        and sorted(sent) == sorted(recv)
    )

    cells: list[CharCell] = []
    for pos in range(max(len(sent), len(recv))):
        s = sent[pos] if pos < len(sent) else ""
        r = recv[pos] if pos < len(recv) else ""
        if not s:
            kind = "extra"  # copied a character that was never sent
        elif r == s:
            kind = "correct"
        elif r in ("", "."):
            kind = "missed"  # you flagged it as "didn't know"
        elif transposed:
            kind = "transposed"
        else:
            kind = "wrong"  # heard it as something else
        cells.append(CharCell(pos, s, r, kind))

    return GroupCell(idx, sent, recv, cells, transposed, reported)


def grade_run(key: list[str], attempt: list[str], reported: list[int] | None = None) -> RunGrade:
    """Grade one run's `attempt` against the session's `key`, aligned by index."""
    n = max(len(key), len(attempt))
    groups = []
    for i in range(n):
        s = key[i] if i < len(key) else ""
        r = attempt[i] if i < len(attempt) else ""
        rep = reported[i] if reported and i < len(reported) else None
        groups.append(grade_group(i, s, r, rep))
    return RunGrade(groups)


# --------------------------------------------------------------------------
# paste parsing
# --------------------------------------------------------------------------

_HEADER_RE = re.compile(r"sent\s*group|received\s*group", re.I)
_INT_RE = re.compile(r"^\d{1,3}$")


@dataclass
class Paste:
    kind: str  # "attempt" | "results"
    groups: list[str]  # your answers (received side)
    key: list[str] | None = None  # correct answers, results paste only
    reported: list[int] | None = None  # LCWO's own per-group error counts
    warnings: list[str] = field(default_factory=list)


def _looks_like_results_row(toks: list[str]) -> bool:
    return len(toks) >= 3 and bool(_INT_RE.match(toks[-1]))


def parse_paste(text: str) -> Paste:
    """Auto-detect an answers-only paste vs. an LCWO results table."""
    rows: list[list[str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or _HEADER_RE.search(line):
            continue
        rows.append(line.split())

    if not rows:
        return Paste("attempt", [], warnings=["nothing to parse"])

    hits = sum(1 for r in rows if _looks_like_results_row(r))
    if hits and hits >= 0.6 * len(rows):
        return _parse_results(rows)
    return _parse_attempt(rows)


def _parse_attempt(rows: list[list[str]]) -> Paste:
    # Line-separated is what you paste, but tolerate space-separated too.
    groups = [t.upper() for row in rows for t in row]
    return Paste("attempt", groups, warnings=_width_warnings(groups))


def _parse_results(rows: list[list[str]]) -> Paste:
    key: list[str] = []
    recv: list[str] = []
    reported: list[int] = []
    warns: list[str] = []

    for i, toks in enumerate(rows, 1):
        if not _looks_like_results_row(toks):
            if len(toks) == 2 and _INT_RE.match(toks[-1]):
                # sent group, nothing copied, error count
                key.append(toks[0].upper())
                recv.append("")
                reported.append(int(toks[-1]))
            else:
                warns.append(f"row {i}: skipped unparseable line {' '.join(toks)!r}")
            continue
        # LCWO repeats the sent group in a diff-highlight column; ignore the middle.
        key.append(toks[0].upper())
        recv.append(toks[1].upper())
        reported.append(int(toks[-1]))

    warns += _width_warnings(key, label="sent")
    return Paste("results", recv, key=key, reported=reported, warnings=warns)


def _width_warnings(groups: list[str], label: str = "") -> list[str]:
    real = [g for g in groups if g]
    if not real:
        return []
    widths = Counter(len(g) for g in real)
    modal, _ = widths.most_common(1)[0]
    odd = [g for g in real if len(g) != modal]
    if not odd:
        return []
    what = f"{label} " if label else ""
    return [
        f"{len(odd)} {what}group(s) are not {modal} characters wide: "
        + ", ".join(repr(g) for g in odd[:8])
        + ("..." if len(odd) > 8 else "")
    ]


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS groups (
    id          INTEGER PRIMARY KEY,
    label       TEXT,
    mode        TEXT NOT NULL,
    assignment  TEXT NOT NULL,
    char_wpm    REAL,
    eff_wpm     REAL,
    created_at  TEXT NOT NULL,
    closed_at   TEXT,
    notes       TEXT,
    source      TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY,
    group_id    INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    mode        TEXT NOT NULL,
    assignment  TEXT NOT NULL,
    char_wpm    REAL,
    eff_wpm     REAL,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    key_json    TEXT,
    notes       TEXT,
    UNIQUE (group_id, seq)
);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY,
    session_id   INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq          INTEGER NOT NULL,
    is_final     INTEGER NOT NULL DEFAULT 0,
    recorded_at  TEXT NOT NULL,
    attempt_json TEXT NOT NULL,
    reported_json TEXT,
    raw_paste    TEXT NOT NULL,
    UNIQUE (session_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_sessions_group ON sessions(group_id);
CREATE INDEX IF NOT EXISTS idx_runs_session ON runs(session_id);
"""


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    migrate(con)
    return con


def table_ddl(table: str) -> str:
    """Pull one CREATE TABLE statement out of SCHEMA (single source of truth)."""
    for stmt in SCHEMA.split(";"):
        if f"CREATE TABLE IF NOT EXISTS {table}" in stmt:
            return stmt.strip()
    raise KeyError(table)


def migrate(con) -> None:
    """Bring a database created by an older version up to the current schema."""

    def cols(table):
        return {r["name"]: r for r in con.execute(f"PRAGMA table_info({table})")}

    for table, col, decl in (
        ("groups", "notes", "TEXT"),
        ("groups", "source", "TEXT"),
        ("sessions", "notes", "TEXT"),
    ):
        if col not in cols(table):
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    con.commit()

    stale = [t for t in ("groups", "sessions")
             if any(c["notnull"] and c["name"] in ("char_wpm", "eff_wpm")
                    for c in cols(t).values())]
    if not stale:
        return

    # Speeds became nullable, which SQLite can only do by rebuilding the table.
    # Foreign keys must be off (ON DELETE CASCADE would wipe the children when
    # the old table is dropped) and legacy_alter_table on (otherwise RENAME
    # rewrites the FK clauses of *other* tables to point at the _old name).
    prev = con.isolation_level
    con.isolation_level = None
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute("PRAGMA legacy_alter_table=ON")
    try:
        con.execute("BEGIN")
        for table in stale:
            keep = ",".join(cols(table))
            con.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
            con.execute(table_ddl(table))
            con.execute(f"INSERT INTO {table} ({keep}) SELECT {keep} FROM {table}_old")
            con.execute(f"DROP TABLE {table}_old")
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.execute("PRAGMA legacy_alter_table=OFF")
        con.execute("PRAGMA foreign_keys=ON")
        con.isolation_level = prev
    con.executescript(SCHEMA)  # recreate indexes dropped with the old tables
    bad = con.execute("PRAGMA foreign_key_check").fetchall()
    if bad:
        raise SystemExit(f"migration left dangling references: {bad[:3]}")


def create_group(con, mode, assignment, char_wpm, eff_wpm, label=None,
                 created_at=None, notes=None, source=None) -> int:
    label = label or (f"{dict(MODES).get(mode, mode)} #{assignment} "
                      f"@ {fmt_wpm(char_wpm)}/{fmt_wpm(eff_wpm)}")
    cur = con.execute(
        "INSERT INTO groups (label, mode, assignment, char_wpm, eff_wpm, created_at,"
        " notes, source) VALUES (?,?,?,?,?,?,?,?)",
        (label, mode, assignment, char_wpm, eff_wpm, created_at or now_iso(), notes, source),
    )
    con.commit()
    return cur.lastrowid


def fmt_wpm(v) -> str:
    return "?" if v is None else f"{v:g}"


def open_groups(con) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM groups WHERE closed_at IS NULL ORDER BY created_at DESC"
    ).fetchall()


def last_group(con) -> sqlite3.Row | None:
    """The most recently worked group, open or closed."""
    return con.execute(
        "SELECT g.*, COALESCE(MAX(s.started_at), g.created_at) AS activity"
        " FROM groups g LEFT JOIN sessions s ON s.group_id = g.id"
        " GROUP BY g.id ORDER BY activity DESC, g.id DESC LIMIT 1"
    ).fetchone()


def reopen_group(con, gid) -> None:
    con.execute("UPDATE groups SET closed_at=NULL WHERE id=?", (gid,))
    con.commit()


def prune_empty(con) -> tuple[int, int]:
    """Drop sessions with no runs and groups with no sessions.

    An interrupted recording leaves a session (and possibly a group) holding
    nothing at all. They carry no data, so clearing them on the way in and out
    keeps an abandoned start from turning into a permanent empty group.
    """
    ns = con.execute(
        "DELETE FROM sessions WHERE id NOT IN (SELECT session_id FROM runs)").rowcount
    ng = con.execute(
        "DELETE FROM groups WHERE id NOT IN (SELECT group_id FROM sessions)").rowcount
    con.commit()
    return ns, ng


def all_groups(con) -> list[sqlite3.Row]:
    return con.execute("SELECT * FROM groups ORDER BY created_at").fetchall()


def get_group(con, gid) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM groups WHERE id=?", (gid,)).fetchone()


def close_group(con, gid) -> None:
    con.execute("UPDATE groups SET closed_at=? WHERE id=?", (now_iso(), gid))
    con.commit()


def group_sessions(con, gid) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM sessions WHERE group_id=? ORDER BY seq", (gid,)
    ).fetchall()


def session_runs(con, sid) -> list[sqlite3.Row]:
    return con.execute("SELECT * FROM runs WHERE session_id=? ORDER BY seq", (sid,)).fetchall()


def start_session(con, grp, started_at=None, notes=None, mode=None) -> sqlite3.Row:
    seq = (con.execute(
        "SELECT COALESCE(MAX(seq),0) FROM sessions WHERE group_id=?", (grp["id"],)
    ).fetchone()[0]) + 1
    cur = con.execute(
        "INSERT INTO sessions (group_id, seq, mode, assignment, char_wpm, eff_wpm,"
        " started_at, notes) VALUES (?,?,?,?,?,?,?,?)",
        (grp["id"], seq, mode or grp["mode"], grp["assignment"], grp["char_wpm"],
         grp["eff_wpm"], started_at or now_iso(), notes),
    )
    con.commit()
    return con.execute("SELECT * FROM sessions WHERE id=?", (cur.lastrowid,)).fetchone()


def add_run(con, sid, attempt, raw, is_final=False, reported=None,
            recorded_at=None) -> int:
    seq = (con.execute(
        "SELECT COALESCE(MAX(seq),0) FROM runs WHERE session_id=?", (sid,)
    ).fetchone()[0]) + 1
    cur = con.execute(
        "INSERT INTO runs (session_id, seq, is_final, recorded_at, attempt_json,"
        " reported_json, raw_paste) VALUES (?,?,?,?,?,?,?)",
        (sid, seq, int(is_final), recorded_at or now_iso(), json.dumps(attempt),
         json.dumps(reported) if reported is not None else None, raw),
    )
    con.commit()
    return cur.lastrowid


def finish_session(con, sid, key, ended_at=None) -> None:
    con.execute(
        "UPDATE sessions SET key_json=?, ended_at=? WHERE id=?",
        (json.dumps(key), ended_at or now_iso(), sid),
    )
    con.commit()


def delete_session(con, sid) -> None:
    con.execute("DELETE FROM sessions WHERE id=?", (sid,))
    con.commit()


# --------------------------------------------------------------------------
# rollups: run -> session -> group -> all time
# --------------------------------------------------------------------------

TROUBLE_THRESHOLD = 2  # missed this many times or more within the scope


@dataclass
class RunView:
    seq: int
    is_final: bool
    recorded_at: str
    grade: RunGrade | None  # None while the session has no key yet


@dataclass
class SessionView:
    row: sqlite3.Row
    runs: list[RunView]

    @property
    def graded(self) -> bool:
        return any(r.grade for r in self.runs)

    @property
    def best(self) -> RunView | None:
        g = [r for r in self.runs if r.grade]
        return min(g, key=lambda r: r.grade.pct_wrong) if g else None

    @property
    def final(self) -> RunView | None:
        for r in self.runs:
            if r.is_final and r.grade:
                return r
        return self.runs[-1] if self.runs and self.runs[-1].grade else None

    def miss_counts(self) -> Counter:
        c: Counter = Counter()
        for r in self.runs:
            if r.grade:
                c.update(r.grade.miss_counts())
        return c

    def sent_counts(self) -> Counter:
        c: Counter = Counter()
        for r in self.runs:
            if r.grade:
                c.update(r.grade.sent_counts())
        return c

    def confusions(self) -> Counter:
        c: Counter = Counter()
        for r in self.runs:
            if r.grade:
                c.update(r.grade.confusions())
        return c

    def miss_breakdown(self) -> dict[str, Counter]:
        out: dict[str, Counter] = defaultdict(Counter)
        for r in self.runs:
            if r.grade:
                for ch, kinds in r.grade.miss_breakdown().items():
                    out[ch].update(kinds)
        return out


@dataclass
class GroupView:
    row: sqlite3.Row
    sessions: list[SessionView]

    def miss_counts(self) -> Counter:
        c: Counter = Counter()
        for s in self.sessions:
            c.update(s.miss_counts())
        return c

    def sent_counts(self) -> Counter:
        c: Counter = Counter()
        for s in self.sessions:
            c.update(s.sent_counts())
        return c

    def confusions(self) -> Counter:
        c: Counter = Counter()
        for s in self.sessions:
            c.update(s.confusions())
        return c

    def miss_breakdown(self) -> dict[str, Counter]:
        out: dict[str, Counter] = defaultdict(Counter)
        for s in self.sessions:
            for ch, kinds in s.miss_breakdown().items():
                out[ch].update(kinds)
        return out

    def trouble(self, threshold: int = TROUBLE_THRESHOLD) -> list[tuple[str, int]]:
        return trouble_from(self.miss_counts(), threshold)

    @property
    def graded_runs(self) -> list[RunView]:
        return [r for s in self.sessions for r in s.runs if r.grade]


def trouble_from(counts: Counter, threshold: int = TROUBLE_THRESHOLD) -> list[tuple[str, int]]:
    """Chars missed >= threshold times in scope, worst first then alphabetical."""
    return sorted(
        ((ch, n) for ch, n in counts.items() if n >= threshold),
        key=lambda kv: (-kv[1], kv[0]),
    )


def load_group(con, gid) -> GroupView:
    grow = get_group(con, gid)
    if grow is None:
        raise SystemExit(f"no such group: {gid}")
    sviews = []
    for srow in group_sessions(con, gid):
        key = json.loads(srow["key_json"]) if srow["key_json"] else None
        rviews = []
        for rrow in session_runs(con, srow["id"]):
            attempt = json.loads(rrow["attempt_json"])
            reported = json.loads(rrow["reported_json"]) if rrow["reported_json"] else None
            grade = grade_run(key, attempt, reported) if key else None
            rviews.append(RunView(rrow["seq"], bool(rrow["is_final"]), rrow["recorded_at"], grade))
        sviews.append(SessionView(srow, rviews))
    return GroupView(grow, sviews)


def load_all(con) -> list[GroupView]:
    return [load_group(con, g["id"]) for g in all_groups(con)]


def global_miss_counts(views: list[GroupView]) -> Counter:
    c: Counter = Counter()
    for v in views:
        c.update(v.miss_counts())
    return c


# --------------------------------------------------------------------------
# HTML report
#
# The page carries the graded data as JSON and aggregates it in the browser,
# so one set of panels serves every scope (all time / a day / a group / a
# session). Pre-rendering each scope server-side would mean ~40 copies of the
# same tables; grading still happens here in Python, and the page only ever
# sums the per-character verdicts it is handed.
# --------------------------------------------------------------------------

KIND_CODE = {"correct": "c", "missed": "m", "wrong": "w", "transposed": "t", "extra": "x"}


def report_payload(views: list[GroupView]) -> dict:
    groups, sessions, runs = [], [], []
    for v in views:
        g = v.row
        groups.append({
            "id": g["id"], "label": g["label"],
            "mode": dict(MODES).get(g["mode"], g["mode"]),
            "assignment": g["assignment"],
            "charWpm": g["char_wpm"], "effWpm": g["eff_wpm"],
            "created": g["created_at"], "closed": g["closed_at"],
            "notes": _col(g, "notes"),
        })
        for sv in v.sessions:
            s = sv.row
            sessions.append({
                "id": s["id"], "gid": g["id"], "seq": s["seq"],
                "at": s["started_at"], "notes": _col(s, "notes"),
            })
            for rv in sv.runs:
                cells = []
                if rv.grade:
                    for gr in rv.grade.groups:
                        cells.append([gr.sent, gr.recv,
                                      "".join(KIND_CODE[c.kind] for c in gr.cells)])
                runs.append({
                    "sid": s["id"], "gid": g["id"], "seq": rv.seq,
                    "final": rv.is_final, "at": rv.recorded_at,
                    "day": (rv.recorded_at or "")[:10],
                    "graded": bool(rv.grade), "cells": cells,
                })
    return {
        "generated": now_iso(),
        "troubleThreshold": TROUBLE_THRESHOLD,
        "groups": groups, "sessions": sessions, "runs": runs,
    }


def _col(row, name):
    """Read a column that may not exist in an older database row."""
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


CSS = """
:root{
  --bg:#f7f7f5; --panel:#fff; --ink:#1b1b19; --muted:#6b6b66; --line:#e3e2dd;
  --accent:#3b5bdb; --ok:#2f8f5b; --okbg:#e8f5ee; --warn:#b8860b;
  --bad:#c0392b; --badbg:#fdecea; --trans:#7c4dbd; --transbg:#f1eafb;
  --barbg:#eceae4; --chip:#f0efea; --barL:42%;
}
@media (prefers-color-scheme: dark){
  :root{
    --bg:#16171a; --panel:#1e2024; --ink:#e8e8e6; --muted:#9a9a95; --line:#2e3137;
    --accent:#7f9cf5; --ok:#5fbf8a; --okbg:#17302a; --warn:#d8a13a;
    --bad:#f0736a; --badbg:#3a1e1c; --trans:#b493e8; --transbg:#2b2140;
    --barbg:#2a2d33; --chip:#26292e; --barL:55%;
  }
}
*{box-sizing:border-box}
body{margin:0;padding:0 0 5rem;background:var(--bg);color:var(--ink);
  font:15px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif;}
.wrap{max-width:1080px;margin:0 auto;padding:0 1.25rem}
h1{font-size:1.5rem;margin:0}
h3{font-size:.8rem;margin:0 0 .6rem;color:var(--muted);
   text-transform:uppercase;letter-spacing:.06em;font-weight:650}
.sub{color:var(--muted);font-size:.85rem;margin:.15rem 0 0}

/* sticky scope bar */
.bar{position:sticky;top:0;z-index:20;background:var(--bg);
  border-bottom:1px solid var(--line);padding:.85rem 0 .8rem;margin-bottom:1.25rem}
.bar .wrap{display:flex;flex-wrap:wrap;gap:.6rem 1rem;align-items:center}
.quick{display:flex;gap:.35rem;flex-wrap:wrap}
.quick button{font:inherit;font-size:.82rem;padding:.3rem .7rem;border-radius:999px;
  border:1px solid var(--line);background:var(--panel);color:var(--ink);cursor:pointer}
.quick button:hover{border-color:var(--accent)}
.quick button[aria-pressed="true"]{background:var(--accent);border-color:var(--accent);color:#fff}
select{font:inherit;font-size:.85rem;padding:.32rem .5rem;border-radius:7px;
  border:1px solid var(--line);background:var(--panel);color:var(--ink);max-width:100%}
.scopeline{font-size:.85rem;color:var(--muted);flex:1 1 100%}
.scopeline b{color:var(--ink)}

.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:.7rem;margin:0 0 1.1rem}
.tile{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:.7rem .85rem}
.tile .n{font-size:1.45rem;font-weight:650;font-variant-numeric:tabular-nums;line-height:1.15}
.tile .l{font-size:.72rem;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin-top:.15rem}
.tile .d{font-size:.72rem;color:var(--muted);margin-top:.1rem}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;
  padding:1rem 1.15rem;margin:0 0 1.1rem}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:1.1rem;align-items:start}
@media (max-width:720px){.cols{grid-template-columns:1fr}}

.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{border-collapse:collapse;width:100%;font-size:.87rem}
th,td{text-align:left;padding:.4rem .55rem;border-bottom:1px solid var(--line);white-space:nowrap}
th{font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);font-weight:600}
tbody tr:last-child td{border-bottom:none}
tbody tr.click{cursor:pointer}
tbody tr.click:hover{background:var(--chip)}
.num{text-align:right;font-variant-numeric:tabular-nums}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:.08em}
.ch{display:inline-block;min-width:1.3em;text-align:center;padding:.05em .1em;border-radius:3px}
.k-c{color:var(--ink)}
.k-m{background:var(--badbg);color:var(--bad);font-weight:700}
.k-w{background:var(--badbg);color:var(--bad);font-weight:700}
.k-t{background:var(--transbg);color:var(--trans);font-weight:700}
.k-x{background:var(--transbg);color:var(--trans);text-decoration:underline}
.pill{display:inline-block;padding:.08rem .45rem;border-radius:999px;font-size:.75rem;font-weight:600}
.pill.ok{background:var(--okbg);color:var(--ok)}
.pill.trans{background:var(--transbg);color:var(--trans)}
.pill.pending{background:var(--barbg);color:var(--muted)}
.trouble{display:flex;flex-wrap:wrap;gap:.4rem}
.tr-chip{display:flex;align-items:baseline;gap:.35rem;background:var(--badbg);color:var(--bad);
  border:1px solid color-mix(in srgb,var(--bad) 30%,transparent);
  border-radius:7px;padding:.28rem .55rem;font-weight:700;font-size:1rem}
.tr-chip small{font-size:.7rem;font-weight:600;opacity:.85}
.bar-g{position:relative;height:7px;background:var(--barbg);border-radius:4px;min-width:60px;overflow:hidden}
.bar-g>i{position:absolute;inset:0 auto 0 0;border-radius:4px}
.sparkcap{font-size:.82rem;color:var(--muted);font-variant-numeric:tabular-nums;
  min-height:1.3em;margin-bottom:.2rem}
circle.hit{cursor:crosshair}
.none{color:var(--muted);font-style:italic;font-size:.87rem}
details{margin:.4rem 0}
summary{cursor:pointer;color:var(--accent);font-size:.84rem;user-select:none}
.legend{display:flex;gap:.9rem;flex-wrap:wrap;font-size:.76rem;color:var(--muted);margin-top:.5rem}
.spark{display:block;max-width:100%;height:auto}
.note{background:var(--transbg);border-radius:7px;padding:.45rem .7rem;
  font-size:.8rem;color:var(--muted);margin-top:.6rem;white-space:pre-wrap}
.delta.up{color:var(--ok)} .delta.down{color:var(--bad)}
"""


HTML_SHELL = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title><style>__CSS__</style></head>
<body>
<div class="bar"><div class="wrap">
  <div class="quick" id="quick"></div>
  <label>Scope <select id="scope"></select></label>
  <div class="scopeline" id="scopeline"></div>
</div></div>
<div class="wrap">
  <h1>__TITLE__</h1>
  <p class="sub" id="sub"></p>
  <noscript><p class="note">This report needs JavaScript to filter and total
  the results. The data is embedded in this file either way.</p></noscript>
  <div id="app"></div>
</div>
<script id="data" type="application/json">__DATA__</script>
<script>__APP__</script>
</body></html>
"""

APP_JS = r"""
const DATA = JSON.parse(document.getElementById('data').textContent);
const TH = DATA.troubleThreshold;
const MISS = {m:'missed', w:'wrong', t:'transposed'};
const byId = (a,k) => Object.fromEntries(a.map(x => [x[k], x]));
const G = byId(DATA.groups, 'id'), S = byId(DATA.sessions, 'id');
const graded = DATA.runs.filter(r => r.graded);
const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const day = iso => (iso || '').slice(0, 10);
const fmtDay = d => d ? new Date(d + 'T12:00:00').toLocaleDateString(undefined,
  {month:'short', day:'numeric', year:'numeric'}) : '-';
const wpm = g => g.charWpm == null ? 'speed not recorded'
  : `${+g.charWpm}/${+g.effWpm} wpm`;

/* ---------- aggregation: sum the per-character verdicts ---------- */
function stats(runs){
  const sent = new Map(), miss = new Map(), conf = new Map();
  let chars = 0, wrong = 0, grp = 0, clean = 0, transposed = 0;
  const bump = (m, k, n=1) => m.set(k, (m.get(k) || 0) + n);
  for (const r of runs) for (const [snt, rcv, kinds] of r.cells){
    if (snt) { grp++; }
    let bad = 0, extra = false;
    for (let i = 0; i < kinds.length; i++){
      const k = kinds[i], c = snt[i];
      if (k === 'x'){ extra = true; continue; }
      chars++; bump(sent, c);
      if (k !== 'c'){
        wrong++; bad++;
        if (!miss.has(c)) miss.set(c, {missed:0, wrong:0, transposed:0, total:0});
        const m = miss.get(c); m[MISS[k]]++; m.total++;
        if (k === 'w') bump(conf, c + '→' + (rcv[i] || '?'));
      }
    }
    if (snt && !bad && !extra) clean++;
    if (kinds.includes('t')) transposed++;
  }
  return {sent, miss, conf, chars, wrong, grp, clean, transposed,
          pctWrong: chars ? 100 * wrong / chars : 0,
          pctRight: chars ? 100 - 100 * wrong / chars : 0};
}
const trouble = st => [...st.miss.entries()]
  .filter(([, m]) => m.total >= TH)
  .sort((a, b) => b[1].total - a[1].total || a[0].localeCompare(b[0]));

/* ---------- scopes ---------- */
const days = [...new Set(graded.map(r => r.day))].sort();
function scopeRuns(sc){
  if (sc.t === 'all') return graded;
  if (sc.t === 'day') return graded.filter(r => r.day === sc.k);
  if (sc.t === 'group') return graded.filter(r => r.gid === +sc.k);
  return graded.filter(r => r.sid === +sc.k);
}
function scopeLabel(sc){
  if (sc.t === 'all') return 'All time';
  if (sc.t === 'day') return fmtDay(sc.k);
  if (sc.t === 'group') return G[sc.k].label;
  const s = S[sc.k];
  return `${G[s.gid].label} · session ${s.seq}`;
}
/* the level below the current one, used for the trouble breakdown columns */
function children(sc, runs){
  const key = sc.t === 'group' ? 'sid' : sc.t === 'session' ? 'seq' : 'gid';
  const out = new Map();
  for (const r of runs){
    const k = r[key];
    if (!out.has(k)) out.set(k, []);
    out.get(k).push(r);
  }
  const name = k => sc.t === 'group' ? 'S' + S[k].seq
    : sc.t === 'session' ? 'R' + k : G[k].label.replace(/ · .*/, '');
  const short = k => sc.t === 'group' ? 'S' + S[k].seq
    : sc.t === 'session' ? 'R' + k : 'G' + k;
  return [...out.entries()].map(([k, rs]) => ({k, name: name(k), short: short(k), runs: rs}));
}

/* ---------- render helpers ---------- */
const tile = (n, l, d) => `<div class="tile"><div class="n">${esc(n)}</div>
  <div class="l">${esc(l)}</div>${d ? `<div class="d">${d}</div>` : ''}</div>`;
/* red -> green by score. `floor` sets where the scale bottoms out: run
   accuracy is interesting between 50-100%, miss rate between 0-40%. */
const hue = (pct, floor) => Math.max(0, Math.min(130, (pct - floor) / (100 - floor) * 130));
const bar = (f, pct, floor = 50) => `<div class="bar-g"><i style="width:${
  Math.max(0, Math.min(1, f)) * 100}%;background:hsl(${hue(pct, floor).toFixed(0)} 62% var(--barL))"></i></div>`;
const table = (head, rows) => rows.length
  ? `<div class="scroll"><table><thead><tr>${head}</tr></thead>
     <tbody>${rows.join('')}</tbody></table></div>` : '';

function sparkline(sc, runs){
  if (runs.length < 2) return '';
  const pts = runs.map(r => stats([r]).pctRight);
  const w = 640, h = 96, pad = 16;
  const lo = Math.min(...pts, 99), span = Math.max(100 - lo, 1);
  const step = (w - 2 * pad) / (pts.length - 1);
  const xy = pts.map((v, i) => [pad + i * step, pad + (100 - v) / span * (h - 2 * pad - 10)]);
  const d = xy.map(([x, y], i) => `${i ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`).join(' ');
  const lbl = runs.map(r => sc.t === 'session' ? 'R' + r.seq
    : `S${S[r.sid].seq}R${r.seq}`);  // scope decides how much context each tick needs
  // Per-point labels collide badly past a handful of runs, so only the two ends
  // are drawn; everything else is on hover, reported in the caption above.
  const ends = [0, xy.length - 1].map(i => `<text x="${xy[i][0].toFixed(1)}" y="${h - 2}"
    font-size="10" text-anchor="${i ? 'end' : 'start'}"
    fill="var(--muted)">${esc(lbl[i])}</text>`).join('');
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" role="img" aria-label="accuracy per run">
    <path d="${d}" fill="none" stroke="var(--accent)" stroke-width="2" stroke-linejoin="round"/>
    ${xy.map(([x, y], i) => `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="3.5"
      fill="var(--accent)"/>`).join('')}
    ${xy.map(([x, y], i) => `<circle class="hit" cx="${x.toFixed(1)}" cy="${y.toFixed(1)}"
      r="11" fill="transparent" data-lbl="${esc(lbl[i])}" data-val="${pts[i].toFixed(1)}"
      ><title>${esc(lbl[i])}: ${pts[i].toFixed(1)}%</title></circle>`).join('')}
    ${ends}
  </svg>`;
}

function recvCells(snt, rcv, kinds){
  let out = '';
  for (let i = 0; i < kinds.length; i++)
    out += `<span class="ch k-${kinds[i]}">${esc(rcv[i] || '_')}</span>`;
  return `<span class="mono">${out}</span>`;
}
function whatHappened(snt, rcv, kinds){
  const m = [], w = [], x = [];
  for (let i = 0; i < kinds.length; i++){
    if (kinds[i] === 'm') m.push(snt[i]);
    else if (kinds[i] === 'w') w.push(`${snt[i]}→${rcv[i]}`);
    else if (kinds[i] === 'x') x.push(rcv[i]);
  }
  if (kinds.includes('t')) return '<span class="pill trans">transposed</span>';
  if (!m.length && !w.length && !x.length) return '<span class="pill ok">clean</span>';
  const bits = [];
  if (m.length) bits.push('missed ' + m.join(', '));
  if (w.length) bits.push('heard ' + w.join(', '));
  if (x.length) bits.push('extra ' + x.join(', '));
  return esc(bits.join('; ')).replace(/→/g, '&rarr;');
}
"""

APP_JS2 = r"""
/* ---------- panels ---------- */
function panelHeadline(runs, st){
  const perRun = runs.map(r => stats([r]).pctRight);
  let delta = '';
  if (perRun.length > 1){
    const d = perRun[perRun.length - 1] - perRun[0];
    delta = `<span class="delta ${d >= 0 ? 'up' : 'down'}">${d >= 0 ? '▲' : '▼'}
             ${Math.abs(d).toFixed(1)} pts</span>`;
  }
  const best = perRun.length ? Math.max(...perRun) : 0;
  return `<div class="tiles">
    ${tile(st.chars, 'chars copied')}
    ${tile(st.wrong, 'chars wrong')}
    ${tile(st.pctWrong.toFixed(1) + '%', 'percent wrong')}
    ${tile(st.pctRight.toFixed(1) + '%', 'accuracy', delta)}
    ${tile(best.toFixed(1) + '%', 'best run')}
    ${tile(trouble(st).length, 'trouble letters')}</div>`;
}

function panelPractice(sc, runs, st){
  const tr = trouble(st);
  const chips = tr.length
    ? `<div class="trouble">${tr.map(([c, m]) =>
        `<span class="tr-chip">${esc(c)}<small>&times;${m.total}</small></span>`).join('')}</div>`
    : `<p class="none">nothing missed ${TH}+ times in this scope.</p>`;

  const kids = children(sc, runs);
  const showKids = tr.length && kids.length > 1;
  const kidStats = showKids ? kids.map(k => ({...k, st: stats(k.runs)})) : [];
  const head = `<th>Char</th>${kidStats.map(k =>
      `<th class="num" title="${esc(k.name)}">${esc(k.short)}</th>`).join('')}
    <th class="num">Sent</th><th class="num">Missed</th>`;
  const rows = tr.map(([c, m]) => `<tr><td class="mono" style="font-weight:700">${esc(c)}</td>
    ${kidStats.map(k => `<td class="num">${k.st.miss.get(c)?.total || ''}</td>`).join('')}
    <td class="num">${st.sent.get(c) || 0}</td>
    <td class="num" style="font-weight:700">${m.total}</td></tr>`);

  const conf = [...st.conf.entries()].sort((a, b) => b[1] - a[1]).slice(0, 12);
  const confHtml = conf.length
    ? table('<th>Sent &rarr; Heard</th><th class="num">Times</th>',
        conf.map(([k, n]) => `<tr><td class="mono">${esc(k).replace('→', ' &rarr; ')}</td>
          <td class="num">${n}</td></tr>`))
    : `<p class="none">no substitutions &mdash; every error was a
       "didn't know" or a transposition.</p>`;

  return `<div class="cols">
    <div class="panel"><h3>Practice these (missed ${TH}+ times)</h3>${chips}
      ${showKids ? table(head, rows) : ''}</div>
    <div class="panel"><h3>Confusions &mdash; heard as something else</h3>${confHtml}</div>
  </div>`;
}

function panelProgress(sc, runs){
  const spark = sparkline(sc, runs);
  if (!spark) return '';
  const pts = runs.map(r => stats([r]).pctRight);
  const cap = `${runs.length} runs · ${pts[0].toFixed(1)}% → ${pts[pts.length - 1].toFixed(1)}%`
    + ` · best ${Math.max(...pts).toFixed(1)}%`;
  return `<div class="panel"><h3>Accuracy per run</h3>
    <div class="sparkcap" id="sparkcap" data-default="${esc(cap)}">${esc(cap)}</div>
    ${spark}</div>`;
}

function panelRuns(sc, runs){
  const anyTrans = runs.some(r => r.cells.some(([, , k]) => k.includes('t')));
  const rows = runs.map((r, i) => {
    const st = stats([r]);
    const s = S[r.sid];
    const where = sc.t === 'session' ? `Run ${r.seq}`
      : `S${s.seq} R${r.seq}` + (sc.t === 'all' || sc.t === 'day'
          ? ` <span class="pill pending">${esc(G[r.gid].label)}</span>` : '');
    const missed = [...stats([r]).miss.keys()].sort().join(' ');
    return `<tr class="click" data-run="${i}">
      <td>${where} ${r.final ? '<span class="pill trans">final</span>' : ''}</td>
      <td class="num">${st.chars - st.wrong}/${st.chars}</td>
      <td class="num">${st.pctRight.toFixed(1)}%</td>
      <td class="num">${st.clean}/${st.grp}</td>
      ${anyTrans ? `<td class="num">${st.transposed || ''}</td>` : ''}
      <td class="mono">${esc(missed) || '&mdash;'}</td>
      <td style="width:110px">${bar(st.pctRight / 100, st.pctRight)}</td></tr>
      <tr id="d${i}" hidden><td colspan="${anyTrans ? 7 : 6}">${runDetail(r)}</td></tr>`;
  });
  return `<div class="panel"><h3>Runs in scope (click one for detail)</h3>
    ${table(`<th>Run</th><th class="num">Chars right</th><th class="num">Correct</th>
      <th class="num">Clean groups</th>${anyTrans ? '<th class="num">Transposed</th>' : ''}
      <th>Chars missed</th><th>Accuracy</th>`, rows)}</div>`;
}

function runDetail(r){
  const head = `<th class="num">#</th><th>Sent</th><th>You copied</th>
    <th class="num">Wrong</th><th>What happened</th>`;
  const row = ([snt, rcv, kinds], i) => `<tr><td class="num">${i + 1}</td>
    <td class="mono">${esc(snt) || '&mdash;'}</td><td>${recvCells(snt, rcv, kinds)}</td>
    <td class="num">${(kinds.match(/[mwt]/g) || []).length || ''}</td>
    <td>${whatHappened(snt, rcv, kinds)}</td></tr>`;
  const bad = r.cells.map((c, i) => [c, i]).filter(([c]) => /[mwtx]/.test(c[2]));
  return (bad.length
      ? table(head, bad.map(([c, i]) => row(c, i)))
      : '<p class="none">perfect run &mdash; nothing missed.</p>')
    + `<details><summary>Show all ${r.cells.length} groups</summary>
       ${table(head, r.cells.map(row))}
       <div class="legend">
         <span><span class="ch k-c">A</span> correct</span>
         <span><span class="ch k-m">.</span> missed</span>
         <span><span class="ch k-w">X</span> heard wrong</span>
         <span><span class="ch k-t">X</span> transposed</span>
         <span><span class="ch k-x">X</span> extra</span></div></details>`;
}

function panelChars(st){
  const chars = [...st.sent.keys()].sort((a, b) =>
    (st.miss.get(b)?.total || 0) - (st.miss.get(a)?.total || 0) || a.localeCompare(b));
  // A column of nothing but blanks is noise - only show a breakdown that happened.
  const kinds = [['missed', "Didn't know"], ['wrong', 'Heard wrong'],
                 ['transposed', 'Transposed']]
    .filter(([k]) => [...st.miss.values()].some(m => m[k] > 0));
  const rows = chars.map(c => {
    const m = st.miss.get(c) || {missed:0, wrong:0, transposed:0, total:0};
    const n = st.sent.get(c), rate = m.total / n;
    return `<tr${m.total >= TH ? ' style="font-weight:700"' : ''}>
      <td class="mono">${esc(c)}</td><td class="num">${n}</td>
      <td class="num">${m.total || ''}</td><td class="num">${(rate * 100).toFixed(0)}%</td>
      ${kinds.map(([k]) => `<td class="num">${m[k] || ''}</td>`).join('')}
      <td style="width:110px">${bar(rate, 100 - rate * 100, 60)}</td></tr>`;
  });
  return `<div class="panel"><h3>Every character in scope</h3>
    <details><summary>Show exposure and miss rate for all ${chars.length} characters</summary>
    ${table(`<th>Char</th><th class="num">Sent</th><th class="num">Missed</th>
      <th class="num">Miss %</th>
      ${kinds.map(([, l]) => `<th class="num">${l}</th>`).join('')}
      <th>Miss rate</th>`, rows)}
    </details></div>`;
}

function panelContext(sc, runs){
  if (sc.t === 'all' || sc.t === 'day') return '';
  const g = sc.t === 'group' ? G[sc.k] : G[S[sc.k].gid];
  const bits = [`<span>Mode <b>${esc(g.mode)}</b></span>`,
    `<span>Assignment <b>${esc(g.assignment)}</b></span>`,
    `<span>${esc(wpm(g))}</span>`];
  const note = sc.t === 'session' ? S[sc.k].notes : g.notes;
  return `<div class="panel"><h3>About this ${sc.t}</h3>
    <div class="scopeline" style="flex:1">${bits.join(' &middot; ')}</div>
    ${note ? `<div class="note">${esc(note)}</div>` : ''}</div>`;
}
"""

APP_JS3 = r"""
/* ---------- controller ---------- */
let sc = {t:'all', k:null};

function buildScopeSelect(){
  const sel = document.getElementById('scope');
  const opt = (v, label, n) => `<option value="${v}">${esc(label)} (${n} run${n === 1 ? '' : 's'})</option>`;
  let h = opt('all', 'All time', graded.length);
  if (days.length > 1){
    h += '<optgroup label="By day">';
    for (const d of [...days].reverse())
      h += opt('day:' + d, fmtDay(d), graded.filter(r => r.day === d).length);
    h += '</optgroup>';
  }
  h += '<optgroup label="By group">';
  for (const g of [...DATA.groups].reverse())
    h += opt('group:' + g.id, g.label, graded.filter(r => r.gid === g.id).length);
  h += '</optgroup><optgroup label="By session">';
  for (const s of [...DATA.sessions].reverse()){
    const n = graded.filter(r => r.sid === s.id).length;
    if (n) h += opt('session:' + s.id, `${G[s.gid].label} · session ${s.seq}`, n);
  }
  h += '</optgroup>';
  sel.innerHTML = h;
  sel.onchange = () => {
    const [t, k] = sel.value.split(':');
    setScope({t, k: k ?? null});
  };
}

function buildQuick(){
  const last = graded[graded.length - 1];
  const items = [['All time', {t:'all', k:null}]];
  if (days.length > 1)
    items.push([fmtDay(days[days.length - 1]), {t:'day', k:days[days.length - 1]}]);
  if (last){
    items.push(['Latest group', {t:'group', k:String(last.gid)}]);
    items.push(['Latest session', {t:'session', k:String(last.sid)}]);
  }
  document.getElementById('quick').innerHTML = items.map(([l, s], i) =>
    `<button data-i="${i}">${esc(l)}</button>`).join('');
  document.querySelectorAll('#quick button').forEach(b => {
    b.onclick = () => setScope(items[+b.dataset.i][1]);
  });
  return items;
}

function setScope(next){
  sc = next;
  const val = sc.t === 'all' ? 'all' : `${sc.t}:${sc.k}`;
  document.getElementById('scope').value = val;
  document.querySelectorAll('#quick button').forEach((b, i) => {
    const s = QUICK[i][1];
    b.setAttribute('aria-pressed', String(s.t === sc.t && String(s.k) === String(sc.k)));
  });
  try { history.replaceState(null, '', '#' + val); } catch (e) {}
  render();
}

function render(){
  const runs = scopeRuns(sc);
  const st = stats(runs);
  const sess = new Set(runs.map(r => r.sid)).size;
  const grps = new Set(runs.map(r => r.gid)).size;
  const dys = [...new Set(runs.map(r => r.day))].sort();
  const span = dys.length > 1 ? `${fmtDay(dys[0])} – ${fmtDay(dys[dys.length - 1])}`
    : fmtDay(dys[0]);
  document.getElementById('scopeline').innerHTML =
    `<b>${esc(scopeLabel(sc))}</b> — ${grps} group${grps === 1 ? '' : 's'},
     ${sess} session${sess === 1 ? '' : 's'}, ${runs.length} run${runs.length === 1 ? '' : 's'}
     · ${esc(span)}`;

  const app = document.getElementById('app');
  if (!runs.length){ app.innerHTML = '<p class="none">No graded runs in this scope.</p>'; return; }
  app.innerHTML = panelHeadline(runs, st) + panelPractice(sc, runs, st)
    + panelProgress(sc, runs) + panelRuns(sc, runs) + panelChars(st) + panelContext(sc, runs);

  app.querySelectorAll('tr.click').forEach(tr => {
    tr.onclick = () => {
      const d = document.getElementById('d' + tr.dataset.run);
      d.hidden = !d.hidden;
    };
  });

  const cap = document.getElementById('sparkcap');
  if (cap){
    app.querySelectorAll('circle.hit').forEach(c => {
      c.onmouseenter = () => {
        cap.textContent = `${c.getAttribute('data-lbl')} — ${c.getAttribute('data-val')}% correct`;
      };
    });
    const svg = app.querySelector('.spark');
    if (svg) svg.onmouseleave = () => { cap.textContent = cap.getAttribute('data-default'); };
  }
}

document.getElementById('sub').textContent =
  `${DATA.groups.length} group(s), ${DATA.sessions.length} session(s), `
  + `${graded.length} graded run(s) · generated `
  + new Date(DATA.generated).toLocaleString();

buildScopeSelect();
const QUICK = buildQuick();
const fromHash = (location.hash || '').slice(1);
const [ht, hk] = fromHash.split(':');
setScope(ht && (ht === 'all' || hk) ? {t:ht, k:hk ?? null} : {t:'all', k:null});
"""


def build_report(views: list[GroupView], title: str = "LCWO progress") -> str:
    payload = json.dumps(report_payload(views), separators=(",", ":"))
    payload = payload.replace("<", "\\u003c")  # never break out of the script tag
    return (HTML_SHELL
            .replace("__TITLE__", escape(title))
            .replace("__CSS__", CSS)
            .replace("__APP__", APP_JS + APP_JS2 + APP_JS3)
            .replace("__DATA__", payload))


def write_report(con, gid: int | None = None, out: Path | None = None) -> Path:
    views = [load_group(con, gid)] if gid else load_all(con)
    if not views:
        raise SystemExit("no data yet - record a session first")
    title = f"LCWO group {gid}" if gid else "LCWO progress"
    html = build_report(views, title)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = out or REPORT_DIR / (f"group-{gid}.html" if gid else "index.html")
    out.write_text(html, encoding="utf-8")
    return out


# --------------------------------------------------------------------------
# terminal helpers
# --------------------------------------------------------------------------

_TTY = sys.stdout.isatty()


def c(text, code) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else str(text)


def bold(t):
    return c(t, "1")


def dim(t):
    return c(t, "2")


def green(t):
    return c(t, "32")


def red(t):
    return c(t, "31")


def yellow(t):
    return c(t, "33")


def cyan(t):
    return c(t, "36")


def rule(title=""):
    w = 66
    if title:
        print("\n" + bold(f"── {title} " + "─" * max(0, w - len(title) - 4)))
    else:
        print(dim("─" * w))


class Abort(Exception):
    pass


def ask_text(prompt, default=None, allow_empty=False) -> str:
    hint = f" [{default}]" if default is not None else ""
    while True:
        try:
            v = input(f"{prompt}{hint}: ").strip()
        except EOFError:
            raise Abort()
        if not v and default is not None:
            return str(default)
        if v or allow_empty:
            return v
        print(dim("  (required)"))


def ask_number(prompt, default=None) -> float:
    while True:
        v = ask_text(prompt, default)
        try:
            return float(v)
        except ValueError:
            print(dim("  (enter a number)"))


def ask_choice(prompt, options: list[tuple[str, str]], default=1) -> str:
    print(f"\n{bold(prompt)}")
    for i, (_, label) in enumerate(options, 1):
        print(f"  {i}) {label}")
    while True:
        v = ask_text("choice", default)
        if v.isdigit() and 1 <= int(v) <= len(options):
            return options[int(v) - 1][0]
        for key, label in options:
            if v.lower() in (key.lower(), label.lower()):
                return key
        print(dim("  (pick a number from the list)"))


def ask_yes_no(prompt, default=True) -> bool:
    d = "Y/n" if default else "y/N"
    while True:
        try:
            v = input(f"{prompt} [{d}]: ").strip().lower()
        except EOFError:
            raise Abort()
        if not v:
            return default
        if v in ("y", "yes"):
            return True
        if v in ("n", "no"):
            return False


def read_paste(prompt: str) -> str:
    """Read a pasted block, terminated by a blank line.

    Returns the pasted text, or a ':done' / ':drop' sentinel. A blank line
    before anything is pasted is ignored, so a stray Enter can't end a session.
    """
    print(f"\n{bold(prompt)}")
    print(dim("  paste, then press Enter on an empty line."))
    print(dim("  :done  stop this session   :drop  discard it   :q  quit"))
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            return ":done" if not lines else "\n".join(lines)
        cmd = line.strip().lower()
        if not lines and cmd in (":q", ":quit", "quit", "exit"):
            raise Abort()
        if not lines and cmd in (":done", "done", ":end"):
            return ":done"
        if not lines and cmd in (":drop", ":abort", ":discard"):
            return ":drop"
        if not line.strip():
            if lines:
                break
            continue
        lines.append(line)
    return "\n".join(lines)


# --------------------------------------------------------------------------
# interactive recording
# --------------------------------------------------------------------------


def print_run_summary(g: RunGrade, label: str) -> None:
    pct = g.pct_right
    color = green if pct >= 95 else (yellow if pct >= 85 else red)
    print(
        f"  {label}: {color(f'{pct:.1f}% correct')}  "
        f"{g.right_chars}/{g.total_chars} chars  "
        f"{g.wrong_chars} wrong ({g.pct_wrong:.1f}%)  "
        f"{g.clean_groups}/{g.total_groups} clean groups"
        + (f"  {len(g.transposed_groups)} transposed" if g.transposed_groups else "")
    )
    bad = [gr for gr in g.groups if not gr.clean]
    for gr in bad[:12]:
        detail = []
        for cell in gr.cells:
            if cell.kind == "missed":
                detail.append(f"missed {cell.sent}")
            elif cell.kind == "wrong":
                detail.append(f"{cell.sent}->{cell.recv}")
            elif cell.kind == "extra":
                detail.append(f"extra {cell.recv}")
        note = "transposed" if gr.transposed else ", ".join(detail)
        print(f"      #{gr.idx + 1:>2}  sent {bold(gr.sent or '--'):<6} "
              f"got {red(gr.recv or '--'):<6}  {dim(note)}")
    if len(bad) > 12:
        print(dim(f"      ... and {len(bad) - 12} more"))


def record_session(con, grp, sess) -> str:
    """Run the paste loop for one session. Returns graded | pending | drop."""
    rule(f"Group {grp['id']} · Session {sess['seq']}")
    recorded = 0
    while True:
        text = read_paste(
            f"Session {sess['seq']}, run {recorded + 1} — paste your answers, "
            "or the LCWO results table to grade and finish"
        )
        if text == ":drop":
            return "drop"
        if text == ":done" or not text.strip():
            if not recorded:
                return "drop"
            print(dim("  no results table yet — keeping this session ungraded"))
            return "pending"

        p = parse_paste(text)
        for w in p.warnings:
            print(yellow(f"  ! {w}"))

        if p.kind == "results":
            add_run(con, sess["id"], p.groups, text, is_final=True, reported=p.reported)
            finish_session(con, sess["id"], p.key)
            print(green(f"  ✓ results table read: {len(p.key)} groups. Session graded."))
            return "graded"

        if not p.groups:
            print(yellow("  ! nothing recognized in that paste, try again"))
            continue
        add_run(con, sess["id"], p.groups, text)
        recorded += 1
        print(f"  {cyan('recorded')} run {recorded}: {len(p.groups)} groups, "
              f"{sum(g.count('.') for g in p.groups)} marked unknown")
        if recorded >= SOFT_RUN_LIMIT:
            print(dim(f"  (that's {recorded} runs — submit on LCWO when you're happy "
                      "and paste the results table)"))


def report_session(con, grp, sess) -> None:
    v = load_group(con, grp["id"])
    sv = next((s for s in v.sessions if s.row["id"] == sess["id"]), None)
    if not sv or not sv.graded:
        return
    rule(f"Session {sess['seq']} results")
    for rv in sv.runs:
        if rv.grade:
            print_run_summary(rv.grade, f"run {rv.seq}" + (" (final)" if rv.is_final else ""))
    st = trouble_from(sv.miss_counts())
    gt = v.trouble()
    print()
    print("  session trouble: " + (", ".join(f"{ch}×{n}" for ch, n in st) if st else dim("none")))
    print("  group trouble:   " + (", ".join(f"{ch}×{n}" for ch, n in gt) if gt else dim("none")))


def group_line(con, g) -> str:
    ns = con.execute("SELECT COUNT(*) FROM sessions WHERE group_id=?",
                     (g["id"],)).fetchone()[0]
    nr = con.execute("SELECT COUNT(*) FROM runs r JOIN sessions s ON s.id=r.session_id"
                     " WHERE s.group_id=?", (g["id"],)).fetchone()[0]
    speed = (f"{fmt_wpm(g['char_wpm'])}/{fmt_wpm(g['eff_wpm'])} wpm"
             if g["char_wpm"] is not None else "speed not recorded")
    state = "closed" if g["closed_at"] else "open"
    return (f"{ns} session(s), {nr} run(s) · {speed} · {state}")


def choose_group(con):
    rows = all_groups(con)
    rule("All groups")
    for g in rows:
        print(f"  {g['id']:>3}) {g['label']:<28} {dim(group_line(con, g))}")
    while True:
        v = ask_text("which group (blank to start a new one)", "", allow_empty=True)
        if not v:
            return None
        if v.isdigit() and any(int(v) == g["id"] for g in rows):
            return get_group(con, int(v))
        print(dim("  (pick an id from the list, or press Enter for a new group)"))


def pick_group(con):
    last = last_group(con)
    if last is None:
        return new_group(con)

    rule("Resume or start new")
    print(f"  most recent:  {bold(last['label'])}  {dim('(group ' + str(last['id']) + ')')}")
    print(f"                {dim(group_line(con, last))}")
    when = last["activity"] if "activity" in last.keys() else last["created_at"]
    print(f"                {dim('last worked ' + fmt_ts(when))}")

    choice = ask_choice("What would you like to do?", [
        ("resume", f"Add a session to group {last['id']} ({last['label']})"),
        ("new", "Start a new group"),
        ("other", "Pick a different group"),
    ], default=1)

    if choice == "resume":
        if last["closed_at"]:
            reopen_group(con, last["id"])
            print(dim(f"  reopened group {last['id']}"))
        return get_group(con, last["id"])
    if choice == "other":
        picked = choose_group(con)
        if picked is not None:
            if picked["closed_at"]:
                reopen_group(con, picked["id"])
                print(dim(f"  reopened group {picked['id']}"))
            return get_group(con, picked["id"])
    return new_group(con)


def new_group(con):
    while True:
        rule("New group")
        mode = ask_choice("1. What kind of drill?", MODES)
        assignment = ask_text("2. Assignment number")
        char_wpm = ask_number("3. Character speed (wpm)", 20)
        eff_wpm = ask_number("4. Effective speed (wpm)", fmt_wpm(char_wpm))
        print(f"\n{bold('5. Confirm')}")
        print(f"     drill:      {dict(MODES)[mode]}")
        print(f"     assignment: {assignment}")
        print(f"     char speed: {fmt_wpm(char_wpm)} wpm")
        print(f"     effective:  {fmt_wpm(eff_wpm)} wpm")
        if ask_yes_no("   look right?", True):
            gid = create_group(con, mode, assignment, char_wpm, eff_wpm)
            print(green(f"\n  ✓ group {gid} created"))
            return get_group(con, gid)
        print(dim("  starting over\n"))


def cmd_record(args) -> int:
    con = connect()
    ns, ng = prune_empty(con)  # clear anything a previous interrupt left behind
    if ns or ng:
        print(dim(f"  cleared {ns} empty session(s) and {ng} empty group(s) "
                  "left by an earlier run"))
    grp = None
    try:
        grp = pick_group(con)
        while True:
            sess = start_session(con, grp)
            try:
                status = record_session(con, grp, sess)
            except (Abort, KeyboardInterrupt):
                # keep the session only if something was actually recorded
                prune_empty(con)
                raise
            if status == "drop":
                delete_session(con, sess["id"])
                print(dim("  session discarded"))
            elif status == "pending":
                print(yellow(f"  session {sess['id']} left ungraded — "
                             "run `python3 lcwo.py key` once you have the results table"))
            else:
                report_session(con, grp, sess)
            done = len(group_sessions(con, grp["id"]))
            if done >= SOFT_SESSION_LIMIT:
                print(dim(f"\n  ({done} sessions in this group)"))
            if not ask_yes_no("\nAnother session in this group?", done < SOFT_SESSION_LIMIT):
                break
        if ask_yes_no("Close this group?", True):
            close_group(con, grp["id"])
    except (Abort, KeyboardInterrupt):
        print(dim("\n  bye — everything recorded so far is saved"))
    finally:
        prune_empty(con)
        try:
            still_there = grp is not None and get_group(con, grp["id"]) is not None
            if con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]:
                out = write_report(con)
                print(green(f"\n  ✓ report: {out}"))
                if still_there:
                    print(green(f"  ✓ group:  {write_report(con, grp['id'])}"))
                if _TTY and ask_yes_no("Open the report?", True):
                    webbrowser.open(out.resolve().as_uri())
        except (Abort, KeyboardInterrupt):
            pass
        con.close()
    return 0


# --------------------------------------------------------------------------
# other commands
# --------------------------------------------------------------------------


def cmd_groups(args) -> int:
    con = connect()
    views = load_all(con)
    if not views:
        print(dim("no groups yet — run `python3 lcwo.py` to start one"))
        return 0
    rule("Groups")
    print(f"  {'id':>3}  {'label':<40} {'sess':>4} {'runs':>4} {'wrong':>6} {'trouble'}")
    for v in views:
        runs = v.graded_runs
        tot = sum(r.grade.total_chars for r in runs)
        wr = sum(r.grade.wrong_chars for r in runs)
        pct = f"{100.0 * wr / tot:.1f}%" if tot else "-"
        tr = ",".join(ch for ch, _ in v.trouble()) or "-"
        state = "" if not v.row["closed_at"] else dim(" (closed)")
        print(f"  {v.row['id']:>3}  {v.row['label'][:40]:<40} {len(v.sessions):>4} "
              f"{len(runs):>4} {pct:>6}  {tr}{state}")
    con.close()
    return 0


def cmd_trouble(args) -> int:
    con = connect()
    views = [load_group(con, args.group)] if args.group else load_all(con)
    counts = global_miss_counts(views)
    sent: Counter = Counter()
    for v in views:
        sent.update(v.sent_counts())
    tr = trouble_from(counts, args.threshold)
    scope = f"group {args.group}" if args.group else "all time"
    rule(f"Trouble letters — {scope} (missed {args.threshold}+ times)")
    if not tr:
        print(dim("  none yet"))
    for ch, n in tr:
        s = sent.get(ch, 0)
        rate = f"{100.0 * n / s:.0f}%" if s else "-"
        bar = "█" * min(30, n)
        print(f"  {bold(ch)}  missed {n:>3} of {s:>3} sent  ({rate:>4})  {red(bar)}")
    con.close()
    return 0


def cmd_report(args) -> int:
    con = connect()
    out = write_report(con, args.group, Path(args.out) if args.out else None)
    print(green(f"  ✓ {out}"))
    if args.open:
        webbrowser.open(out.resolve().as_uri())
    con.close()
    return 0


def cmd_key(args) -> int:
    """Attach a results table to a session that was left ungraded."""
    con = connect()
    pending = con.execute(
        "SELECT s.*, g.label FROM sessions s JOIN groups g ON g.id = s.group_id"
        " WHERE s.key_json IS NULL ORDER BY s.started_at"
    ).fetchall()
    if not pending:
        print(dim("no ungraded sessions"))
        return 0
    if args.session:
        sess = con.execute("SELECT * FROM sessions WHERE id=?", (args.session,)).fetchone()
        if sess is None:
            raise SystemExit(f"no such session: {args.session}")
    else:
        rule("Ungraded sessions")
        for s in pending:
            n = len(session_runs(con, s["id"]))
            print(f"  {s['id']}) {s['label']} · session {s['seq']} · {n} run(s) · "
                  f"{fmt_ts(s['started_at'])}")
        sid = ask_text("which session", str(pending[0]["id"]))
        sess = con.execute("SELECT * FROM sessions WHERE id=?", (int(sid),)).fetchone()
        if sess is None:
            raise SystemExit("no such session")

    try:
        text = read_paste("Paste the LCWO results table")
    except Abort:
        return 0
    if text in (":done", ":drop") or not text.strip():
        return 0
    p = parse_paste(text)
    for w in p.warnings:
        print(yellow(f"  ! {w}"))
    if p.kind != "results":
        print(red("  that doesn't look like a results table (need Sent / Received / Errors)"))
        return 1
    add_run(con, sess["id"], p.groups, text, is_final=True, reported=p.reported)
    finish_session(con, sess["id"], p.key)
    grp = get_group(con, sess["group_id"])
    print(green(f"  ✓ session {sess['id']} graded"))
    report_session(con, grp, sess)
    con.close()
    return 0


def cmd_speed(args) -> int:
    """Fill in the speed for groups whose source note never recorded one."""
    con = connect()
    if args.char is None and args.eff is None:  # nothing to set: just report
        sql = "SELECT id, label, char_wpm, eff_wpm FROM groups"
        params = ()
        if args.group is not None:
            sql += " WHERE id=?"
            params = (args.group,)
        rows = con.execute(sql + " ORDER BY created_at", params).fetchall()
        if not rows:
            raise SystemExit(f"no such group: {args.group}")
        rule("Speeds")
        for r in rows:
            known = r["char_wpm"] is not None
            val = (f"{fmt_wpm(r['char_wpm'])}/{fmt_wpm(r['eff_wpm'])} wpm" if known
                   else "not recorded")
            print(f"  {r['id']:>3}  {r['label']:<26} {val if known else yellow(val)}")
        print(dim("\n  set one with:  python3 lcwo.py speed -g ID --char 25 --eff 6"))
        return 0
    if args.group is None:
        raise SystemExit("which group? pass -g ID")
    if args.char is None or args.eff is None:
        raise SystemExit("need both --char and --eff")
    n = con.execute("UPDATE groups SET char_wpm=?, eff_wpm=? WHERE id=?",
                    (args.char, args.eff, args.group)).rowcount
    con.execute("UPDATE sessions SET char_wpm=?, eff_wpm=? WHERE group_id=?",
                (args.char, args.eff, args.group))
    con.commit()
    if not n:
        raise SystemExit(f"no such group: {args.group}")
    print(green(f"  ✓ group {args.group} set to "
                f"{fmt_wpm(args.char)}/{fmt_wpm(args.eff)} wpm"))
    con.close()
    return 0


# --------------------------------------------------------------------------
# selftest
# --------------------------------------------------------------------------

SAMPLE_RESULTS = """Sent Group\tReceived Group\tErrors
EH\tEH\tEH\t0
SM\tSM\tSM\t0
TR\tTR\tTR\t0
BU\tBU\tBU\t0
EQ\tEQ\tEQ\t0
KJ\tKJ\tKJ\t0
VL\tVL\tVL\t0
HR\tSR\tHR\t1
CF\tCF\tCF\t0
YC\tYC\tYC\t0
TB\tTB\tTB\t0
GR\tGR\tGR\t0
JZ\tJZ\tJZ\t0
NF\tNF\tNF\t0
NM\tNM\tNM\t0
WM\tWM\tWM\t0"""


def cmd_selftest(args) -> int:
    fails = []

    def check(name, cond):
        (print(green(f"  ok   {name}")) if cond
         else (fails.append(name), print(red(f"  FAIL {name}"))))

    rule("selftest")

    p = parse_paste(SAMPLE_RESULTS)
    check("results table detected", p.kind == "results")
    check("16 groups parsed", len(p.key) == 16 and len(p.groups) == 16)
    check("diff column ignored", p.key[7] == "HR" and p.groups[7] == "SR")
    check("errors read", p.reported[7] == 1 and sum(p.reported) == 1)

    g = grade_run(p.key, p.groups, p.reported)
    check("32 chars", g.total_chars == 32)
    check("1 wrong char", g.wrong_chars == 1)
    check("pct wrong", abs(g.pct_wrong - 3.125) < 1e-9)
    check("15/16 clean groups", g.clean_groups == 15)
    check("H counted as missed once", g.miss_counts()["H"] == 1)
    check("confusion H->S", g.confusions()[("H", "S")] == 1)
    check("no LCWO disagreement", not any(x.disagrees_with_lcwo for x in g.groups))

    a = parse_paste("EH\nsm\n.R\nBU\nHE\n")
    check("attempt detected", a.kind == "attempt")
    check("attempt uppercased", a.groups == ["EH", "SM", ".R", "BU", "HE"])
    a2 = parse_paste("EH SM .R BU HE")
    check("space-separated attempt", a2.groups == a.groups)

    g2 = grade_run(["EH", "SM", "TR", "BU", "EH"], a.groups)
    check("dot = missed", g2.groups[2].cells[0].kind == "missed")
    check("substitution flagged", g2.groups[3].clean)
    check("transposition flagged", g2.groups[4].transposed)
    check("transposed chars counted", g2.miss_counts()["E"] == 1 and g2.miss_counts()["H"] == 1)
    check("wrong char T->._", g2.miss_counts()["T"] == 1)

    short = grade_run(["EH", "SM", "TR"], ["EH"])
    check("short attempt = all missed", short.wrong_chars == 4)
    extra = grade_run(["EH"], ["EHX"])
    check("extra char flagged", extra.groups[0].cells[2].kind == "extra")
    check("extra is not clean", not extra.groups[0].clean)

    mism = parse_paste("Sent Group\tReceived Group\tErrors\nEH\tEX\tEH\t0")
    gm = grade_run(mism.key, mism.groups, mism.reported)
    check("LCWO disagreement caught", gm.groups[0].disagrees_with_lcwo)

    check("trouble threshold", trouble_from(Counter({"A": 2, "B": 1})) == [("A", 2)])

    # end-to-end through the db + report
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        con = connect(Path(td) / "t.db")
        gid = create_group(con, "letters", "12", 20, 10)
        grp = get_group(con, gid)
        s = start_session(con, grp)
        add_run(con, s["id"], ["EH", "SM", ".R"], "raw")
        pr = parse_paste(SAMPLE_RESULTS)
        add_run(con, s["id"], pr.groups, SAMPLE_RESULTS, is_final=True, reported=pr.reported)
        finish_session(con, s["id"], pr.key)
        v = load_group(con, gid)
        check("session loaded", len(v.sessions) == 1 and len(v.sessions[0].runs) == 2)
        check("earlier run graded retroactively", v.sessions[0].runs[0].grade is not None)
        check("R missed twice -> trouble", dict(v.trouble()).get("R", 0) >= 2)
        html = build_report([v])
        check("html builds", 'id="scope"' in html and 'id="data"' in html)
        blob = re.search(r'<script id="data"[^>]*>(.*?)</script>', html, re.S).group(1)
        payload = json.loads(blob)
        check("payload carries both runs", len(payload["runs"]) == 2)
        check("payload carries verdicts", payload["runs"][1]["cells"][7] == ["HR", "SR", "wc"])
        check("payload has no raw '<'", "<" not in blob)

        # resuming: the most recently worked group is what `record` offers first
        check("last_group finds the recent group", last_group(con)["id"] == gid)
        older = create_group(con, "letters", "old", 20, 10, label="older",
                             created_at="2000-01-01T00:00:00+00:00")
        os_ = start_session(con, get_group(con, older),
                            started_at="2000-01-01T00:00:00+00:00")
        add_run(con, os_["id"], ["EH"], "raw")
        check("last_group ignores older activity", last_group(con)["id"] == gid)
        close_group(con, gid)
        check("last_group still finds a closed group", last_group(con)["id"] == gid)
        reopen_group(con, gid)
        check("reopen clears closed_at", get_group(con, gid)["closed_at"] is None)

        # an interrupted start must not leave an empty group behind
        ghost = create_group(con, "letters", "ghost", 20, 10, label="ghost")
        start_session(con, get_group(con, ghost))
        before = con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        ns, ng = prune_empty(con)
        check("prune removes the empty session and group", ns == 1 and ng == 1)
        check("prune kept every run", con.execute(
            "SELECT COUNT(*) FROM runs").fetchone()[0] == before)
        check("pruned group is gone", get_group(con, ghost) is None)
        check("populated group survives prune", get_group(con, gid) is not None)
        check("prune is idempotent", prune_empty(con) == (0, 0))

        # a hostile label must not be able to close the data script tag
        nasty = create_group(con, "letters", "x", 20, 10,
                             label='</script><img src=x onerror=alert(1)>')
        ns = start_session(con, get_group(con, nasty))
        pr2 = parse_paste(SAMPLE_RESULTS)
        add_run(con, ns["id"], pr2.groups, SAMPLE_RESULTS, is_final=True)
        finish_session(con, ns["id"], pr2.key)
        h2 = build_report(load_all(con))
        blob2 = re.search(r'<script id="data"[^>]*>(.*?)</script>', h2, re.S).group(1)
        check("script-tag breakout escaped", "</script>" not in blob2
              and "\\u003c/script" in blob2)
        check("hostile label still parses", any(
            g["label"].startswith("</script>") for g in json.loads(blob2)["groups"]))
        con.close()

    print()
    if fails:
        print(red(f"  {len(fails)} check(s) failed"))
        return 1
    print(green("  all checks passed"))
    return 0


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="lcwo", description="Grade LCWO practice assignments.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="run with no arguments to start recording",
    )
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("record", help="record sessions (default)").set_defaults(fn=cmd_record)
    sub.add_parser("groups", help="list groups").set_defaults(fn=cmd_groups)

    r = sub.add_parser("report", help="build the HTML report")
    r.add_argument("-g", "--group", type=int, help="limit to one group id")
    r.add_argument("-o", "--out", help="output path")
    r.add_argument("--open", action="store_true", help="open in a browser")
    r.set_defaults(fn=cmd_report)

    t = sub.add_parser("trouble", help="show trouble letters")
    t.add_argument("-g", "--group", type=int)
    t.add_argument("-n", "--threshold", type=int, default=TROUBLE_THRESHOLD)
    t.set_defaults(fn=cmd_trouble)

    k = sub.add_parser("key", help="attach a results table to an ungraded session")
    k.add_argument("-s", "--session", type=int)
    k.set_defaults(fn=cmd_key)

    sp = sub.add_parser("speed", help="show or set a group's speed")
    sp.add_argument("-g", "--group", type=int)
    sp.add_argument("--char", type=float, help="character speed in wpm")
    sp.add_argument("--eff", type=float, help="effective speed in wpm")
    sp.set_defaults(fn=cmd_speed)

    sub.add_parser("selftest", help="run built-in checks").set_defaults(fn=cmd_selftest)

    args = ap.parse_args(argv)
    if not getattr(args, "fn", None):
        args.fn = cmd_record
    try:
        return args.fn(args)
    except (Abort, KeyboardInterrupt):
        print(dim("\n  bye"))
        return 130


if __name__ == "__main__":
    sys.exit(main())
