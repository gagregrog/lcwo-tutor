# Design notes

Background for `lcwo.py` — how grading works, how the report is built and
tested, and how the data is stored. See [README.md](README.md) to just use it.

## How errors are classified

| Kind | Meaning | Example |
|---|---|---|
| **missed** | you marked it `.` — didn't know it | sent `HR`, copied `.R` |
| **wrong** | you heard it as a different character | sent `HR`, copied `SR` |
| **transposed** | right characters, wrong order | sent `WM`, copied `MW` |
| **extra** | you copied a character that was never sent | sent `EH`, copied `EHX` |

A transposition is tracked separately because it means something different:
you knew both characters, you just wrote them down swapped. It still counts
toward the miss tally, but the report breaks it out so you can tell "I don't
know W" from "I keep flipping W and M".

Runs shorter than the key are aligned by index, and the missing tail counts as
missed. The parser also cross-checks its own error count against LCWO's per-group
count and flags any group where they disagree — that usually means the paste lost
a column.


## Trouble letters

A **trouble letter** is any character missed 2 or more times within a scope.
The report shows the tally at every level:

- **per run** — the `Chars missed` column in the session table
- **per session** — a chip row under each session
- **per group** — chips plus a `char × session` matrix, so you can see whether
  something is improving across the group
- **all time** — the practice list at the top of the report, plus a
  `char × group` matrix

Change the threshold with `trouble -n 3`, or edit `TROUBLE_THRESHOLD`.

There's also a **confusion table** (`H → S ×2`) which is usually the most
actionable thing in the report — consistent substitutions point at two characters
whose rhythms you're conflating, which is a different fix from just not knowing one.


## The report

`reports/index.html` covers everything; `reports/group-N.html` is one group.
Self-contained (no network, no CDN), light/dark aware, rebuilt at the end of a
recording run or on demand with `make report`.

### The scope filter

Everything on the page is driven by one **scope** control at the top:

```
All time  ·  a day  ·  a group  ·  a session
```

Pick a scope and every panel recomputes for it — the tiles, the practice list,
the confusions, the chart, the runs. There are also one-click shortcuts for
*All time*, *today*, *latest group*, and *latest session*, and the scope is
written to the URL fragment, so a particular view is linkable and survives a
reload.

The page carries the graded data as JSON and totals it in the browser. Grading
still happens in Python — the page only ever sums per-character verdicts it is
handed, so the browser cannot disagree with the CLI about what counts as an
error. This is why the filter exists at all: the aggregate numbers genuinely
have to be *recalculated* per scope, and pre-rendering ~40 variants server-side
would have meant a multi-megabyte file. As one set of panels, it is 37 KB and
grows with data rather than with the number of groups.

### Layout, most useful first

1. **Tiles** — chars copied, wrong, percent wrong, accuracy (with the
   first→last change in scope), best run, trouble-letter count.
2. **Practice these** — trouble letters, plus a breakdown across the level
   *below* the current scope: all time splits by group, a group splits by
   session, a session splits by run. That is the "see it across levels" view.
3. **Confusions** — `sent → heard` pairs, usually the most actionable panel.
4. **Accuracy per run** — sparkline over the runs in scope. Only the first and
   last runs are labelled on the axis; per-point labels collide past a handful
   of runs. Hover any point and the caption above names it and its accuracy.
5. **Runs in scope** — one row per run; click any row to expand its errors,
   with the full group-by-group grid behind a toggle. The accuracy bar runs
   red → green with the score rather than being uniformly green.
6. **Every character in scope** — exposure and miss rate, collapsed by default.
   Breakdown columns that are entirely zero in the current scope are dropped,
   so a "Transposed" column only appears once you have actually transposed
   something.
7. **About this group/session** — mode, assignment, speed, and the imported note.

### Testing the report

