# lcwo tutor

Grade your [LCWO](https://lcwo.net) Morse code practice, track which characters
you keep missing, and see whether you are getting better.

Python 3, standard library only. No installs, no network, one file.

```
make            record a session
make report     build the HTML report and open it
make help       everything else
```

## How it works

```
operator  ──  you: a name and a call sign
 └ group  ──  one homework assignment (S2HW3)
    └ session  ──  one Copy exercise = one audio clip, with its own drill and speed
       └ run   ──  one attempt at that clip
```

The first time you record, it asks who is copying and files everything under
that call sign. With one operator on file it never asks again; add a second
(`make user ADD=1`) and recording starts by asking which of you it is. Every
group, and so every number in every report, belongs to exactly one operator.

CW Academy homework alternates **Send 1 → Copy 1, Send 2 → Copy 2, …**. Only the
copies are gradeable, so a send block is simply a gap: come back to copying and
you add a *session*, not a new group. Drill type and speed belong to the session,
because one assignment can start on letters and finish on custom error characters.
A new group means a new assignment.

While working a lesson you replay the same clip and retype your answers until
you are happy with them. Paste each attempt as you go — two-character groups,
one per line, `.` for a character you did not know:

```
EH
SM
.R
```

When you finally submit on LCWO you get its results table. **That table is both
your last run and the answer key**, so pasting it records the final attempt,
grades every earlier attempt in the session retroactively, and closes the
session. The tool detects which kind of paste you gave it, so there is no mode
to switch.

```
Sent Group   Received Group   Errors
EH           EH               EH       0
HR           SR               HR       1
```

## What you get

A self-contained HTML report with a filter bar at the top. The dimensions are
independent and combine — **a date range, an operator, an assignment, a session,
a drill type** — so "the last two days, letters only" is one filter rather than
a view somebody had to think of in advance. Every panel recomputes for whatever
is set:

- **Practice these** — characters missed twice or more **across everything the
  filter covers**, not per day. Miss `W` once on Tuesday and once on Wednesday
  and a two-day range lists it at ×2; the columns beside it decompose that
  total by day (or session, or assignment) rather than each applying the
  threshold again
- **Confusions** — `Y → L ×5`, `K → R ×4`. Usually the most useful panel: a
  consistent substitution means two rhythms you are conflating, which needs a
  different fix from a character you simply do not know
- **Accuracy per run** — hover any point for its run and score
- **Runs in scope** — click a row to see exactly what went wrong, group by group

Presets across the top (*All time*, the latest day, *Last 2 days*, latest
assignment, latest session) set the whole filter in one click, **Clear** resets
it, and each dropdown shows how many runs a choice would leave you with, so an
empty combination is visible before you pick it. The filter lives in the URL
(`#from=2026-09-09&drill=letters`), so a particular view is linkable and
survives a reload.

The same question from the terminal:

```
make trouble D=2        letters missed 2+ times over your last 2 practice days
make trouble D=7 N=3    missed 3+ times over the last 7
```

`D=` counts days you actually practised, not calendar days — skip a Tuesday and
`D=2` still means your last two sessions' worth. Same threshold rule as the
report: the count is the total over the whole window.

## Sending practice

The copy side tells you which characters you are missing; `make practice` turns
that straight into something to send:

```
$ make practice D=2 C=12
── Sending practice — last 2 practice day(s), 2026-09-10 to 2026-09-13 · W7YFR
  from: D×9  G×5  H×4  J×4  L×4  Y×4  C×3  F×3  K×3  U×3

   DD    GGG   HHH   JJJ   LL    YY
   CJY   UF    JK    HJU   UF    CC
```

It opens with each character on its own — its rhythm with nothing to compare it
to, worst first — then mixes them, drawn weighted by how often you missed them,
so the worst come round most and you practise the transitions between them too.
At most half the set is solo runs; anything crowded out that way is planted into
the mixed groups, so every character you are working on still appears.

`PAIRS=1` adds a second block for the characters you actually mix up, taken from
the confusion table — every group in it holds both halves of the pair, because
the thing to practise is the contrast:

```
── Confusions — the pairs you mix up
   H ↔ S ×6        HS    SHS   SH
   L ↔ Y ×6        LLY   LYY   LYL
   K ↔ R ×4        RRK   KKR   RKR
```

```
make practice              your trouble letters, all time, 24 groups
make practice PAIRS=1      plus drills for the pairs you confuse
make practice D=2          just what you have been missing lately
make practice N=3 C=40     stricter threshold, longer drill
make practice CHARS=kyv    forget the stats, practise these
make practice PLAIN=1      one line, no formatting, for piping
make practice SEED=7       the same set again
```

## Commands

| Command | What it does |
|---|---|
| `make` | record a session (resume the last group, or start a new one) |
| `make report` | rebuild the HTML report and open it |
| `make user` | who is on file, and who is being recorded for |
| `make groups` | list groups with accuracy and trouble letters |
| `make trouble` | trouble letters — `D=2` for the last 2 practice days, `N=3` for the threshold |
| `make practice` | sending practice built from those letters — same `D=`/`N=`, `PAIRS=1` for confusions |
| `make merge` | fold groups that share an assignment into one (`APPLY=1` to write) |
| `make delete G=11` | move a group (or `S=`/`R=`) to the bin — reversible |
| `make restore G=11` | bring it back |
| `make trash` | list what's in the bin |
| `make purge` | permanently delete everything in the bin |
| `make db` | SQLite shell on the database |
| `make key` | grade a session you left unfinished |
| `make speed` | show or set a group's wpm — `G=2 CHAR=25 EFF=6` |
| `make test` | run the Python and browser test suites |

All of it works without `make` too: `python3 lcwo.py <command>`.

Anything that reads or writes practice data takes `U=<call sign>` to act as
another operator for that one command (`make report U=KD7ABC`), and the
listing commands take `EVERYONE=1` to drop the filter entirely.

## More than one operator

```
make user                        who is on file, and who is current
make user ADD=1                  add one (asks for name and call sign)
make user USE=KD7ABC             record for them from now on
make user NAME="Sam Example"     rename the current operator (or CALL=)
make user REMOVE=KD7ABC          remove one, if they own no groups
```

Switching is sticky — it is remembered until you switch again — and `record`
shows you who it is recording for before it asks anything else. Each operator
gets their own report under `reports/<call sign>/` once there is more than one,
so nobody overwrites anybody. `make report EVERYONE=1` builds a combined page
with an extra **By operator** filter.

A database recorded before operators existed still works: the first operator
you add adopts every group already in it.

## Demoing, and undoing

Deleting is soft. `make delete G=11` stamps the row with `deleted_at` and it
vanishes from every listing, stat and report — but nothing leaves the database,
and `make restore G=11` brings it back. So you can record a throwaway group
while showing someone the tool, then hide it afterwards without risking real
practice data.

Visibility cascades by containment: binning a group hides its sessions and runs
without touching those rows, which is why restoring is one update rather than a
bookkeeping exercise. You can also bin a single session or run (`S=`, `R=`).

`make trash` lists the bin. `make purge` is the only command that destroys
anything; it asks you to type `purge` to confirm.

For anything the commands don't cover, `make db` opens a SQLite shell. The
tables are `operators`, `settings`, `groups`, `sessions`, `runs`; the views
`live_groups`, `live_sessions`, `live_runs` apply the bin rule for you, and are
what the tool reads.

## Your data stays local

Everything lives in `lcwo.db` next to the script — including your name and
call sign — and nothing is ever sent anywhere. `.gitignore` keeps both the
database and `reports/` out of version control — **a generated report embeds
the whole graded dataset as JSON**, so publishing one publishes every session
it covers, and the call sign it belongs to.

## More

[DESIGN.md](DESIGN.md) covers how errors are classified, how the report is
built and tested, and the database schema.

## License

MIT — see [LICENSE](LICENSE).
