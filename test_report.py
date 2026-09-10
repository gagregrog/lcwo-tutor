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
    dataset:{}, _attrs:{},
    get innerHTML(){ return this._html; }, set innerHTML(v){ this._html = v; },
    getAttribute(k){ return this._attrs[k] ?? null; },
    setAttribute(k, v){ this._attrs[k] = v; },
    querySelectorAll(sel){ return parseTags(this._html, sel); },
    querySelector(sel){ return parseTags(this._html, sel)[0] || null; }};
  return els[id];
}
global.document = {getElementById: mk,
  querySelectorAll: sel => mk('quick').querySelectorAll(sel)};
global.location = {hash: ''};
global.history = {replaceState(a, b, h){ global.location.hash = h; }};
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
check('quick buttons rendered', btns.length >= 1, 'got ' + btns.length);
for (const b of btns){
  const entry = QUICK[+b.dataset.i];
  check('button "' + entry[0] + '" carries a scope object',
        entry && entry[1] && typeof entry[1].t === 'string');
  setScope(entry[1]);
  check('button "' + entry[0] + '" sets a real scope',
        sc.t && location.hash !== '#undefined:undefined',
        'hash=' + location.hash);
  const h = document.getElementById('app').innerHTML;
  check('button "' + entry[0] + '" renders', h.length > 200 && !/undefined|NaN/.test(h));
}

// ---- every scope renders ----
const scopes = [{t:'all', k:null}, ...days.map(d => ({t:'day', k:d})),
  ...DATA.groups.map(g => ({t:'group', k:String(g.id)})),
  ...DATA.sessions.map(s => ({t:'session', k:String(s.id)}))];
let bad = [];
for (const s of scopes){
  try {
    setScope(s);
    const h = document.getElementById('app').innerHTML;
    if (!h || /undefined|NaN|\[object/.test(h)) throw new Error('placeholder leaked');
  } catch (e){ bad.push(JSON.stringify(s) + ': ' + e.message); }
}
check(scopes.length + ' scopes render', !bad.length, bad[0]);

// ---- totals must agree with the Python figures handed in ----
setScope({t:'all', k:null});
const all = stats(graded);
check('char total matches Python', all.chars === EXPECT.chars, `${all.chars} vs ${EXPECT.chars}`);
check('wrong total matches Python', all.wrong === EXPECT.wrong, `${all.wrong} vs ${EXPECT.wrong}`);
let sc2 = 0, sw = 0;
for (const g of DATA.groups){
  const st = stats(scopeRuns({t:'group', k:String(g.id)}));
  sc2 += st.chars; sw += st.wrong;
}
check('per-group scopes sum to all-time', sc2 === all.chars && sw === all.wrong);

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
const scopes = [{t:'all', k:null}, ...days.map(d => ({t:'day', k:d})),
  ...DATA.groups.map(g => ({t:'group', k:String(g.id)})),
  ...DATA.sessions.map(s => ({t:'session', k:String(s.id)}))];
let bad = [];
for (const s of scopes){
  try {
    setScope(s);
    const h = document.getElementById('app').innerHTML;
    if (!h || /undefined|NaN|\[object/.test(h)) throw new Error('placeholder leaked');
  } catch (e){ bad.push(JSON.stringify(s) + ': ' + e.message); }
}
check('live data: ' + scopes.length + ' scopes render', !bad.length, bad[0]);
for (const b of document.getElementById('quick').querySelectorAll('button')){
  const e = QUICK[+b.dataset.i];
  setScope(e[1]);
  check('live data: "' + e[0] + '" works', sc.t && location.hash !== '#undefined:undefined');
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
    gid = lcwo.create_group(con, "letters", "fixture", 25, 6, label="Fixture · Letters")
    grp = lcwo.get_group(con, gid)
    key = ["EH", "SM", "TR", "BU", "WM", "QX"]
    sess = lcwo.start_session(con, grp)
    for i, attempt in enumerate([["EH", "S.", "T.", ".U", "MW", "QX"],
                                 ["EH", "SM", "TR", "BU", "WM", "QX"]]):
        lcwo.add_run(con, sess["id"], attempt, "fixture", is_final=(i == 1))
    lcwo.finish_session(con, sess["id"], key)
    sess2 = lcwo.start_session(con, grp)
    lcwo.add_run(con, sess2["id"], ["EH", "SM", "TR", "BU", "MW", "QX"], "fixture",
                 is_final=True)
    lcwo.finish_session(con, sess2["id"], key)
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
        html = lcwo.build_report(views, "Fixture")
        con.close()
        ok &= run("fixture", harness(html, FIXTURE_TESTS, expect), node)

    if lcwo.DB_PATH.exists():
        con = lcwo.connect()
        views = lcwo.load_all(con)
        con.close()
        if any(v.graded_runs for v in views):
            ok &= run("live database", harness(lcwo.build_report(views), SMOKE_TESTS), node)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
