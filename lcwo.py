#!/usr/bin/env python3
"""
lcwo.py - grade LCWO (Learn CW Online) practice assignments.

Hierarchy:  operator  >  group  >  session  >  run

  operator who is copying - a name and a call sign. Every group belongs to
           one, so several operators can share a database.
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
    python3 lcwo.py user            list operators, add or switch
    python3 lcwo.py selftest        run the built-in checks
"""

from __future__ import annotations

import argparse
import json
import os
import random
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
    ("custom", "Error practice"),
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

-- Who is copying. Every group belongs to one operator, so one database can
-- hold more than one person's practice without mixing their numbers.
CREATE TABLE IF NOT EXISTS operators (
    id          INTEGER PRIMARY KEY,
    callsign    TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    notes       TEXT
);

-- small key/value bag; for now only which operator is being recorded for
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT
);

CREATE TABLE IF NOT EXISTS groups (
    id          INTEGER PRIMARY KEY,
    operator_id INTEGER REFERENCES operators(id),
    label       TEXT,
    -- mode/speed on a group are only the defaults carried into the next
    -- session; each session records its own, so these may be NULL
    mode        TEXT,
    assignment  TEXT NOT NULL,
    char_wpm    REAL,
    eff_wpm     REAL,
    created_at  TEXT NOT NULL,
    closed_at   TEXT,
    notes       TEXT,
    source      TEXT UNIQUE,
    deleted_at  TEXT
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
    deleted_at  TEXT,
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
    deleted_at   TEXT,
    UNIQUE (session_id, seq)
);
"""

# Applied after migrate(): an older database has yet to grow the columns these
# name, so they cannot live in SCHEMA.
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_groups_operator ON groups(operator_id);
CREATE INDEX IF NOT EXISTS idx_sessions_group ON sessions(group_id);
CREATE INDEX IF NOT EXISTS idx_runs_session ON runs(session_id);
"""

# Deleting is soft: rows stay put and gain a deleted_at stamp. Visibility
# cascades by containment - a run disappears when its session or group is
# deleted, without touching the child rows - so restoring is a single update.
# Recreated on every connect so a definition change cannot go stale.
VIEWS = """
DROP VIEW IF EXISTS live_runs;
DROP VIEW IF EXISTS live_sessions;
DROP VIEW IF EXISTS live_groups;

CREATE VIEW live_groups AS
    SELECT * FROM groups WHERE deleted_at IS NULL;

CREATE VIEW live_sessions AS
    SELECT s.* FROM sessions s JOIN groups g ON g.id = s.group_id
    WHERE s.deleted_at IS NULL AND g.deleted_at IS NULL;

CREATE VIEW live_runs AS
    SELECT r.* FROM runs r JOIN live_sessions s ON s.id = r.session_id
    WHERE r.deleted_at IS NULL;
"""


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    migrate(con)
    # both reference columns migrate() may have just added
    con.executescript(INDEXES)
    con.executescript(VIEWS)
    return con


def table_ddl(table: str) -> str:
    """Pull one CREATE TABLE statement out of SCHEMA (single source of truth).

    Comments are stripped first: a `;` inside a `--` comment would otherwise
    split a statement in half and yield invalid SQL.
    """
    clean = "\n".join(re.sub(r"--.*$", "", ln) for ln in SCHEMA.splitlines())
    for stmt in clean.split(";"):
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
        ("groups", "deleted_at", "TEXT"),
        ("sessions", "deleted_at", "TEXT"),
        ("runs", "deleted_at", "TEXT"),
        ("groups", "operator_id", "INTEGER REFERENCES operators(id)"),
    ):
        if col not in cols(table):
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    con.commit()

    # columns that became nullable as the model changed
    relaxed = {"groups": ("char_wpm", "eff_wpm", "mode"),
               "sessions": ("char_wpm", "eff_wpm")}
    stale = [t for t, names in relaxed.items()
             if any(c["notnull"] and c["name"] in names for c in cols(t).values())]
    if not stale:
        return

    # Dropping NOT NULL is only possible by rebuilding the table in SQLite.
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
    con.executescript(INDEXES)  # recreate indexes dropped with the old tables
    bad = con.execute("PRAGMA foreign_key_check").fetchall()
    if bad:
        raise SystemExit(f"migration left dangling references: {bad[:3]}")


def create_group(con, mode=None, assignment="", char_wpm=None, eff_wpm=None,
                 label=None, created_at=None, notes=None, source=None,
                 operator_id=None) -> int:
    """A group is one homework assignment. Its mode/speed columns are only the
    defaults carried into the next session - each session records its own."""
    label = label or str(assignment)
    cur = con.execute(
        "INSERT INTO groups (operator_id, label, mode, assignment, char_wpm, eff_wpm,"
        " created_at, notes, source) VALUES (?,?,?,?,?,?,?,?,?)",
        (operator_id, label, mode, assignment, char_wpm, eff_wpm,
         created_at or now_iso(), notes, source),
    )
    con.commit()
    return cur.lastrowid


def fmt_wpm(v) -> str:
    return "?" if v is None else f"{v:g}"


# --- operators -------------------------------------------------------------
#
# Groups carry an operator_id; sessions and runs inherit it through their
# group. Which operator is current lives in `settings`, so it survives between
# invocations and `record` does not have to ask when there is only one.


def normalise_call(s) -> str:
    return re.sub(r"\s+", "", s or "").upper()


def create_operator(con, name, callsign, created_at=None, notes=None) -> int:
    name, callsign = (name or "").strip(), normalise_call(callsign)
    if not name or not callsign:
        raise ValueError("an operator needs both a name and a call sign")
    cur = con.execute(
        "INSERT INTO operators (callsign, name, created_at, notes) VALUES (?,?,?,?)",
        (callsign, name, created_at or now_iso(), notes),
    )
    con.commit()
    return cur.lastrowid


def list_operators(con) -> list[sqlite3.Row]:
    return con.execute("SELECT * FROM operators ORDER BY callsign").fetchall()


def get_operator(con, oid) -> sqlite3.Row | None:
    if oid is None:
        return None
    return con.execute("SELECT * FROM operators WHERE id=?", (oid,)).fetchone()


def find_operator(con, token) -> sqlite3.Row | None:
    """Look an operator up by id, call sign, or name - however it was typed."""
    token = str(token or "").strip()
    if not token:
        return None
    if token.isdigit():
        row = get_operator(con, int(token))
        if row:
            return row
    return con.execute(
        "SELECT * FROM operators WHERE callsign=? OR lower(name)=lower(?)",
        (normalise_call(token), token)).fetchone()


def op_label(op) -> str:
    return "nobody" if op is None else f"{op['name']} ({op['callsign']})"


def get_setting(con, key, default=None):
    row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return default if row is None else row["value"]


def set_setting(con, key, value) -> None:
    con.execute("INSERT INTO settings (key, value) VALUES (?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, None if value is None else str(value)))
    con.commit()


def current_operator(con) -> sqlite3.Row | None:
    """Who new groups are recorded for, or None if nobody is on file yet."""
    op = get_operator(con, get_setting(con, "operator_id"))
    if op is not None:
        return op
    ops = list_operators(con)
    if len(ops) == 1:  # only one candidate: adopt it rather than asking
        set_current_operator(con, ops[0]["id"])
        return ops[0]
    return None


def set_current_operator(con, oid) -> None:
    set_setting(con, "operator_id", oid)


def adopt_unassigned(con, oid) -> int:
    """Hand groups recorded before operators existed to their first owner."""
    n = con.execute("UPDATE groups SET operator_id=? WHERE operator_id IS NULL",
                    (oid,)).rowcount
    con.commit()
    return n


def operator_counts(con, oid) -> tuple[int, int, int]:
    row = con.execute(
        "SELECT (SELECT COUNT(*) FROM live_groups WHERE operator_id=?),"
        "       (SELECT COUNT(*) FROM live_sessions s JOIN live_groups g"
        "          ON g.id=s.group_id WHERE g.operator_id=?),"
        "       (SELECT COUNT(*) FROM live_runs r JOIN live_sessions s"
        "          ON s.id=r.session_id JOIN live_groups g ON g.id=s.group_id"
        "         WHERE g.operator_id=?)", (oid, oid, oid)).fetchone()
    return tuple(row)


def _owned(oid, alias="") -> tuple[str, tuple]:
    """WHERE fragment scoping a group query to one operator (None = everyone)."""
    col = f"{alias}.operator_id" if alias else "operator_id"
    return ("", ()) if oid is None else (f" AND {col}=?", (oid,))


def open_groups(con, oid=None) -> list[sqlite3.Row]:
    where, params = _owned(oid)
    return con.execute(
        "SELECT * FROM live_groups WHERE closed_at IS NULL" + where
        + " ORDER BY created_at DESC", params).fetchall()


def last_group(con, oid=None) -> sqlite3.Row | None:
    """The most recently worked group, open or closed."""
    where, params = _owned(oid, "g")
    return con.execute(
        "SELECT g.*, COALESCE(MAX(s.started_at), g.created_at) AS activity"
        " FROM live_groups g LEFT JOIN live_sessions s ON s.group_id = g.id"
        " WHERE 1=1" + where +
        " GROUP BY g.id ORDER BY activity DESC, g.id DESC LIMIT 1", params).fetchone()


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
        "DELETE FROM sessions WHERE id NOT IN (SELECT session_id FROM runs)"
        " AND deleted_at IS NULL").rowcount
    ng = con.execute(
        "DELETE FROM groups WHERE id NOT IN (SELECT group_id FROM sessions)"
        " AND deleted_at IS NULL").rowcount
    con.commit()
    return ns, ng


def all_groups(con, oid=None) -> list[sqlite3.Row]:
    where, params = _owned(oid)
    return con.execute("SELECT * FROM live_groups WHERE 1=1" + where
                       + " ORDER BY created_at", params).fetchall()


def get_group(con, gid) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM live_groups WHERE id=?", (gid,)).fetchone()


def get_group_any(con, gid) -> sqlite3.Row | None:
    """Including soft-deleted, for restore and trash listings."""
    return con.execute("SELECT * FROM groups WHERE id=?", (gid,)).fetchone()


