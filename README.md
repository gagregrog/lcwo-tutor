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
group  ──  a batch of sessions at one drill / assignment / speed
 └ session  ──  one LCWO lesson = one audio clip
    └ run   ──  one attempt at that clip
```

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

A self-contained HTML report with a scope filter at the top — **all time, a
day, a group, or a session** — that recomputes every panel for what you picked:

- **Practice these** — characters missed twice or more, broken down across the
  level below whatever you are looking at
- **Confusions** — `Y → L ×5`, `K → R ×4`. Usually the most useful panel: a
  consistent substitution means two rhythms you are conflating, which needs a
  different fix from a character you simply do not know
- **Accuracy per run** — hover any point for its run and score
- **Runs in scope** — click a row to see exactly what went wrong, group by group

## Commands

| Command | What it does |
|---|---|
| `make` | record a session (resume the last group, or start a new one) |
| `make report` | rebuild the HTML report and open it |
| `make groups` | list groups with accuracy and trouble letters |
| `make trouble` | trouble letters in the terminal — `N=3` to change the threshold |
| `make key` | grade a session you left unfinished |
| `make speed` | show or set a group's wpm — `G=2 CHAR=25 EFF=6` |
| `make test` | run the Python and browser test suites |

All of it works without `make` too: `python3 lcwo.py <command>`.

## Your data stays local

Everything lives in `lcwo.db` next to the script, and nothing is ever sent
anywhere. `.gitignore` keeps both the database and `reports/` out of version
control — **a generated report embeds the whole graded dataset as JSON**, so
publishing one publishes every session it covers.

## More

[DESIGN.md](DESIGN.md) covers how errors are classified, how the report is
built and tested, and the database schema.

## License

MIT — see [LICENSE](LICENSE).
