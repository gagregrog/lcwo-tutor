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

A **trouble letter** is any character missed 2 or more times within the
current filter.
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

A note on what the threshold applies to: `trouble()` runs over the totals for
*everything the filter covers*, so two misses spread across two days count the
same as two in one sitting. The breakdown columns then split that total by the
child level — they are a decomposition of the listed number, never a second
threshold. Thresholding per day and unioning would answer a different and much
less useful question ("did I have a bad day with this letter"), and would drop
exactly the characters that are quietly wrong all week.

## Sending practice

`lcwo practice` reads the same trouble tally the report does and emits groups to
send. Three decisions in `practice_set` are worth recording:

- **Weighted, not uniform.** Draws are weighted by miss count, so the character
  you missed nine times comes round roughly nine times as often as the one you
  missed once. Uniform sampling would spend the same effort on a character you
  nearly have as on one you keep dropping.
- **Solo runs, capped at half the set.** A run of one character (`DDD`) drills
  its rhythm with nothing to compare it to; mixed groups (`CJY`) drill the
  transitions, which is where sending actually falls apart. Both are needed, but
  a ten-character trouble list would spend an entire drill on solos, so solos
  are capped at half and the worst characters get them.
- **Coverage is guaranteed by construction.** Characters crowded out of the solo
  pass are planted into mixed groups as they are generated, rather than patched
  in afterwards. Splicing them in after the fact can overwrite the only
  appearance of some other character - the same bug, one level down.

`--pairs` adds a second block built from the confusion table rather than the
miss tally. `H` heard as `S` and `S` heard as `H` are the same two rhythms
failing to separate, so the directions are merged into one unordered pair and
their counts add. Every group in that block contains both characters - a group
of one would just be the solo drill the main set already ran, and the contrast
is the whole point.

`--seed` makes a set reproducible, which is what the tests use; without it each
run is fresh.

## The report

`reports/index.html` covers one operator's practice; `reports/group-N.html` is
one group. Once a second operator exists each gets their own tree
(`reports/<call sign>/`) so a rebuild cannot overwrite somebody else's page.
Self-contained (no network, no CDN), light/dark aware, rebuilt at the end of a
recording run or on demand with `make report`.

### The filter

Everything on the page is driven by one filter object with six independent
dimensions, AND-ed together:

```js
{from, to, op, gid, sid, drill}   // null means "don't care"
```

The first version had a single `scope` select — all time, *or* a day, *or* a
group. That only answers questions somebody predicted. "Which letters did I
miss twice over the last two days" is a date range crossed with nothing else,
and no single-select could express it: the two days spanned two assignments,
and the assignment that covered them also covered a third day.

So the control is a bar of small selects, one per dimension, rather than a list
of named views. Consequences worth knowing:

- **Counts are contextual.** Each option shows the runs you would be left with
  *given the other filters*, computed by re-running the filter with that one
  dimension swapped. An empty combination is visible before you click it.
- **The controls keep themselves coherent.** Sessions cascade off the chosen
  assignment; picking a session pins its assignment; an inverted date range
  clears the other end instead of silently showing nothing.
- **The breakdown columns follow what is pinned** rather than a scope type:
  runs inside a session, sessions inside an assignment, days when the range
  spans several, assignments otherwise.
- **The whole filter is the URL** — `#from=2026-09-09&drill=letters`. Links
  written by the old scheme (`#group:8`, `#day:…`, `#drill:…`) still resolve,
  translated on the way in.
- Operator and drill controls hide themselves when there is only one of them,
  so the bar stays short for the common case.

Presets are just filters: *All time*, the latest day, *Last 2 days*, latest
assignment, latest session. "Last N days" counts days with practice rather than
calendar days, so a day off does not empty the window — the same rule the CLI's
`lcwo trouble --days N` uses.

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

## Operators

Every group carries an `operator_id`; sessions and runs inherit one through
their group. Nothing else needed a column: a group is one homework assignment
and homework belongs to one person, so putting the owner any deeper would only
create the possibility of a group split between two operators — a state with no
real-world meaning that every rollup would then have to handle.

Which operator is current lives in `settings`, not in a column on `operators`,
so "who am I recording for" is one row to write and cannot end up true of two
rows at once. It is sticky across invocations, which is what makes the common
single-operator case silent: `record` prints who it is recording for and asks
nothing. The prompt only appears when the answer is genuinely ambiguous — no
operators on file, or more than one.

Scoping is a query filter (`_owned`), not another view. The soft-delete views
answer "does this row exist"; an operator filter answers "whose is it", and
folding the second into the first would make one person's data look deleted to
another and quietly change what `trash` and `purge` see.

A database from before this existed has `operator_id IS NULL` everywhere. The
first operator added adopts those rows (`adopt_unassigned`), which is the only
backfill that needs no guesswork — there was exactly one person using it.

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

SQLite at `lcwo.db` next to the script — `operators`, `settings`, `groups`,
`sessions`, `runs`. Grading is derived at read time, never stored. Every
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

- **New per-operator data** (goals, target speeds, exam dates) — hangs off
  `operators`; nothing below the group level needs to know about it.
- **New drill types** — add to `MODES` at the top; it's stored as a plain string,
  so old rows keep working.
- **Longer groups** — nothing assumes 2 characters. Five-char code groups grade
  correctly as-is; the parser just warns when group widths are inconsistent,
  which is how a mangled paste announces itself.
- **New filter dimensions** — add the key to `EMPTY`, one clause to
  `filterRuns`, and one select to the bar; everything else (URL, counts,
  presets, validation) iterates over `DIMS`.
- **New metrics** — grading is pure functions over `(key, attempt)` in the
  grading-engine section, with no I/O. Add a property to `RunGrade` and it
  rolls up through `SessionView` → `GroupView` automatically.


---