def close_group(con, gid) -> None:
    con.execute("UPDATE groups SET closed_at=? WHERE id=?", (now_iso(), gid))
    con.commit()


def group_sessions(con, gid) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM live_sessions WHERE group_id=? ORDER BY seq", (gid,)
    ).fetchall()


def session_runs(con, sid) -> list[sqlite3.Row]:
    return con.execute("SELECT * FROM live_runs WHERE session_id=? ORDER BY seq",
                       (sid,)).fetchall()


def start_session(con, grp, started_at=None, notes=None, mode=None,
                  char_wpm=..., eff_wpm=...) -> sqlite3.Row:
    seq = (con.execute(
        "SELECT COALESCE(MAX(seq),0) FROM sessions WHERE group_id=?", (grp["id"],)
    ).fetchone()[0]) + 1
    mode = mode or grp["mode"]
    char_wpm = grp["char_wpm"] if char_wpm is ... else char_wpm
    eff_wpm = grp["eff_wpm"] if eff_wpm is ... else eff_wpm
    cur = con.execute(
        "INSERT INTO sessions (group_id, seq, mode, assignment, char_wpm, eff_wpm,"
        " started_at, notes) VALUES (?,?,?,?,?,?,?,?)",
        (grp["id"], seq, mode, grp["assignment"], char_wpm, eff_wpm,
         started_at or now_iso(), notes),
    )
    # the group carries the latest settings forward as next session's default
    con.execute("UPDATE groups SET mode=?, char_wpm=?, eff_wpm=? WHERE id=?",
                (mode, char_wpm, eff_wpm, grp["id"]))
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


SOLO_SHARE = 0.35  # groups that drill one character's rhythm on its own


