#!/usr/bin/env python3
"""
Browser-side tests for the HTML report.

The report aggregates and renders in the browser, so `lcwo.py selftest` (which
only covers the Python grading engine) cannot see a broken click handler. This
extracts the page's JSON payload and script, runs them under node with a small
DOM shim, and drives the real event handlers.

It does two passes:
  1. a synthetic fixture with known properties, so assertions are stable
  2. the live database, as a smoke test that every scope still renders

Skips cleanly with exit 0 if node is not installed.

    python3 test_report.py
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lcwo  # noqa: E402

SHIM = r"""
// Tracks elements produced via innerHTML so handlers can actually be fired.
// A shim whose querySelectorAll returns [] silently passes broken handlers.
function parseTags(html, sel){
  const out = []; const re = /<(\w+)([^>]*)>/g; let m;
  while ((m = re.exec(html))){
    const [, tag, attrs] = m;
    const cls = (attrs.match(/class="([^"]*)"/) || [, ''])[1].split(/\s+/);
    const el = {tag, _attrs:{}, dataset:{}, hidden:false, textContent:'', innerHTML:'',
      getAttribute(k){ return this._attrs[k] ?? null; },
      setAttribute(k, v){ this._attrs[k] = v; },
      querySelectorAll(){ return []; }};
    for (const a of attrs.matchAll(/([\w-]+)="([^"]*)"/g)){
      el._attrs[a[1]] = a[2];
      if (a[1].startsWith('data-'))
        el.dataset[a[1].slice(5).replace(/-(\w)/g, (x, c) => c.toUpperCase())] = a[2];
    }
    let ok;
    if (sel.startsWith('.')) ok = cls.includes(sel.slice(1));
    else if (sel.includes('.')){ const [t, c] = sel.split('.'); ok = tag === t && cls.includes(c); }
    else ok = tag === sel;
    if (ok) out.push(el);
  }
  return out;
}
const els = {};
function mk(id){
  if (!els[id]) els[id] = {id, _html:'', textContent:'', value:'', hidden:false,
    dataset:{}, _attrs:{}, _cache:{}, _cacheHtml:null,
    get innerHTML(){ return this._html; }, set innerHTML(v){ this._html = v; },
    getAttribute(k){ return this._attrs[k] ?? null; },
    setAttribute(k, v){ this._attrs[k] = v; },
    // Same html, same element objects: a handler bound through one query has
    // to still be there for the next one, or a dead handler looks alive.
    querySelectorAll(sel){
      if (this._cacheHtml !== this._html){ this._cacheHtml = this._html; this._cache = {}; }
      return this._cache[sel] || (this._cache[sel] = parseTags(this._html, sel));
    },
    querySelector(sel){ return this.querySelectorAll(sel)[0] || null; }};
  return els[id];
}
global.document = {getElementById: mk,
  querySelectorAll(sel){
    const m = sel.match(/^#([\w-]+)\s+(.*)$/);
    return m ? mk(m[1]).querySelectorAll(m[2]) : mk('quick').querySelectorAll(sel);
  }};
global.location = {hash: ''};
global.history = {replaceState(a, b, h){ global.location.hash = h; }};
const _listeners = {};
global.window = {addEventListener(ev, fn){ (_listeners[ev] ||= []).push(fn); }};
// set the fragment and fire hashchange, the way following a link would
global.dispatchHash = h => {
  global.location.hash = h;
  (_listeners.hashchange || []).forEach(f => f());
};
let FAIL = 0;
global.check = (name, ok, extra) => {
  if (ok) console.log('  ok   ' + name);
  else { FAIL++; console.log('  FAIL ' + name + (extra ? '  [' + extra + ']' : '')); }
};
global.done = () => { console.log(FAIL ? `\n${FAIL} failure(s)` : '\n  all browser checks passed');
  process.exit(FAIL ? 1 : 0); };
"""

FIXTURE_TESTS = r"""
// ---- the click path, which is what actually broke in the wild ----
const btns = document.getElementById('quick').querySelectorAll('button');
check('quick presets rendered', btns.length >= 3, 'got ' + btns.length);
for (const b of btns){
  const entry = QUICK[+b.dataset.i];
  check('preset "' + entry[0] + '" carries a filter object',
        !!entry && !!entry[1] && DIMS.every(k => k in entry[1]));
  b.onclick();
  check('preset "' + entry[0] + '" sets a real filter',
        !/undefined|NaN/.test(location.hash), 'hash=' + location.hash);
  check('preset "' + entry[0] + '" marks itself pressed',
        b.getAttribute('aria-pressed') === 'true');
  const h = document.getElementById('app').innerHTML;
  check('preset "' + entry[0] + '" renders', h.length > 200 && !/undefined|NaN/.test(h));
}

// ---- filters combine, which is the whole point ----
setFilter({...EMPTY});
const total = filterRuns(F).length;
check('three practice days in the fixture', days.length === 3, days.join(','));

setFilter({from: days[1]});
const dated = filterRuns(F);
check('a date floor narrows', dated.length < total && dated.length > 0,
      dated.length + '/' + total);
check('nothing before the floor survives', dated.every(r => r.day >= days[1]));

setFilter({from: days[1], to: days[1]});
check('a single-day range is that day alone',
      filterRuns(F).length > 0 && filterRuns(F).every(r => r.day === days[1]));

setFilter({from: days[1], drill: 'letters'});
const both = filterRuns(F);
check('date and drill apply together',
      both.every(r => r.day >= days[1] && drillOf(r) === 'letters'));
check('two conditions are at least as narrow as one', both.length <= dated.length);
check('the label names both dimensions',
      filterLabel(F).includes('Letters') && filterLabel(F).includes(fmtDay(days[1])),
      filterLabel(F));

setFilter({from: days[1], drill: 'letters', op: ops[0]});
check('three conditions apply together', filterRuns(F).every(r =>
  r.day >= days[1] && drillOf(r) === 'letters' && String(opOf(r)) === ops[0]));

setFilter({from: days[days.length - 1], to: days[0]});
check('an impossible range yields nothing', filterRuns(F).length === 0);
check('an empty result is reported, not crashed',
      document.getElementById('app').innerHTML.includes('No graded runs'));

// ---- the controls drive it, and stay coherent ----
setFilter({...EMPTY});
const gsel = document.getElementById('f-gid');
gsel.value = String(DATA.groups[0].id);
gsel.onchange();
check('the assignment control filters', String(F.gid) === String(DATA.groups[0].id));
check('only that assignment is left',
      filterRuns(F).every(r => r.gid === DATA.groups[0].id));
const inGroup = DATA.sessions.filter(s => s.gid === DATA.groups[0].id)[0];
const ssel = document.getElementById('f-sid');
ssel.value = String(inGroup.id);
ssel.onchange();
check('picking a session pins its assignment too',
      String(F.sid) === String(inGroup.id) && String(F.gid) === String(inGroup.gid));
const other = DATA.groups.find(g => g.id !== DATA.groups[0].id);
gsel.value = String(other.id);
gsel.onchange();
check('changing assignment drops a session that cannot belong to it',
      F.sid === null && String(F.gid) === String(other.id));
document.getElementById('f-from').value = days[2];
document.getElementById('f-from').onchange();
document.getElementById('f-to').value = days[0];
document.getElementById('f-to').onchange();
check('an inverted range is corrected instead of emptied',
      !(F.from && F.to && F.from > F.to), JSON.stringify(F));
document.getElementById('clear').onclick();
check('clear resets every dimension',
      isEmpty(F) && filterRuns(F).length === total);
check('option counts warn before you empty the page',
      document.getElementById('f-drill').innerHTML.includes('('));

// ---- the URL carries the whole combination ----
setFilter({from: days[1], drill: 'letters'});
check('the hash carries every active dimension',
      location.hash.includes('from=') && location.hash.includes('drill=letters'),
      location.hash);
check('the hash round-trips',
      JSON.stringify(filterFromHash()) === JSON.stringify(F));
location.hash = '#group:' + DATA.groups[0].id;
check('a legacy #group link still opens',
      String((filterFromHash() || {}).gid) === String(DATA.groups[0].id));
location.hash = '#day:' + days[0];
const leg = filterFromHash() || {};
check('a legacy #day link becomes a one-day range',
      leg.from === days[0] && leg.to === days[0]);
location.hash = '#all';
check('#all clears everything', isEmpty(filterFromHash() || {from:'x'}));
location.hash = '#gid=99999';
check('an unknown id falls back', filterFromHash() === null);
location.hash = '#nonsense';
check('a malformed fragment falls back', filterFromHash() === null);
location.hash = '';
check('no fragment falls back', filterFromHash() === null);
setFilter({...EMPTY});
dispatchHash('#gid=' + DATA.groups[0].id);
check('hashchange re-filters an open page',
      String(F.gid) === String(DATA.groups[0].id), JSON.stringify(F));

// ---- each dimension on its own still partitions the data ----
check('two drills detected', drills.length === 2, drills.join(','));
const dsum = drills.reduce((n, d) => n + filterRuns({...EMPTY, drill:d}).length, 0);
check('drill filters partition every run', dsum === graded.length);
check('two operators detected', ops.length === 2, ops.join(','));
const osum = ops.reduce((n, o) => n + filterRuns({...EMPTY, op:o}).length, 0);
check('operator filters partition every run', osum === graded.length);
const daysum = days.reduce((n, d) => n + filterRuns({...EMPTY, from:d, to:d}).length, 0);
check('day filters partition every run', daysum === graded.length);

// ---- the threshold applies to the whole filter, not to each day ----
setFilter({...EMPTY});
const combined = trouble(stats(filterRuns(F)));
const perDay = d => stats(filterRuns({...EMPTY, from:d, to:d}));
check('every listed count is the sum over the days in range',
      combined.every(([c, m]) =>
        m.total === days.reduce((n, d) => n + (perDay(d).miss.get(c)?.total || 0), 0)),
      combined.map(([c, m]) => c + ':' + m.total).join(' '));
const union = new Set(days.flatMap(d =>
  [...perDay(d).miss.entries()].filter(([, m]) => m.total >= TH).map(([c]) => c)));
check('a character missed once on each of two days still qualifies',
      combined.some(([c]) => !union.has(c)),
      'combined=' + combined.length + ' union-of-days=' + union.size);
const kids = children(F, filterRuns(F));
check('the day columns sum back to the listed total',
      kids.length === days.length && combined.every(([c, m]) =>
        kids.reduce((n, k) => n + (stats(k.runs).miss.get(c)?.total || 0), 0) === m.total));

// ---- the breakdown follows whatever is pinned ----
setFilter({...EMPTY});
check('a multi-day view breaks down by day',
      children(F, filterRuns(F)).length === days.length);
setFilter({gid: String(DATA.groups[0].id)});
check('an assignment breaks down by session',
      children(F, filterRuns(F)).every(k => String(k.short).startsWith('S')));
setFilter({sid: String(DATA.sessions[0].id)});
check('a session breaks down by run',
      children(F, filterRuns(F)).every(k => String(k.short).startsWith('R')));

// ---- badges appear only where they add something ----
setFilter({...EMPTY});
const wide = document.getElementById('app').innerHTML;
check('run rows carry an operator badge', wide.includes('pill op'));
check('run rows carry a drill badge', wide.includes('pill drill'));
setFilter({drill: drills[0]});
check('the drill badge goes away once pinned',
      !document.getElementById('app').innerHTML.includes('pill drill'));
setFilter({gid: String(DATA.groups[0].id)});
check('assignment context names its operator',
      document.getElementById('app').innerHTML.includes('Operator'));

// ---- every combination renders ----
const combos = [{...EMPTY},
  ...days.map(d => ({...EMPTY, from:d, to:d})),
  ...days.map(d => ({...EMPTY, from:d})),
  ...ops.map(o => ({...EMPTY, op:o})),
  ...drills.map(d => ({...EMPTY, drill:d})),
  ...DATA.groups.map(g => ({...EMPTY, gid:String(g.id)})),
  ...DATA.sessions.map(s => ({...EMPTY, gid:String(s.gid), sid:String(s.id)})),
  {...EMPTY, from:days[1], drill:drills[0]},
  {...EMPTY, from:days[0], to:days[1], op:ops[0]}];
let bad = [];
for (const f of combos){
  try {
    setFilter(f);
    const h = document.getElementById('app').innerHTML;
    if (!h || /undefined|NaN|\[object/.test(h)) throw new Error('placeholder leaked');
  } catch (e){ bad.push(JSON.stringify(f) + ': ' + e.message); }
}
check(combos.length + ' filter combinations render', !bad.length, bad[0]);

// ---- totals must agree with the Python figures handed in ----
setFilter({...EMPTY});
const all = stats(graded);
check('char total matches Python', all.chars === EXPECT.chars, `${all.chars} vs ${EXPECT.chars}`);
check('wrong total matches Python', all.wrong === EXPECT.wrong, `${all.wrong} vs ${EXPECT.wrong}`);
let sc2 = 0, sw = 0;
for (const g of DATA.groups){
  const st = stats(filterRuns({...EMPTY, gid:String(g.id)}));
  sc2 += st.chars; sw += st.wrong;
}
check('per-assignment filters sum to all time', sc2 === all.chars && sw === all.wrong);

// ---- transposed column appears only when there are transpositions ----
const withT = DATA.runs.some(r => r.cells.some(c => c[2].includes('t')));
const h0 = document.getElementById('app').innerHTML;
check('Transposed column shown iff transpositions exist',
      h0.includes('Transposed') === withT, 'hasTransposition=' + withT);
check('Miss rate column labelled', h0.includes('Miss rate'));

// ---- sparkline: hover targets, not a wall of text ----
check('one hover target per run',
      (h0.match(/circle class="hit"/g) || []).length === graded.length);
check('at most two axis labels',
      (h0.match(/<text[^>]*>/g) || []).length <= 2);
check('caption present', h0.includes('id="sparkcap"'));

// ---- bars are graded red->green, not one flat colour ----
const hues = [...h0.matchAll(/hsl\((\d+) 62%/g)].map(m => +m[1]);
check('bars span a range of hues', new Set(hues).size > 2, 'distinct=' + new Set(hues).size);
check('a poor score is red-ish', Math.min(...hues) < 45, 'min=' + Math.min(...hues));
check('a perfect score is green-ish', Math.max(...hues) >= 129, 'max=' + Math.max(...hues));
done();
"""

SMOKE_TESTS = r"""
const combos = [{...EMPTY},
  ...days.map(d => ({...EMPTY, from:d, to:d})),
  ...WINDOWS.map(w => ({...EMPTY, from: days[days.length - w]})),
  ...ops.map(o => ({...EMPTY, op:o})),
  ...drills.map(d => ({...EMPTY, drill:d})),
  ...DATA.groups.map(g => ({...EMPTY, gid:String(g.id)})),
  ...DATA.sessions.map(s => ({...EMPTY, gid:String(s.gid), sid:String(s.id)}))];
if (days.length > 1 && drills.length > 1)
  combos.push({...EMPTY, from: days[days.length - 2], drill: drills[0]});
let bad = [];
for (const f of combos){
  try {
    setFilter(f);
    const h = document.getElementById('app').innerHTML;
    if (!h || /undefined|NaN|\[object/.test(h)) throw new Error('placeholder leaked');
  } catch (e){ bad.push(JSON.stringify(f) + ': ' + e.message); }
}
check('live data: ' + combos.length + ' combinations render', !bad.length, bad[0]);
for (const b of document.getElementById('quick').querySelectorAll('button')){
  const e = QUICK[+b.dataset.i];
  b.onclick();
  check('live data: preset "' + e[0] + '" works',
        !/undefined|NaN/.test(location.hash) && b.getAttribute('aria-pressed') === 'true');
}
done();
"""


def harness(html: str, tests: str, expect: dict | None = None) -> str:
    data = re.search(r'<script id="data"[^>]*>(.*?)</script>', html, re.S).group(1)
    app = re.search(r"<script>(.*?)</script>\s*</body>", html, re.S).group(1)
    return "\n".join([
        SHIM,
        f"document.getElementById('data').textContent = {json.dumps(data)};",
        f"const EXPECT = {json.dumps(expect or {})};",
        app,
        tests,
    ])


def build_fixture(con) -> list:
    """A small group with known properties, including a transposition."""
    me = lcwo.create_operator(con, "Fixture Op", "W0FIX")
    gid = lcwo.create_group(con, assignment="FIXTURE", label="FIXTURE",
                            operator_id=me)
    grp = lcwo.get_group(con, gid)
    key = ["EH", "SM", "TR", "BU", "WM", "QX"]
    # three separate days, so date ranges have something to slice
    day1, day2, day3 = (f"2026-04-0{n}T09:00:00+00:00" for n in (1, 2, 3))
    sess = lcwo.start_session(con, grp, mode="letters", char_wpm=25, eff_wpm=6,
                              started_at=day1)
    for i, attempt in enumerate([["EH", "S.", "T.", ".U", "MW", "QX"],
                                 ["EH", "SM", "TR", "BU", "WM", "QX"]]):
        lcwo.add_run(con, sess["id"], attempt, "fixture", is_final=(i == 1),
                     recorded_at=day1)
    lcwo.finish_session(con, sess["id"], key)
    sess2 = lcwo.start_session(con, grp, mode="letters", char_wpm=25, eff_wpm=6,
                               started_at=day2)
    lcwo.add_run(con, sess2["id"], ["EH", "SM", "TR", "BU", "MW", "QX"], "fixture",
                 is_final=True, recorded_at=day2)
    lcwo.finish_session(con, sess2["id"], key)
    # a second drill in the same assignment, so the drill filter has something
    # to separate
    sess3 = lcwo.start_session(con, grp, mode="custom", char_wpm=28, eff_wpm=8,
                               started_at=day3)
    lcwo.add_run(con, sess3["id"], ["KY", "V.", "JZ"], "fixture", is_final=True,
                 recorded_at=day3)
    lcwo.finish_session(con, sess3["id"], ["KY", "VG", "JZ"])
    # a second operator, so the operator filter has two sides to separate
    them = lcwo.create_operator(con, "Other Op", "K0OTH")
    ogid = lcwo.create_group(con, assignment="OTHER", label="OTHER",
                             operator_id=them)
    osess = lcwo.start_session(con, lcwo.get_group(con, ogid), mode="letters",
                               char_wpm=20, eff_wpm=5, started_at=day3)
    lcwo.add_run(con, osess["id"], ["EH", "S.", "TR"], "fixture", is_final=True,
                 recorded_at=day3)
    lcwo.finish_session(con, osess["id"], ["EH", "SM", "TR"])
    return lcwo.load_all(con)


def run(name: str, js: str, node: str) -> bool:
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(js)
        path = f.name
    print(f"\n── {name} " + "─" * max(0, 60 - len(name)), flush=True)
    ok = subprocess.run([node, path]).returncode == 0
    Path(path).unlink(missing_ok=True)
    return ok


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("node not found - skipping browser tests")
        return 0

    ok = True
    with tempfile.TemporaryDirectory() as td:
        con = lcwo.connect(Path(td) / "fixture.db")
        views = build_fixture(con)
        runs = [r for v in views for r in v.graded_runs]
        expect = {"chars": sum(r.grade.total_chars for r in runs),
                  "wrong": sum(r.grade.wrong_chars for r in runs)}
        html = lcwo.build_report(views, "Fixture", lcwo.list_operators(con))
        con.close()
        ok &= run("fixture", harness(html, FIXTURE_TESTS, expect), node)

    if lcwo.DB_PATH.exists():
        con = lcwo.connect()
        views = lcwo.load_all(con)
        operators = lcwo.list_operators(con)
        con.close()
        if any(v.graded_runs for v in views):
            html = lcwo.build_report(views, "LCWO progress", operators)
            ok &= run("live database", harness(html, SMOKE_TESTS), node)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