`lcwo.py selftest` covers the Python grading engine and cannot see a broken
click handler, so `test_report.py` extracts the published page's payload and
script, runs them under node with a DOM shim, and drives the real handlers.
It asserts on a synthetic fixture with known properties, then smoke-tests every
scope against the live database. `make test` runs both; it skips cleanly when
node is not installed.

The shim deliberately tracks elements created via `innerHTML`. An earlier
version returned `[]` from `querySelectorAll`, which meant no handler was ever
invoked — and a quick-filter button that passed the wrong argument shipped
looking fully tested.


## Why drill type lives on the session

An assignment alternates send and copy, and can change drill partway through —
letters for the first copies, custom error characters for the last. If the drill
were a group property, a single homework day would have to be split across two
groups, which is what the first version did.

The cost of folding them back together is that a group's aggregate miss rate now
mixes normal letters with error practice, and error practice deliberately
over-samples your worst characters. The filter absorbs that: pick a group to ask
"how did this assignment go", pick a drill to ask "how am I doing on plain
letters". Keeping the split in the data model would have answered the second
question only, and permanently.

`lcwo.py merge` folds groups that share an assignment into one, re-sequencing
their sessions chronologically. It is a dry run unless given `--apply`, and it
refuses to finish if the run count changes or a foreign key is left dangling.

## Soft delete

`groups`, `sessions` and `runs` each carry a nullable `deleted_at`. Rather than
add `WHERE deleted_at IS NULL` to twenty-odd read queries and hope none is
missed later, the rule lives in three SQL views:

```sql
CREATE VIEW live_groups AS
    SELECT * FROM groups WHERE deleted_at IS NULL;
CREATE VIEW live_sessions AS
    SELECT s.* FROM sessions s JOIN groups g ON g.id = s.group_id
    WHERE s.deleted_at IS NULL AND g.deleted_at IS NULL;
CREATE VIEW live_runs AS
    SELECT r.* FROM runs r JOIN live_sessions s ON s.id = r.session_id
    WHERE r.deleted_at IS NULL;
```

Visibility therefore cascades by containment — binning a group hides its
sessions and runs without writing to them, so restoring is a single update.
The views are dropped and recreated on every connect, so a changed definition
cannot go stale, and they are created *after* the column migration since they
reference `deleted_at`.

Two places deliberately read the base tables instead:

- **sequence numbers.** `start_session` and `add_run` compute `seq` from the
  base tables so a binned row keeps its slot and restoring cannot collide.
- **`prune_empty`.** It removes sessions with no runs; reading `live_runs`
  would make a session whose runs are merely binned look empty and hard-delete
  it. It also skips binned rows entirely.

`purge` is the only destructive command. It deletes children before parents and
verifies `PRAGMA foreign_key_check` afterwards.

## Storage

SQLite at `lcwo.db` next to the script — four tables (`groups`, `sessions`,
`runs`, and the derived grading is computed at read time, never stored). Every
session and run is timestamped, so date-based analysis later is just SQL. The
raw text of every paste is kept verbatim in `runs.raw_paste`, so if the grading
logic ever changes, everything re-grades from source with no data loss.

```sql
-- e.g. accuracy over time at a given speed
SELECT s.started_at, s.char_wpm, r.attempt_json
FROM runs r JOIN sessions s ON s.id = r.session_id
WHERE s.char_wpm >= 20 ORDER BY s.started_at;
```

Override paths with `LCWO_HOME`, `LCWO_DB`, or `LCWO_REPORTS`.


## Extending

- **New drill types** — add to `MODES` at the top; it's stored as a plain string,
  so old rows keep working.
- **Longer groups** — nothing assumes 2 characters. Five-char code groups grade
  correctly as-is; the parser just warns when group widths are inconsistent,
  which is how a mangled paste announces itself.
- **New metrics** — grading is pure functions over `(key, attempt)` in the
  grading-engine section, with no I/O. Add a property to `RunGrade` and it
  rolls up through `SessionView` → `GroupView` automatically.


---