def practice_set(counts, n=24, rng=None, min_len=2, max_len=3) -> list[str]:
    """Sending practice built from the characters you miss.

    It opens with a run of one character on its own - its rhythm with nothing
    to compare it to - worst first. The rest are drawn weighted by how often
    you missed them, so the worst come round most, and mixed together so you
    practise the transitions between them too.

    At most half the set is solo runs: a ten-character trouble list would
    otherwise spend the whole drill on single characters. Whatever gets crowded
    out that way is planted into the mixed groups instead, so everything you
    are working on still appears.
    """
    rng = rng or random.Random()
    chars = [c for c, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
    if not chars or n < 1:
        return []
    weights = [counts[c] for c in chars]
    solos = chars[:max(1, min(len(chars), (n + 1) // 2))]
    out = [c * rng.randint(min_len, max_len) for c in solos]
    pending = chars[len(solos):n]  # owed an appearance
    while len(out) < n:
        k = rng.randint(min_len, max_len)
        if not pending and rng.random() < SOLO_SHARE:
            out.append(rng.choices(chars, weights)[0] * k)
            continue
        g = rng.choices(chars, weights, k=k)
        if pending:
            g[rng.randrange(k)] = pending.pop(0)
        elif len(set(g)) == 1 and len(chars) > 1:
            g = rng.choices(chars, weights, k=k)  # that is a solo; draw again
        out.append("".join(g))
    return out


def pair_drill(pairs, per=3, rng=None, min_len=2, max_len=3) -> list[tuple]:
    """For each confused pair, groups that put the two rhythms side by side.

    Every group holds both characters - a group of one is just the solo drill
    the main set already covers, and the whole point here is the contrast.
    """
    rng = rng or random.Random()
    out = []
    for a, b, n in pairs:
        groups = []
        while len(groups) < per:
            k = rng.randint(min_len, max_len)
            g = [rng.choice((a, b)) for _ in range(k)]
            if len(set(g)) == 1:
                g[rng.randrange(k)] = b if g[0] == a else a
            groups.append("".join(g))
        out.append((a, b, n, groups))
    return out


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


def load_all(con, oid=None) -> list[GroupView]:
    return [load_group(con, g["id"]) for g in all_groups(con, oid)]


def practice_days(views: list[GroupView]) -> list[str]:
    """Days with at least one graded run, oldest first."""
    return sorted({r.recorded_at[:10] for v in views for r in v.graded_runs})


def char_counts(views: list[GroupView], days=None) -> tuple[Counter, Counter]:
    """(missed, sent) per character, optionally limited to a set of days."""
    miss: Counter = Counter()
    sent: Counter = Counter()
    for v in views:
        for s in v.sessions:
            for r in s.runs:
                if r.grade and (days is None or r.recorded_at[:10] in days):
                    miss.update(r.grade.miss_counts())
                    sent.update(r.grade.sent_counts())
    return miss, sent


def confusion_counts(views: list[GroupView], days=None) -> Counter:
    """(sent, heard) -> count, optionally limited to a set of days."""
    c: Counter = Counter()
    for v in views:
        for s in v.sessions:
            for r in s.runs:
                if r.grade and (days is None or r.recorded_at[:10] in days):
                    c.update(r.grade.confusions())
    return c


def confused_pairs(counts, min_count=2, top=6) -> list[tuple[str, str, int]]:
    """Unordered pairs you mix up, worst first.

    H heard as S and S heard as H are the same two rhythms failing to separate,
    so they are one drill and their counts add.
    """
    merged: Counter = Counter()
    for (a, b), n in counts.items():
        merged[tuple(sorted((a, b)))] += n
    ranked = sorted(merged.items(), key=lambda kv: (-kv[1], kv[0]))
    return [(a, b, n) for (a, b), n in ranked[:top] if n >= min_count]


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


def report_payload(views: list[GroupView], operators=()) -> dict:
    groups, sessions, runs = [], [], []
    for v in views:
        g = v.row
        groups.append({
            "id": g["id"], "op": _col(g, "operator_id"), "label": g["label"],
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
                "mode": s["mode"],
                "modeLabel": dict(MODES).get(s["mode"], s["mode"] or "-"),
                "charWpm": s["char_wpm"], "effWpm": s["eff_wpm"],
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
        "modes": dict(MODES),
        "operators": [{"id": o["id"], "callsign": o["callsign"], "name": o["name"]}
                      for o in operators],
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
.filters{display:flex;flex-wrap:wrap;gap:.4rem .8rem;align-items:center;flex:1 1 100%}
.filters label{display:flex;gap:.3rem;align-items:center;font-size:.76rem;
  color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.filters label[hidden]{display:none}
#clear{font:inherit;font-size:.8rem;padding:.28rem .65rem;border-radius:7px;
  border:1px solid var(--line);background:var(--panel);color:var(--muted);cursor:pointer}
#clear:hover{border-color:var(--accent);color:var(--ink)}
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
.pill.drill,.pill.op{background:var(--chip);color:var(--muted)}
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
  <div class="filters" id="filters">
    <label>From <select id="f-from"></select></label>
    <label>To <select id="f-to"></select></label>
    <label id="w-op" hidden>Operator <select id="f-op"></select></label>
    <label>Assignment <select id="f-gid"></select></label>
    <label>Session <select id="f-sid"></select></label>
    <label id="w-drill" hidden>Drill <select id="f-drill"></select></label>
    <button id="clear" type="button">Clear</button>
  </div>
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
const O = byId(DATA.operators || [], 'id');
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
/* Windows count days you actually practised, not calendar days: skip a
   Tuesday and "the last two days" still means your last two sessions' days,
   which is the question people actually ask of a practice log. */
const WINDOWS = [2, 3, 7, 14, 30].filter(n => n < days.length);
const fmtDayShort = d => d ? new Date(d + 'T12:00:00')
  .toLocaleDateString(undefined, {month:'short', day:'numeric'}) : '-';
const drills = [...new Set(DATA.sessions.map(s => s.mode).filter(Boolean))].sort();
const drillOf = r => S[r.sid].mode;
const opName = id => O[id] ? `${O[id].name} (${O[id].callsign})` : 'unassigned';
const opCall = id => O[id] ? O[id].callsign : 'unassigned';
const opOf = r => G[r.gid].op;
/* normally one - a report covers a single operator unless built --everyone */
const ops = [...new Set(DATA.groups.map(g => g.op))].map(String)
  .sort((a, b) => opName(a).localeCompare(opName(b)));
/* ---------- the filter ----------
   Every dimension is independent and they AND together, so "the last two days,
   letters only" is a combination rather than a scope somebody had to predefine.
   null means "don't care". */
const EMPTY = {from:null, to:null, op:null, gid:null, sid:null, drill:null};
const DIMS = Object.keys(EMPTY);
const isEmpty = f => DIMS.every(k => f[k] == null);

function filterRuns(f){
  return graded.filter(r =>
       (!f.from  || r.day >= f.from)
    && (!f.to    || r.day <= f.to)
    && (!f.op    || String(opOf(r)) === String(f.op))
    && (!f.gid   || r.gid === +f.gid)
    && (!f.sid   || r.sid === +f.sid)
    && (!f.drill || drillOf(r) === f.drill));
}

function filterLabel(f){
  const bits = [];
  if (f.from || f.to){
    const a = f.from || days[0], b = f.to || days[days.length - 1];
    bits.push(a === b ? fmtDay(a) : `${fmtDay(a)} – ${fmtDay(b)}`);
  }
  if (f.op) bits.push(opName(f.op));
  if (f.sid) bits.push(`${G[S[f.sid].gid].label} · session ${S[f.sid].seq}`);
  else if (f.gid) bits.push(G[f.gid].label);
  if (f.drill) bits.push(DATA.modes[f.drill] || f.drill);
  return bits.length ? bits.join(' · ') : 'All time';
}

/* The breakdown columns show the level below whatever is pinned: runs inside a
   session, sessions inside an assignment, otherwise days if the range spans
   more than one, and assignments if it does not. */
function children(f, runs){
  const by = (keyOf, name, short) => {
    const out = new Map();
    for (const r of runs){
      const k = keyOf(r);
      if (!out.has(k)) out.set(k, []);
      out.get(k).push(r);
    }
    return [...out.entries()]
      .sort((a, b) => String(a[0]).localeCompare(String(b[0]), undefined, {numeric:true}))
      .map(([k, rs]) => ({k, name: name(k), short: short(k), runs: rs}));
  };
  if (f.sid) return by(r => r.seq, k => 'Run ' + k, k => 'R' + k);
  if (f.gid) return by(r => r.sid, k => 'Session ' + S[k].seq, k => 'S' + S[k].seq);
  if (new Set(runs.map(r => r.day)).size > 1) return by(r => r.day, fmtDay, fmtDayShort);
  return by(r => r.gid, k => G[k].label, k => G[k].label.replace(/ · .*/, ''));
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

function sparkline(f, runs){
  if (runs.length < 2) return '';
  const pts = runs.map(r => stats([r]).pctRight);
  const w = 640, h = 96, pad = 16;
  const lo = Math.min(...pts, 99), span = Math.max(100 - lo, 1);
  const step = (w - 2 * pad) / (pts.length - 1);
  const xy = pts.map((v, i) => [pad + i * step, pad + (100 - v) / span * (h - 2 * pad - 10)]);
  const d = xy.map(([x, y], i) => `${i ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`).join(' ');
  const lbl = runs.map(r => f.sid ? 'R' + r.seq
    : `S${S[r.sid].seq}R${r.seq}`);  // how much context a tick needs
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

function panelPractice(f, runs, st){
  const tr = trouble(st);
  const chips = tr.length
    ? `<div class="trouble">${tr.map(([c, m]) =>
        `<span class="tr-chip">${esc(c)}<small>&times;${m.total}</small></span>`).join('')}</div>`
    : `<p class="none">nothing missed ${TH}+ times in this scope.</p>`;

  const kids = children(f, runs);
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

function panelProgress(f, runs){
  const spark = sparkline(f, runs);
  if (!spark) return '';
  const pts = runs.map(r => stats([r]).pctRight);
  const cap = `${runs.length} runs · ${pts[0].toFixed(1)}% → ${pts[pts.length - 1].toFixed(1)}%`
    + ` · best ${Math.max(...pts).toFixed(1)}%`;
  return `<div class="panel"><h3>Accuracy per run</h3>
    <div class="sparkcap" id="sparkcap" data-default="${esc(cap)}">${esc(cap)}</div>
    ${spark}</div>`;
}

function panelRuns(f, runs){
  const anyTrans = runs.some(r => r.cells.some(([, , k]) => k.includes('t')));
  const rows = runs.map((r, i) => {
    const st = stats([r]);
    const s = S[r.sid];
    const drill = f.drill || drills.length < 2 ? ''
      : ` <span class="pill drill">${esc(s.modeLabel)}</span>`;
    const who = f.op || ops.length < 2 ? ''
      : ` <span class="pill op">${esc(opCall(G[r.gid].op))}</span>`;
    const where = f.sid ? `Run ${r.seq}`
      : `S${s.seq} R${r.seq}`
        + (f.gid ? '' : ` <span class="pill pending">${esc(G[r.gid].label)}</span>`)
        + drill + who;
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

function panelContext(f, runs){
  // only one assignment (or one session of it) maps onto one set of settings
  const gid = f.sid ? S[f.sid].gid : f.gid;
  if (!gid) return '';
  const g = G[gid];
  const mine = DATA.sessions.filter(x => f.sid ? x.id === +f.sid : x.gid === +gid);
  const uniq = a => [...new Set(a)].join(', ') || '-';
  const bits = [
    ...(ops.length > 1 ? [`<span>Operator <b>${esc(opName(g.op))}</b></span>`] : []),
    `<span>Assignment <b>${esc(g.assignment)}</b></span>`,
    `<span>Drill <b>${esc(uniq(mine.map(x => x.modeLabel)))}</b></span>`,
    `<span>Speed <b>${esc(uniq(mine.map(x => x.charWpm == null ? 'not recorded'
      : `${+x.charWpm}/${+x.effWpm} wpm`)))}</b></span>`];
  const note = f.sid ? S[f.sid].notes : g.notes;
  return `<div class="panel"><h3>About this ${f.sid ? 'session' : 'assignment'}</h3>
    <div class="scopeline" style="flex:1">${bits.join(' &middot; ')}</div>
    ${note ? `<div class="note">${esc(note)}</div>` : ''}</div>`;
}
"""

APP_JS3 = r"""
/* ---------- controller ---------- */
let F = {...EMPTY};

const ctl = k => document.getElementById('f-' + k);

function buildControls(){
  const opt = (v, label, n, cur) =>
    `<option value="${esc(v)}"${String(cur ?? '') === String(v) ? ' selected' : ''}>`
    + `${esc(label)}${n == null ? '' : ` (${n})`}</option>`;
  /* Counts are computed with that one dimension replaced, so a choice that
     would empty the page says so before you pick it. */
  const n = over => filterRuns({...F, ...over}).length;

  let h = opt('', 'Earliest', null, F.from);
  for (const d of days) h += opt(d, fmtDay(d), null, F.from);
  ctl('from').innerHTML = h;

  h = opt('', 'Latest', null, F.to);
  for (const d of [...days].reverse()) h += opt(d, fmtDay(d), null, F.to);
  ctl('to').innerHTML = h;

  h = opt('', 'Everyone', n({op:null}), F.op);
  for (const o of ops) h += opt(o, opName(o), n({op:o}), F.op);
  ctl('op').innerHTML = h;
  document.getElementById('w-op').hidden = ops.length < 2;

  h = opt('', 'All assignments', n({gid:null, sid:null}), F.gid);
  for (const g of [...DATA.groups].reverse())
    h += opt(g.id, g.label, n({gid:g.id, sid:null}), F.gid);
  ctl('gid').innerHTML = h;

  // sessions cascade off the assignment, and drop out when nothing is left
  h = opt('', 'All sessions', n({sid:null}), F.sid);
  for (const s of [...DATA.sessions].reverse()){
    if (F.gid && s.gid !== +F.gid) continue;
    const c = n({sid:s.id});
    if (c || String(F.sid) === String(s.id))
      h += opt(s.id, `${F.gid ? '' : G[s.gid].label + ' · '}session ${s.seq}`, c, F.sid);
  }
  ctl('sid').innerHTML = h;

  h = opt('', 'All drills', n({drill:null}), F.drill);
  for (const d of drills) h += opt(d, DATA.modes[d] || d, n({drill:d}), F.drill);
  ctl('drill').innerHTML = h;
  document.getElementById('w-drill').hidden = drills.length < 2;
}

function wireControls(){
  for (const k of DIMS) ctl(k).onchange = () => {
    const v = ctl(k).value || null;
    const next = {...F, [k]: v};
    // keep the combination coherent rather than silently empty
    if (k === 'gid' && next.sid && S[next.sid] && String(S[next.sid].gid) !== String(v))
      next.sid = null;
    if (k === 'sid' && v) next.gid = String(S[v].gid);
    if (k === 'from' && v && next.to && v > next.to) next.to = null;
    if (k === 'to' && v && next.from && v < next.from) next.from = null;
    setFilter(next);
  };
  document.getElementById('clear').onclick = () => setFilter({...EMPTY});
}

function buildQuick(){
  const last = graded[graded.length - 1];
  const items = [['All time', {...EMPTY}]];
  if (days.length > 1){
    const d = days[days.length - 1];
    items.push([fmtDay(d), {...EMPTY, from:d, to:d}]);
  }
  for (const w of WINDOWS.filter(w => w === 2 || w === 7))
    items.push([`Last ${w} days`, {...EMPTY, from: days[days.length - w]}]);
  if (last){
    items.push(['Latest assignment', {...EMPTY, gid: String(last.gid)}]);
    items.push(['Latest session',
                {...EMPTY, gid: String(S[last.sid].gid), sid: String(last.sid)}]);
  }
  document.getElementById('quick').innerHTML = items.map(([l, s], i) =>
    `<button data-i="${i}">${esc(l)}</button>`).join('');
  document.querySelectorAll('#quick button').forEach(b => {
    b.onclick = () => setFilter(items[+b.dataset.i][1]);
  });
  return items;
}

/* ---------- the URL carries the whole filter ---------- */
function hashOf(f){
  const parts = DIMS.filter(k => f[k] != null && f[k] !== '')
    .map(k => `${k}=${encodeURIComponent(f[k])}`);
  return parts.length ? '#' + parts.join('&') : '#all';
}

function validFilter(f){
  return (!f.from || days.includes(f.from))
    && (!f.to || days.includes(f.to))
    && (!f.op || ops.includes(String(f.op)))
    && (!f.gid || !!G[f.gid])
    && (!f.sid || !!S[f.sid])
    && (!f.drill || drills.includes(f.drill));
}

function filterFromHash(){
  const raw = (location.hash || '').slice(1);
  if (!raw) return null;
  if (raw === 'all') return {...EMPTY};
  if (raw.includes('=')){
    const f = {...EMPTY};
    let any = false;
    for (const part of raw.split('&')){
      const [k, v] = part.split('=');
      if (!DIMS.includes(k) || !v) continue;
      f[k] = decodeURIComponent(v);
      any = true;
    }
    return any && validFilter(f) ? f : null;
  }
  // links written before the filter bar existed: #group:8, #day:2026-09-10
  const [t, k] = raw.split(':');
  if (!t || !k) return null;
  const legacy = {
    day: () => ({from:k, to:k}),
    group: () => ({gid:k}),
    session: () => S[k] ? {gid:String(S[k].gid), sid:k} : null,
    drill: () => ({drill:k}),
    operator: () => ({op:k}),
    last: () => days.length > +k ? {from: days[days.length - +k]} : null,
  };
  const made = legacy[t] && legacy[t]();
  const f = made && {...EMPTY, ...made};
  return f && validFilter(f) ? f : null;
}

function setFilter(next){
  F = {...EMPTY, ...next};
  buildControls();
  document.querySelectorAll('#quick button').forEach((b, i) => {
    b.setAttribute('aria-pressed', String(hashOf(QUICK[i][1]) === hashOf(F)));
  });
  try { history.replaceState(null, '', hashOf(F)); } catch (e) {}
  render();
}

function render(){
  const runs = filterRuns(F);
  const st = stats(runs);
  const sess = new Set(runs.map(r => r.sid)).size;
  const grps = new Set(runs.map(r => r.gid)).size;
  const dys = [...new Set(runs.map(r => r.day))].sort();
  const span = dys.length > 1 ? `${fmtDay(dys[0])} – ${fmtDay(dys[dys.length - 1])}`
    : fmtDay(dys[0]);
  document.getElementById('scopeline').innerHTML =
    `<b>${esc(filterLabel(F))}</b> — ${grps} group${grps === 1 ? '' : 's'},
     ${sess} session${sess === 1 ? '' : 's'}, ${runs.length} run${runs.length === 1 ? '' : 's'}
     · ${esc(span)}`;

  const app = document.getElementById('app');
  if (!runs.length){
    app.innerHTML = '<p class="none">No graded runs match these filters.</p>';
    return;
  }
  app.innerHTML = panelHeadline(runs, st) + panelPractice(F, runs, st)
    + panelProgress(F, runs) + panelRuns(F, runs) + panelChars(st) + panelContext(F, runs);

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
  (ops.length === 1 && O[ops[0]] ? opName(ops[0]) + ' · ' : '')
  + `${DATA.groups.length} group(s), ${DATA.sessions.length} session(s), `
  + `${graded.length} graded run(s) · generated `
  + new Date(DATA.generated).toLocaleString();

wireControls();
const QUICK = buildQuick();
setFilter(filterFromHash() || {...EMPTY});

// following a link into a tab that already has the report open would otherwise
// change the URL and nothing else
if (typeof window !== 'undefined' && window.addEventListener)
  window.addEventListener('hashchange', () => {
    const next = filterFromHash();
    if (next && hashOf(next) !== hashOf(F)) setFilter(next);
  });
"""


def build_report(views: list[GroupView], title: str = "LCWO progress",
                 operators=()) -> str:
    payload = json.dumps(report_payload(views, operators), separators=(",", ":"))
    payload = payload.replace("<", "\\u003c")  # never break out of the script tag
    return (HTML_SHELL
            .replace("__TITLE__", escape(title))
            .replace("__CSS__", CSS)
            .replace("__APP__", APP_JS + APP_JS2 + APP_JS3)
            .replace("__DATA__", payload))


def write_report(con, gid: int | None = None, out: Path | None = None,
                 oid: int | None = None) -> Path:
    if gid:
        views = [load_group(con, gid)]
        oid = _col(views[0].row, "operator_id")  # a group implies its operator
    else:
        views = load_all(con, oid)
    if not views:
        raise SystemExit("no data yet - record a session first")
    op, ops = get_operator(con, oid), list_operators(con)
    title = f"LCWO group {gid}" if gid else "LCWO progress"
    if op is not None:
        title += f" \u2014 {op['callsign']}"
    html = build_report(views, title, ops)
    # once more than one operator is on file each gets their own report tree,
    # so switching operators cannot overwrite someone else's page
    base = REPORT_DIR / op["callsign"].lower() if op and len(ops) > 1 else REPORT_DIR
    out = out or base / (f"group-{gid}.html" if gid else "index.html")
    out.parent.mkdir(parents=True, exist_ok=True)
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
    """Enter always means yes. Nothing here is destructive enough to be worth
    a moving default - a prompt whose answer changes underneath you trains you
    to stop reading it."""
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
    rule(f"{grp['label']} · Session {sess['seq']} · "
         f"{dict(MODES).get(sess['mode'], sess['mode'])} @ "
         f"{fmt_wpm(sess['char_wpm'])}/{fmt_wpm(sess['eff_wpm'])} wpm")
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


def group_drills(con, gid) -> str:
    rows = con.execute("SELECT DISTINCT mode FROM live_sessions WHERE group_id=?"
                       " ORDER BY mode", (gid,)).fetchall()
    names = [dict(MODES).get(r[0], r[0]) for r in rows if r[0]]
    return ", ".join(names) if names else "-"


def group_speeds(con, gid) -> str:
    rows = con.execute(
        "SELECT DISTINCT char_wpm, eff_wpm FROM live_sessions WHERE group_id=?"
        " AND char_wpm IS NOT NULL ORDER BY char_wpm", (gid,)).fetchall()
    if not rows:
        return "speed not recorded"
    return ", ".join(f"{fmt_wpm(r[0])}/{fmt_wpm(r[1])}" for r in rows) + " wpm"


def group_line(con, g) -> str:
    ns = con.execute("SELECT COUNT(*) FROM live_sessions WHERE group_id=?",
                     (g["id"],)).fetchone()[0]
    nr = con.execute("SELECT COUNT(*) FROM live_runs r JOIN live_sessions s"
                     " ON s.id=r.session_id WHERE s.group_id=?", (g["id"],)).fetchone()[0]
    state = "closed" if g["closed_at"] else "open"
    return (f"{ns} session(s), {nr} run(s) · {group_drills(con, g['id'])}"
            f" · {group_speeds(con, g['id'])} · {state}")


def add_operator(con, adopt=False):
    """Ask for a name and a call sign, and make that operator current."""
    rule("Who is copying?")
    print(dim("  recorded with every group, so one database can hold more"
              " than one operator"))
    name = ask_text("Name")
    while True:
        call = normalise_call(ask_text("Call sign"))
        if not call:
            print(dim("  (required)"))
            continue
        if find_operator(con, call) is not None:
            print(red(f"  {call} is already in the database"))
            continue
        break
    oid = create_operator(con, name, call)
    set_current_operator(con, oid)
    if adopt:
        n = adopt_unassigned(con, oid)
        if n:
            print(dim(f"  {n} existing group(s) now belong to {call}"))
    print(green(f"\n  ✓ recording as {name} ({call})"))
    return get_operator(con, oid)


def choose_operator(con, token=None):
    """Who the next group/session is for. Only asks when there is a choice."""
    if token:
        op = find_operator(con, token)
        if op is None:
            raise SystemExit(f"no operator matching {token!r}"
                             " - add one with `python3 lcwo.py user --add`")
        set_current_operator(con, op["id"])
        return op

    ops = list_operators(con)
    if not ops:
        # first run, or a database from before operators existed
        return add_operator(con, adopt=True)

    cur = current_operator(con)
    if len(ops) == 1:
        print(dim(f"  recording as {op_label(ops[0])}"
                  "   (change with `lcwo user --add` / `--use`)"))
        return ops[0]

    rule("Operator")
    opts = [(str(o["id"]), op_label(o)) for o in ops]
    opts.append(("new", "Add a new operator"))
    default = next((i for i, o in enumerate(ops, 1)
                    if cur and o["id"] == cur["id"]), 1)
    pick = ask_choice("Who is copying?", opts, default=default)
    if pick == "new":
        return add_operator(con)
    op = get_operator(con, int(pick))
    set_current_operator(con, op["id"])
    return op


def choose_group(con, op=None):
    oid = op["id"] if op else None
    rows = all_groups(con, oid)
    rule("All groups")
    for g in rows:
        print(f"  {g['id']:>3}) {g['label']:<28} {dim(group_line(con, g))}")
    while True:
        v = ask_text("which group (blank to start a new one)", "", allow_empty=True)
        if not v:
            return None
        if v.isdigit() and any(int(v) == g["id"] for g in rows):
            return get_group(con, int(v))
        if v.isdigit() and op is not None:
            print(dim(f"  (group {v} is not {op['callsign']}'s)"))
        print(dim("  (pick an id from the list, or press Enter for a new group)"))


def pick_group(con, op=None):
    oid = op["id"] if op else None
    last = last_group(con, oid)
    if last is None:
        return new_group(con, op)

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
        picked = choose_group(con, op)
        if picked is not None:
            if picked["closed_at"]:
                reopen_group(con, picked["id"])
                print(dim(f"  reopened group {picked['id']}"))
            return get_group(con, picked["id"])
    return new_group(con, op)


def new_group(con, op=None):
    """A group is one homework assignment. Drill and speed are asked per
    session, because a single assignment alternates copy and send and can
    change drill partway through."""
    while True:
        rule("New assignment")
        assignment = ask_text("Assignment (e.g. S2HW3)")
        if ask_yes_no(f"   start assignment {bold(assignment)}?"):
            gid = create_group(con, assignment=assignment, label=assignment,
                               operator_id=op["id"] if op else None)
            print(green(f"\n  ✓ group {gid} created for {assignment}"))
            return get_group(con, gid)
        print(dim("  starting over\n"))


def session_settings(con, grp):
    """Drill and speed for the next session, carrying the last one forward."""
    prev = con.execute(
        "SELECT mode, char_wpm, eff_wpm FROM live_sessions WHERE group_id=?"
        " ORDER BY seq DESC LIMIT 1", (grp["id"],)).fetchone()
    if prev and prev["mode"]:
        cur = (f"{dict(MODES).get(prev['mode'], prev['mode'])}"
               f" at {fmt_wpm(prev['char_wpm'])}/{fmt_wpm(prev['eff_wpm'])} wpm")
        if not ask_yes_no(f"  Same as last session ({cur})?"):
            return ask_settings(prev["char_wpm"], prev["eff_wpm"])
        return prev["mode"], prev["char_wpm"], prev["eff_wpm"]
    return ask_settings()


def ask_settings(char_default=25, eff_default=None):
    mode = ask_choice("Which drill?", MODES)
    char_wpm = ask_number("Character speed (wpm)", fmt_wpm(char_default or 25))
    eff_wpm = ask_number("Effective speed (wpm)",
                         fmt_wpm(eff_default if eff_default is not None else char_wpm))
    return mode, char_wpm, eff_wpm


def cmd_record(args) -> int:
    con = connect()
    ns, ng = prune_empty(con)  # clear anything a previous interrupt left behind
    if ns or ng:
        print(dim(f"  cleared {ns} empty session(s) and {ng} empty group(s) "
                  "left by an earlier run"))
    grp = None
    op = None
    try:
        op = choose_operator(con, getattr(args, "user", None))
        grp = pick_group(con, op)
        while True:
            mode, cw, ew = session_settings(con, grp)
            sess = start_session(con, grp, mode=mode, char_wpm=cw, eff_wpm=ew)
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
            if not ask_yes_no("\nAnother session in this group?"):
                break
        if ask_yes_no("Close this group?"):
            close_group(con, grp["id"])
    except (Abort, KeyboardInterrupt):
        print(dim("\n  bye — everything recorded so far is saved"))
    finally:
        prune_empty(con)
        try:
            still_there = grp is not None and get_group(con, grp["id"]) is not None
            oid = op["id"] if op else None
            recorded = con.execute(
                "SELECT COUNT(*) FROM live_runs r"
                " JOIN live_sessions s ON s.id = r.session_id"
                " JOIN live_groups g ON g.id = s.group_id"
                " WHERE (? IS NULL OR g.operator_id = ?)", (oid, oid)).fetchone()[0]
            if recorded:
                out = write_report(con, oid=oid)
                print(green(f"\n  ✓ report: {out}"))
                if still_there:
                    print(green(f"  ✓ group:  {write_report(con, grp['id'])}"))
                if _TTY and ask_yes_no("Open the report?"):
                    # land on the group just finished; the filter can still
                    # widen to all time from there
                    url = out.resolve().as_uri()
                    if still_there:
                        url += f"#gid={grp['id']}"
                    webbrowser.open(url)
        except (Abort, KeyboardInterrupt):
            pass
        con.close()
    return 0


# --------------------------------------------------------------------------
# other commands
# --------------------------------------------------------------------------


def scope_operator(con, args, persist=False):
    """Which operator a command applies to: --user, else whoever is current."""
    tok = getattr(args, "user", None)
    if tok:
        op = find_operator(con, tok)
        if op is None:
            raise SystemExit(f"no operator matching {tok!r}"
                             " - see `python3 lcwo.py user`")
        if persist:
            set_current_operator(con, op["id"])
        return op
    if getattr(args, "everyone", False):
        return None
    return current_operator(con)


def cmd_groups(args) -> int:
    con = connect()
    op = scope_operator(con, args)
    views = load_all(con, op["id"] if op else None)
    if not views:
        print(dim("no groups yet — run `python3 lcwo.py` to start one"))
        return 0
    rule(f"Groups — {op_label(op)}" if op else "Groups — everyone")
    print(f"  {'id':>3}  {'assignment':<12} {'drills':<26} {'sess':>4} {'runs':>4} "
          f"{'wrong':>6}  trouble")
    for v in views:
        runs = v.graded_runs
        tot = sum(r.grade.total_chars for r in runs)
        wr = sum(r.grade.wrong_chars for r in runs)
        pct = f"{100.0 * wr / tot:.1f}%" if tot else "-"
        tr = ",".join(ch for ch, _ in v.trouble()) or "-"
        state = "" if not v.row["closed_at"] else dim(" (closed)")
        print(f"  {v.row['id']:>3}  {v.row['label'][:12]:<12} "
              f"{group_drills(con, v.row['id'])[:26]:<26} {len(v.sessions):>4} "
              f"{len(runs):>4} {pct:>6}  {tr}{state}")
    con.close()
    return 0


def counts_in_scope(con, args):
    """(operator, views, window, missed, sent) for the character commands."""
    op = scope_operator(con, args)
    views = ([load_group(con, args.group)] if args.group
             else load_all(con, op["id"] if op else None))
    window = practice_days(views)[-args.days:] if args.days else None
    counts, sent = char_counts(views, window)
    return op, views, window, counts, sent


def scope_label(args, op, window) -> str:
    if window:
        label = f"last {len(window)} practice day(s), {window[0]} to {window[-1]}"
    elif args.group:
        label = f"group {args.group}"
    else:
        label = "all time"
    if op and not args.group:
        label += f" · {op['callsign']}"
    return label


def cmd_trouble(args) -> int:
    con = connect()
    op, _views, window, counts, sent = counts_in_scope(con, args)
    tr = trouble_from(counts, args.threshold)
    scope = scope_label(args, op, window)
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


def cmd_practice(args) -> int:
    """Turn the trouble list into something to send."""
    con = connect()
    pairs = []
    if args.chars:
        # uppercased, spaces dropped, first occurrence wins - all equally weighted
        picked = list(dict.fromkeys(normalise_call(args.chars)))
        counts = Counter(dict.fromkeys(picked, 1))
        scope = "your list"
        sent = Counter()
    else:
        op, views, window, counts, sent = counts_in_scope(con, args)
        counts = Counter(dict(trouble_from(counts, args.threshold)))
        scope = scope_label(args, op, window)
        if args.pairs:
            pairs = confused_pairs(confusion_counts(views, window),
                                   args.threshold, args.pair_count)
    con.close()

    rng = random.Random(args.seed) if args.seed is not None else random.Random()
    groups = practice_set(counts, args.count, rng)
    drills = pair_drill(pairs, rng=rng)
    if args.plain:
        print(" ".join(groups + [g for _a, _b, _n, gs in drills for g in gs]))
        return 0
    rule(f"Sending practice — {scope}")
    if not groups:
        print(dim(f"  nothing missed {args.threshold}+ times in that scope —"
                  " widen it with -d/-n, or pass --chars ABCD"))
        return 0
    worst = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    if sent:
        print("  " + dim("from: ") + "  ".join(
            f"{bold(c)}{dim('×' + str(n))}" for c, n in worst))
    else:
        print("  " + dim("from: ") + " ".join(bold(c) for c, _ in worst))
    print()
    for i in range(0, len(groups), 6):
        print("   " + "  ".join(f"{g:<4}" for g in groups[i:i + 6]).rstrip())
    if drills:
        rule("Confusions — the pairs you mix up")
        for a, b, n, gs in drills:
            print(f"   {bold(a)} {dim('↔')} {bold(b)} {dim('×' + str(n)):<10}"
                  + "  ".join(f"{g:<4}" for g in gs).rstrip())
    elif args.pairs and not args.chars:
        print(dim(f"\n  no substitution seen {args.threshold}+ times in that scope"))
    total = len(groups) + sum(len(gs) for _a, _b, _n, gs in drills)
    print(dim(f"\n  {total} groups · send them, don't read them ·"
              " --plain to pipe elsewhere"))
    return 0


def list_users(con) -> None:
    ops = list_operators(con)
    if not ops:
        print(dim('no operators yet — python3 lcwo.py user --add'))
        return
    cur = current_operator(con)
    rule("Operators")
    print(f"      {'id':>3}  {'call':<10} {'name':<22} {'grp':>4} {'sess':>5} {'runs':>5}")
    for o in ops:
        ng, nsess, nruns = operator_counts(con, o["id"])
        mark = green(" >") if cur and o["id"] == cur["id"] else "  "
        print(f"  {mark}  {o['id']:>3}  {o['callsign']:<10} {o['name'][:22]:<22}"
              f" {ng:>4} {nsess:>5} {nruns:>5}")
    loose = con.execute(
        "SELECT COUNT(*) FROM live_groups WHERE operator_id IS NULL").fetchone()[0]
    if loose:
        print(yellow(f"\n  {loose} group(s) belong to nobody"
                     " — `python3 lcwo.py user --add` adopts them"))
    print(dim("\n  switch with:  python3 lcwo.py user --use CALL"))


def cmd_user(args) -> int:
    """Show who is on file, add an operator, switch, rename, or remove one."""
    con = connect()
    if args.add:
        name = args.name or (ask_text("Name") if _TTY else "")
        call = args.call or (ask_text("Call sign") if _TTY else "")
        if not (name or "").strip() or not normalise_call(call):
            raise SystemExit("an operator needs both --name and --call")
        if find_operator(con, call) is not None:
            raise SystemExit(f"{normalise_call(call)} is already in the database")
        first = not list_operators(con)
        oid = create_operator(con, name, call)
        set_current_operator(con, oid)
        if first:
            n = adopt_unassigned(con, oid)
            if n:
                print(dim(f"  {n} existing group(s) now belong to {normalise_call(call)}"))
        print(green(f"  ✓ added {op_label(get_operator(con, oid))}, now recording as them"))
    elif args.use:
        op = find_operator(con, args.use)
        if op is None:
            raise SystemExit(f"no operator matching {args.use!r}")
        set_current_operator(con, op["id"])
        print(green(f"  ✓ recording as {op_label(op)}"))
    elif args.remove:
        op = find_operator(con, args.remove)
        if op is None:
            raise SystemExit(f"no operator matching {args.remove!r}")
        held = con.execute("SELECT COUNT(*) FROM groups WHERE operator_id=?",
                           (op["id"],)).fetchone()[0]
        if held:
            raise SystemExit(f"{op['callsign']} still owns {held} group(s)"
                             " — bin and purge those first")
        con.execute("DELETE FROM operators WHERE id=?", (op["id"],))
        cur = get_setting(con, "operator_id")
        if cur is not None and int(cur) == op["id"]:
            set_current_operator(con, None)
        con.commit()
        print(green(f"  ✓ removed {op_label(op)}"))
    elif args.name or args.call:
        op = current_operator(con)
        if op is None:
            raise SystemExit("nobody is current — add one with --add")
        name = (args.name or op["name"]).strip()
        call = normalise_call(args.call) or op["callsign"]
        clash = find_operator(con, call)
        if clash is not None and clash["id"] != op["id"]:
            raise SystemExit(f"{call} is already in the database")
        con.execute("UPDATE operators SET name=?, callsign=? WHERE id=?",
                    (name, call, op["id"]))
        con.commit()
        print(green(f"  ✓ {op_label(op)} is now {name} ({call})"))
    list_users(con)
    con.close()
    return 0


def cmd_report(args) -> int:
    con = connect()
    op = scope_operator(con, args)
    out = write_report(con, args.group, Path(args.out) if args.out else None,
                       oid=op["id"] if op else None)
    print(green(f"  ✓ {out}"))
    if args.open:
        webbrowser.open(out.resolve().as_uri())
    con.close()
    return 0


def cmd_key(args) -> int:
    """Attach a results table to a session that was left ungraded."""
    con = connect()
    op = scope_operator(con, args)
    oid = op["id"] if op else None
    pending = con.execute(
        "SELECT s.*, g.label FROM live_sessions s JOIN live_groups g ON g.id = s.group_id"
        " WHERE s.key_json IS NULL AND (? IS NULL OR g.operator_id = ?)"
        " ORDER BY s.started_at", (oid, oid)
    ).fetchall()
    if not pending:
        print(dim("no ungraded sessions"))
        return 0
    if args.session:
        sess = con.execute("SELECT * FROM live_sessions WHERE id=?",
                           (args.session,)).fetchone()
        if sess is None:
            raise SystemExit(f"no such session: {args.session}")
    else:
        rule("Ungraded sessions")
        for s in pending:
            n = len(session_runs(con, s["id"]))
            print(f"  {s['id']}) {s['label']} · session {s['seq']} · {n} run(s) · "
                  f"{fmt_ts(s['started_at'])}")
        sid = ask_text("which session", str(pending[0]["id"]))
        sess = con.execute("SELECT * FROM live_sessions WHERE id=?", (int(sid),)).fetchone()
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
        if args.group is not None:  # an explicit id outranks the operator filter
            sql, params = "SELECT id, label, char_wpm, eff_wpm FROM live_groups WHERE id=?", (args.group,)
        else:
            op = scope_operator(con, args)
            oid = op["id"] if op else None
            sql = ("SELECT id, label, char_wpm, eff_wpm FROM live_groups"
                   " WHERE (? IS NULL OR operator_id = ?)")
            params = (oid, oid)
        rows = con.execute(sql + " ORDER BY created_at", params).fetchall()
        if not rows:
            raise SystemExit(f"no such group: {args.group}" if args.group is not None
                             else "no groups yet")
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


def merge_plan(con) -> list[dict]:
    """Groups that share an assignment, oldest first, and what they'd become."""
    rows = con.execute("SELECT * FROM live_groups ORDER BY created_at, id").fetchall()
    buckets: dict[str, list] = {}
    for g in rows:
        # keyed by operator too: two people can both have an S1HW1
        buckets.setdefault(
            (_col(g, "operator_id"), (g["assignment"] or "").strip().upper()), []
        ).append(g)
    plan = []
    for (_oid, key), gs in buckets.items():
        if not key:
            continue
        target, rest = gs[0], gs[1:]
        if not rest and target["label"] == key:
            continue  # already the shape we want
        plan.append({"assignment": key, "target": target, "absorb": rest})
    return plan


def merge_groups(con, entry) -> None:
    """Fold `absorb` into `target`, re-sequencing sessions chronologically."""
    target, absorb = entry["target"], entry["absorb"]
    tid = target["id"]
    ids = [tid] + [g["id"] for g in absorb]
    marks = ",".join("?" * len(ids))
    sessions = con.execute(
        f"SELECT id FROM sessions WHERE group_id IN ({marks})"
        " ORDER BY started_at, group_id, seq", ids).fetchall()

    # UNIQUE(group_id, seq) means the incoming rows have to be parked out of the
    # way before they can be renumbered into one continuous sequence.
    for s in sessions:
        con.execute("UPDATE sessions SET seq=? WHERE id=?", (-s["id"], s["id"]))
    for s in sessions:
        con.execute("UPDATE sessions SET group_id=? WHERE id=?", (tid, s["id"]))
    for i, s in enumerate(sessions, 1):
        con.execute("UPDATE sessions SET seq=? WHERE id=?", (i, s["id"]))

    notes = [n for n in (target["notes"] or "").splitlines() if n.strip()]
    for g in absorb:
        for n in (g["notes"] or "").splitlines():
            if n.strip() and n not in notes:
                notes.append(n)
        if g["source"]:
            line = f"Merged in {g['source']}."
            if line not in notes:
                notes.append(line)
    closed = [g["closed_at"] for g in [target] + absorb]
    latest = con.execute(
        "SELECT mode, char_wpm, eff_wpm FROM live_sessions WHERE group_id=?"
        " ORDER BY seq DESC LIMIT 1", (tid,)).fetchone()
    con.execute(
        "UPDATE groups SET label=?, notes=?, closed_at=?, mode=?, char_wpm=?, eff_wpm=?"
        " WHERE id=?",
        (entry["assignment"], "\n".join(notes) or None,
         max(closed) if all(closed) else None,
         latest["mode"] if latest else None,
         latest["char_wpm"] if latest else None,
         latest["eff_wpm"] if latest else None, tid))
    for g in absorb:
        con.execute("DELETE FROM groups WHERE id=?", (g["id"],))
    con.commit()


def cmd_merge(args) -> int:
    con = connect()
    plan = merge_plan(con)
    if not plan:
        print(dim("  nothing to merge - every assignment is already one group"))
        return 0
    rule("Merge groups that share an assignment")
    for e in plan:
        tgt, absorb = e["target"], e["absorb"]
        ns = con.execute("SELECT COUNT(*) FROM live_sessions WHERE group_id=?",
                         (tgt["id"],)).fetchone()[0]
        moved = sum(con.execute("SELECT COUNT(*) FROM live_sessions WHERE group_id=?",
                                (g["id"],)).fetchone()[0] for g in absorb)
        print(f"\n  {bold(e['assignment'])}  → group {tgt['id']}"
              f"  ({ns} session(s)" + (f" + {moved} moved in)" if moved else ")"))
        print(f"      {dim('label: ' + repr(tgt['label']) + ' → ' + repr(e['assignment']))}")
        for g in absorb:
            n = con.execute("SELECT COUNT(*) FROM live_sessions WHERE group_id=?",
                            (g["id"],)).fetchone()[0]
            print(f"      {dim(f'absorb group {g[chr(34)+chr(34)] if False else g[chr(105)+chr(100)]}')}"
                  f" {dim(repr(g['label']) + f' ({n} session(s))')}")
    if not args.apply:
        print(dim("\n  dry run - re-run with --apply to write\n"))
        return 0
    before = con.execute(
        "SELECT COUNT(*) FROM runs").fetchone()[0]
    for e in plan:
        merge_groups(con, e)
    after = con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    if before != after:
        raise SystemExit(f"run count changed during merge: {before} -> {after}")
    bad = con.execute("PRAGMA foreign_key_check").fetchall()
    if bad:
        raise SystemExit(f"merge left dangling references: {bad[:3]}")
    print(green(f"\n  ✓ merged; {after} runs intact, "
                f"{con.execute('SELECT COUNT(*) FROM groups').fetchone()[0]} groups remain\n"))
    con.close()
    return 0


# --------------------------------------------------------------------------
# the bin: soft delete, restore, purge
# --------------------------------------------------------------------------

TABLES = {"group": "groups", "session": "sessions", "run": "runs"}


def _target(args):
    picked = [(k, getattr(args, k)) for k in TABLES if getattr(args, k, None) is not None]
    if len(picked) != 1:
        raise SystemExit("pass exactly one of --group, --session or --run")
    return picked[0]


def scope_counts(con, kind, rid) -> str:
    """What else disappears with this row, by containment."""
    if kind == "group":
        ns = con.execute("SELECT COUNT(*) FROM sessions WHERE group_id=?", (rid,)).fetchone()[0]
        nr = con.execute("SELECT COUNT(*) FROM runs r JOIN sessions s ON s.id=r.session_id"
                         " WHERE s.group_id=?", (rid,)).fetchone()[0]
        return f"{ns} session(s) and {nr} run(s)"
    if kind == "session":
        nr = con.execute("SELECT COUNT(*) FROM runs WHERE session_id=?", (rid,)).fetchone()[0]
        return f"{nr} run(s)"
    return "1 run"


def describe_row(con, kind, rid) -> str | None:
    if kind == "group":
        r = con.execute("SELECT id, label, deleted_at FROM groups WHERE id=?", (rid,)).fetchone()
        return f"group {r['id']} ({r['label']})" if r else None
    if kind == "session":
        r = con.execute("SELECT s.id, s.seq, s.mode, g.label, s.deleted_at FROM sessions s"
                        " JOIN groups g ON g.id=s.group_id WHERE s.id=?", (rid,)).fetchone()
        return f"session {r['id']} ({r['label']} · session {r['seq']})" if r else None
    r = con.execute("SELECT r.id, r.seq, s.seq ss, g.label, r.deleted_at FROM runs r"
                    " JOIN sessions s ON s.id=r.session_id JOIN groups g ON g.id=s.group_id"
                    " WHERE r.id=?", (rid,)).fetchone()
    return f"run {r['id']} ({r['label']} · session {r['ss']} run {r['seq']})" if r else None


def cmd_delete(args) -> int:
    con = connect()
    kind, rid = _target(args)
    what = describe_row(con, kind, rid)
    if what is None:
        raise SystemExit(f"no such {kind}: {rid}")
    already = con.execute(
        f"SELECT deleted_at FROM {TABLES[kind]} WHERE id=?", (rid,)).fetchone()[0]
    if already:
        print(dim(f"  {what} is already in the bin (since {fmt_ts(already)})"))
        return 0
    print(f"  {bold(what)}")
    print(dim(f"      hides {scope_counts(con, kind, rid)}"))
    if not args.yes and not ask_yes_no("  move to the bin?"):
        return 0
    con.execute(f"UPDATE {TABLES[kind]} SET deleted_at=? WHERE id=?", (now_iso(), rid))
    con.commit()
    print(green(f"  ✓ {what} moved to the bin — restore with "
                f"`lcwo.py restore --{kind} {rid}`"))
    con.close()
    return 0


def cmd_restore(args) -> int:
    con = connect()
    kind, rid = _target(args)
    what = describe_row(con, kind, rid)
    if what is None:
        raise SystemExit(f"no such {kind}: {rid}")
    n = con.execute(f"UPDATE {TABLES[kind]} SET deleted_at=NULL WHERE id=?", (rid,)).rowcount
    con.commit()
    print(green(f"  ✓ restored {what}") if n else dim(f"  {what} was not in the bin"))
    # a child stays hidden while its parent is still binned
    if kind in ("session", "run"):
        col = "group_id" if kind == "session" else "session_id"
        parent = "groups" if kind == "session" else "sessions"
        pid = con.execute(f"SELECT {col} FROM {TABLES[kind]} WHERE id=?", (rid,)).fetchone()[0]
        if con.execute(f"SELECT deleted_at FROM {parent} WHERE id=?", (pid,)).fetchone()[0]:
            print(yellow(f"  ! its parent {parent[:-1]} {pid} is still in the bin, "
                         "so this stays hidden until that is restored too"))
    con.close()
    return 0


def cmd_trash(args) -> int:
    con = connect()
    rows = []
    for kind, table in TABLES.items():
        for r in con.execute(
                f"SELECT id, deleted_at FROM {table} WHERE deleted_at IS NOT NULL"
                " ORDER BY deleted_at"):
            rows.append((r["deleted_at"], kind, r["id"]))
    rule("Bin")
    if not rows:
        print(dim("  empty"))
        return 0
    for when, kind, rid in sorted(rows):
        print(f"  {fmt_ts(when)}  {describe_row(con, kind, rid)}")
    print(dim(f"\n  restore:  python3 lcwo.py restore --group ID"
              f"\n  empty it: python3 lcwo.py purge"))
    con.close()
    return 0


def cmd_purge(args) -> int:
    """Permanently remove everything in the bin. The only destructive command."""
    con = connect()
    counts = {k: con.execute(
        f"SELECT COUNT(*) FROM {t} WHERE deleted_at IS NOT NULL").fetchone()[0]
        for k, t in TABLES.items()}
    if not any(counts.values()):
        print(dim("  bin is already empty"))
        return 0
    rule("Purge")
    for k, n in counts.items():
        if n:
            print(f"  {n} {k}(s)")
    print(red("  this cannot be undone."))
    if not args.yes:
        typed = ask_text('  type "purge" to confirm', "", allow_empty=True)
        if typed.strip().lower() != "purge":
            print(dim("  cancelled"))
            return 0
    # children first: deleting a group cascades, but a soft-deleted run inside a
    # live session has to go on its own
    for table in ("runs", "sessions", "groups"):
        con.execute(f"DELETE FROM {table} WHERE deleted_at IS NOT NULL")
    con.commit()
    bad = con.execute("PRAGMA foreign_key_check").fetchall()
    if bad:
        raise SystemExit(f"purge left dangling references: {bad[:3]}")
    print(green("  ✓ bin emptied"))
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
        check("html builds", 'id="f-from"' in html and 'id="f-drill"' in html
              and 'id="data"' in html)
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

        # drill and speed belong to the session, not the assignment
        mixed = create_group(con, assignment="MIX", label="MIX")
        m1 = start_session(con, get_group(con, mixed), mode="letters",
                           char_wpm=25, eff_wpm=6)
        add_run(con, m1["id"], ["EH"], "raw", is_final=True)
        finish_session(con, m1["id"], ["EH"])
        check("group inherits the first session's settings",
              get_group(con, mixed)["mode"] == "letters"
              and get_group(con, mixed)["char_wpm"] == 25)
        m2 = start_session(con, get_group(con, mixed), mode="custom",
                           char_wpm=28, eff_wpm=8)
        add_run(con, m2["id"], ["SM"], "raw", is_final=True)
        finish_session(con, m2["id"], ["SM"])
        check("one group holds two drills",
              group_drills(con, mixed) == "Error practice, Letters")
        check("each session keeps its own speed",
              [r["char_wpm"] for r in group_sessions(con, mixed)] == [25, 28])
        check("group default follows the latest session",
              get_group(con, mixed)["mode"] == "custom")

        # merging two groups that share an assignment
        dup = create_group(con, assignment="MIX", label="MIX dup",
                           created_at="2099-01-01T00:00:00+00:00")
        d1 = start_session(con, get_group(con, dup), mode="letters",
                           started_at="2099-01-01T00:00:00+00:00")
        add_run(con, d1["id"], ["TR"], "raw", is_final=True)
        finish_session(con, d1["id"], ["TR"])
        runs_before = con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        plan = [e for e in merge_plan(con) if e["assignment"] == "MIX"]
        check("merge plan finds the duplicate", len(plan) == 1
              and [g["id"] for g in plan[0]["absorb"]] == [dup])
        merge_groups(con, plan[0])
        check("duplicate group is gone", get_group(con, dup) is None)
        check("merge kept every run",
              con.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == runs_before)
        merged = group_sessions(con, mixed)
        check("sessions renumbered 1..n", [x["seq"] for x in merged] == [1, 2, 3])
        check("sessions ordered by time",
              [x["started_at"] for x in merged] == sorted(x["started_at"] for x in merged))
        check("label normalised to the assignment", get_group(con, mixed)["label"] == "MIX")
        check("merge is idempotent",
              not [e for e in merge_plan(con) if e["assignment"] == "MIX"])

        # the bin: soft delete hides rows without destroying them
        demo = create_group(con, assignment="DEMO", label="DEMO")
        ds = start_session(con, get_group(con, demo), mode="letters",
                           char_wpm=25, eff_wpm=6)
        add_run(con, ds["id"], ["EH", "SM"], "raw", is_final=True)
        finish_session(con, ds["id"], ["EH", "SM"])
        live0 = len(all_groups(con))
        rows0 = con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]

        con.execute("UPDATE groups SET deleted_at=? WHERE id=?", (now_iso(), demo))
        con.commit()
        check("binned group hidden from listings", len(all_groups(con)) == live0 - 1)
        check("binned group hidden from get_group", get_group(con, demo) is None)
        check("binned group still fetchable for restore",
              get_group_any(con, demo) is not None)
        check("binned group's rows stay on disk",
              con.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == rows0)
        check("binned group's sessions hidden", not con.execute(
            "SELECT COUNT(*) FROM live_sessions WHERE group_id=?", (demo,)).fetchone()[0])
        check("binned group's runs hidden by containment", not con.execute(
            "SELECT COUNT(*) FROM live_runs WHERE session_id=?", (ds["id"],)).fetchone()[0])
        check("binned group absent from the report payload",
              all(g["id"] != demo for g in report_payload(load_all(con))["groups"]))
        check("prune leaves binned rows alone", prune_empty(con) == (0, 0)
              and get_group_any(con, demo) is not None)

        con.execute("UPDATE groups SET deleted_at=NULL WHERE id=?", (demo,))
        con.commit()
        check("restore brings the group back", get_group(con, demo) is not None)
        check("restore brings its runs back", con.execute(
            "SELECT COUNT(*) FROM live_runs WHERE session_id=?", (ds["id"],)).fetchone()[0] == 1)

        # a run binned on its own, inside a live session
        rid = con.execute("SELECT id FROM runs WHERE session_id=?", (ds["id"],)).fetchone()[0]
        con.execute("UPDATE runs SET deleted_at=? WHERE id=?", (now_iso(), rid))
        con.commit()
        check("binned run hidden, session still live", not session_runs(con, ds["id"])
              and con.execute("SELECT COUNT(*) FROM live_sessions WHERE id=?",
                              (ds["id"],)).fetchone()[0] == 1)
        check("prune keeps a session whose only run is binned",
              prune_empty(con) == (0, 0) and con.execute(
                  "SELECT COUNT(*) FROM sessions WHERE id=?", (ds["id"],)).fetchone()[0] == 1)
        con.execute("UPDATE runs SET deleted_at=NULL WHERE id=?", (rid,))
        con.execute("UPDATE groups SET deleted_at=? WHERE id=?", (now_iso(), demo))
        con.commit()
        for t in ("runs", "sessions", "groups"):
            con.execute(f"DELETE FROM {t} WHERE deleted_at IS NOT NULL")
        con.commit()
        check("purge removes the rows for good", get_group_any(con, demo) is None)
        check("purge left no dangling references",
              not con.execute("PRAGMA foreign_key_check").fetchall())

        # sending practice generated from a weighted trouble set
        rng = random.Random(11)
        ps = practice_set(Counter({"A": 9, "B": 1}), n=200, rng=rng)
        check("practice set is the size asked for", len(ps) == 200)
        check("practice groups only use the given characters",
              set("".join(ps)) == {"A", "B"})
        check("every character gets a run of its own first",
              ps[0] == ps[0][0] * len(ps[0]) and ps[1] == ps[1][0] * len(ps[1])
              and {ps[0][0], ps[1][0]} == {"A", "B"})
        check("the worst character leads", ps[0][0] == "A")
        check("group lengths stay in range", all(2 <= len(g) <= 3 for g in ps))
        check("weighting favours the worse character",
              "".join(ps).count("A") > 3 * "".join(ps).count("B"))
        check("mixed groups appear", any(len(set(g)) > 1 for g in ps))
        check("solo groups appear", sum(len(set(g)) == 1 for g in ps) > 20)
        check("a seed repeats a set",
              practice_set(Counter({"A": 9, "B": 1}), n=20, rng=random.Random(3))
              == practice_set(Counter({"A": 9, "B": 1}), n=20, rng=random.Random(3)))
        check("nothing to practise yields nothing", practice_set(Counter()) == [])
        check("one character is all solos",
              all(set(g) == {"Q"} for g in practice_set(Counter({"Q": 2}), n=5)))
        small = practice_set(Counter({"A": 3, "B": 2, "C": 1}), n=3,
                             rng=random.Random(5))
        check("a short set still covers every character",
              set("".join(small)) == {"A", "B", "C"})
        check("the worst character still leads", small[0][0] == "A")
        wide = practice_set(Counter({c: 2 for c in "ABCDEFGH"}), n=8,
                            rng=random.Random(5))
        check("a long trouble list still leaves room to mix",
              sum(len(set(g)) > 1 for g in wide) >= 3)
        check("and still covers every character", set("".join(wide)) == set("ABCDEFGH"))

        # the pairs you mix up: both directions are one drill
        cc = Counter({("H", "S"): 2, ("S", "H"): 1, ("K", "R"): 2, ("A", "B"): 1})
        cp = confused_pairs(cc)
        check("confusions merge in both directions", cp[0] == ("H", "S", 3))
        check("pairs come worst first",
              [x[:2] for x in cp] == [("H", "S"), ("K", "R")])
        check("a one-off pair is below the threshold",
              all(x[:2] != ("A", "B") for x in cp))
        check("top caps the list", len(confused_pairs(cc, top=1)) == 1)
        pd = pair_drill(cp, per=3, rng=random.Random(4))
        check("one drill per pair", len(pd) == 2 and all(len(x[3]) == 3 for x in pd))
        check("pair groups use only their two characters",
              all(set(g) <= {a, b} for a, b, _n, gs in pd for g in gs))
        check("every pair group holds both characters",
              all(set(g) == {a, b} for a, b, _n, gs in pd for g in gs))
        check("a seed repeats a pair drill",
              pair_drill(cp, rng=random.Random(4)) == pair_drill(cp, rng=random.Random(4)))
        check("no confusions, no drill", pair_drill([]) == [])
        check("confusions are read from the graded runs",
              confusion_counts([load_group(con, gid)])[("H", "S")] == 1)
        check("a window filters confusions",
              not confusion_counts([load_group(con, gid)], days=["1999-01-01"]))

        # a window over the days actually practised, for "how did this week go"
        span = create_group(con, assignment="SPAN", label="SPAN")
        for i, day in enumerate(("2026-03-01", "2026-03-02", "2026-03-05")):
            sp = start_session(con, get_group(con, span), mode="letters",
                               started_at=f"{day}T09:00:00+00:00")
            add_run(con, sp["id"], ["EH", "SM" if i == 0 else "S."], "raw",
                    is_final=True, recorded_at=f"{day}T09:0{i}:00+00:00")
            finish_session(con, sp["id"], ["EH", "SM"])
        sv = [load_group(con, span)]
        check("practice days are the days with graded runs",
              practice_days(sv) == ["2026-03-01", "2026-03-02", "2026-03-05"])
        recent = practice_days(sv)[-2:]
        check("a window skips over days off", recent == ["2026-03-02", "2026-03-05"])
        miss_all, sent_all = char_counts(sv)
        miss_win, sent_win = char_counts(sv, recent)
        check("the window drops the oldest day's characters",
              sent_win == Counter({"E": 2, "H": 2, "S": 2, "M": 2})
              and sent_all["E"] == 3)
        check("the window keeps only its own misses",
              miss_win["M"] == 2 and miss_all["M"] == 2)
        check("an oversized window is just everything",
              char_counts(sv, practice_days(sv)[-99:]) == (miss_all, sent_all))
        check("a window thresholds the combined total, not each day",
              trouble_from(char_counts(sv, recent)[0]) == [("M", 2)]
              and not trouble_from(char_counts(sv, recent[:1])[0]))

        # operators: every group belongs to one, and listings follow
        me = create_operator(con, "Test Op", " w0test ")
        check("call sign normalised", get_operator(con, me)["callsign"] == "W0TEST")
        check("found by call sign", find_operator(con, "w0test")["id"] == me)
        check("found by id", find_operator(con, str(me))["id"] == me)
        check("found by name", find_operator(con, "test op")["id"] == me)
        check("adopting claims the older groups", adopt_unassigned(con, me) > 0
              and all(g["operator_id"] == me for g in all_groups(con)))
        check("the only operator becomes current", current_operator(con)["id"] == me)
        them = create_operator(con, "Other Op", "K0OTH")
        check("current stays put when a second appears",
              current_operator(con)["id"] == me)
        theirs = create_group(con, assignment="THEIRS", label="THEIRS",
                              operator_id=them)
        check("listings are scoped to one operator",
              [g["id"] for g in all_groups(con, them)] == [theirs])
        check("the other operator cannot see it",
              theirs not in [g["id"] for g in all_groups(con, me)])
        check("unscoped listings still see everyone",
              len(all_groups(con)) == len(all_groups(con, me)) + 1)
        check("last_group is per operator", last_group(con, them)["id"] == theirs)
        check("payload carries the operator", report_payload(
            load_all(con, them), list_operators(con))["groups"][0]["op"] == them)
        set_current_operator(con, them)
        check("switching operator sticks", current_operator(con)["id"] == them)
        set_current_operator(con, me)
        check("merging stays inside one operator", all(
            len({_col(g, "operator_id") for g in [e["target"]] + e["absorb"]}) == 1
            for e in merge_plan(con)))

        # a database written before operators existed must migrate intact
        old = Path(td) / "old.db"
        oc = sqlite3.connect(old)
        oc.executescript("""
            CREATE TABLE groups (id INTEGER PRIMARY KEY, label TEXT, mode TEXT,
              assignment TEXT NOT NULL, char_wpm REAL NOT NULL, eff_wpm REAL NOT NULL,
              created_at TEXT NOT NULL, closed_at TEXT);
            CREATE TABLE sessions (id INTEGER PRIMARY KEY, group_id INTEGER NOT NULL
              REFERENCES groups(id) ON DELETE CASCADE, seq INTEGER NOT NULL,
              mode TEXT NOT NULL, assignment TEXT NOT NULL, char_wpm REAL NOT NULL,
              eff_wpm REAL NOT NULL, started_at TEXT NOT NULL, ended_at TEXT,
              key_json TEXT, UNIQUE (group_id, seq));
            CREATE TABLE runs (id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL
              REFERENCES sessions(id) ON DELETE CASCADE, seq INTEGER NOT NULL,
              is_final INTEGER NOT NULL DEFAULT 0, recorded_at TEXT NOT NULL,
              attempt_json TEXT NOT NULL, reported_json TEXT, raw_paste TEXT NOT NULL,
              UNIQUE (session_id, seq));
            INSERT INTO groups VALUES
              (1,'OLD','letters','OLD',25,6,'2026-01-01T00:00:00+00:00',NULL);
            INSERT INTO sessions VALUES
              (1,1,1,'letters','OLD',25,6,'2026-01-01T00:00:00+00:00',NULL,'["EH"]');
            INSERT INTO runs VALUES
              (1,1,1,1,'2026-01-01T00:00:00+00:00','["EH"]',NULL,'raw');
        """)
        oc.commit()
        oc.close()
        mc = connect(old)
        check("migration keeps every row",
              mc.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
              and len(all_groups(mc)) == 1)
        check("migration adds operator_id", "operator_id" in
              {r["name"] for r in mc.execute("PRAGMA table_info(groups)")})
        legacy = create_operator(mc, "Legacy Op", "W0OLD")
        check("pre-operator groups adopt the first operator",
              adopt_unassigned(mc, legacy) == 1
              and all_groups(mc, legacy)[0]["label"] == "OLD")
        check("migration left no dangling references",
              not mc.execute("PRAGMA foreign_key_check").fetchall())
        mc.close()

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

    rec = sub.add_parser("record", help="record sessions (default)")
    rec.set_defaults(fn=cmd_record)
    grp = sub.add_parser("groups", help="list groups")
    grp.set_defaults(fn=cmd_groups)

    u = sub.add_parser("user", help="show operators, add one, or switch")
    u.add_argument("--add", action="store_true", help="add an operator")
    u.add_argument("--use", metavar="WHO", help="record for this operator from now on")
    u.add_argument("--remove", metavar="WHO", help="remove an operator who owns no groups")
    u.add_argument("--name", help="with --add, or on its own to rename the current one")
    u.add_argument("--call", help="call sign, same rules as --name")
    u.set_defaults(fn=cmd_user)

    r = sub.add_parser("report", help="build the HTML report")
    r.add_argument("-g", "--group", type=int, help="limit to one group id")
    r.add_argument("-o", "--out", help="output path")
    r.add_argument("--open", action="store_true", help="open in a browser")
    r.set_defaults(fn=cmd_report)

    t = sub.add_parser("trouble", help="show trouble letters")
    t.add_argument("-g", "--group", type=int)
    t.add_argument("-n", "--threshold", type=int, default=TROUBLE_THRESHOLD)
    t.add_argument("-d", "--days", type=int, metavar="N",
                   help="only the last N days you practised")
    t.set_defaults(fn=cmd_trouble)

    k = sub.add_parser("key", help="attach a results table to an ungraded session")
    k.add_argument("-s", "--session", type=int)
    k.set_defaults(fn=cmd_key)

    pr = sub.add_parser("practice", help="sending practice from your trouble letters")
    pr.add_argument("-g", "--group", type=int)
    pr.add_argument("-n", "--threshold", type=int, default=TROUBLE_THRESHOLD)
    pr.add_argument("-d", "--days", type=int, metavar="N",
                    help="only the last N days you practised")
    pr.add_argument("-c", "--count", type=int, default=24, help="how many groups")
    pr.add_argument("--chars", help="use these characters instead of the trouble list")
    pr.add_argument("--pairs", action="store_true",
                    help="add drills for the characters you mix up")
    pr.add_argument("--pair-count", type=int, default=6, metavar="N",
                    help="how many confused pairs to drill (with --pairs)")
    pr.add_argument("--plain", action="store_true", help="just the groups, one line")
    pr.add_argument("--seed", type=int, help="repeat an earlier set")
    pr.set_defaults(fn=cmd_practice)

    sp = sub.add_parser("speed", help="show or set a group's speed")
    sp.add_argument("-g", "--group", type=int)
    sp.add_argument("--char", type=float, help="character speed in wpm")
    sp.add_argument("--eff", type=float, help="effective speed in wpm")
    sp.set_defaults(fn=cmd_speed)

    for name, helptext in (("delete", "move a group/session/run to the bin"),
                           ("restore", "bring one back from the bin")):
        q = sub.add_parser(name, help=helptext)
        q.add_argument("-g", "--group", type=int)
        q.add_argument("-s", "--session", type=int)
        q.add_argument("-r", "--run", type=int)
        q.add_argument("-y", "--yes", action="store_true", help="skip the prompt")
        q.set_defaults(fn=cmd_delete if name == "delete" else cmd_restore)

    sub.add_parser("trash", help="list what is in the bin").set_defaults(fn=cmd_trash)
    pg = sub.add_parser("purge", help="permanently delete everything in the bin")
    pg.add_argument("-y", "--yes", action="store_true", help="skip the confirmation")
    pg.set_defaults(fn=cmd_purge)

    mg = sub.add_parser("merge", help="merge groups that share an assignment")
    mg.add_argument("--apply", action="store_true", help="write the change")
    mg.set_defaults(fn=cmd_merge)

    sub.add_parser("selftest", help="run built-in checks").set_defaults(fn=cmd_selftest)

    # every command that reads or writes practice data works on one operator
    for q in (rec, grp, r, t, k, sp, pr):
        q.add_argument("-u", "--user", metavar="WHO",
                       help="operator: call sign, name, or id")
    for q in (grp, r, t, pr):
        q.add_argument("--everyone", action="store_true",
                       help="every operator, not just the current one")

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
