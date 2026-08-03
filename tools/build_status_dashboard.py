#!/usr/bin/env python3
"""Render the gate board as a status page the owner can read without asking anyone.

The complaint this answers is "i dont have a dashboard to know what is going
where", and the earlier answer to it -- the Plane projection -- turned out to
carry task states only. Plane says 56 PROPOSED / 1 FAILED_ORACLE, which is true
and tells you nothing about whether the build is passing. This renders the thing
that actually moves: gate verdicts and assertion coverage.

WHAT IS DELIBERATELY NOT PUBLISHED
----------------------------------
Two fields are dropped from every assertion before anything is written:

* ``expected`` -- the checker's expected value. Publishing it hands a candidate
  the answer key. The central finding of the 2026-08-02/03 session was that any
  checker whose expected value is a constant the candidate knows is forgeable;
  appending one backticked pytest command once flipped 18 of 20 rows FAIL->PASS.
  A dashboard that renders ``expected`` is that hole with a nicer font.
* ``findings`` -- free-text evidence bodies, which quote sources and can quote
  holdout content.

Everything emitted is a count, an identifier, a verdict, or a human-written
claim. The whitelist is explicit (``_GATE_FIELDS`` / ``_ASSERTION_FIELDS``) so
that adding a field to the gate schema does not silently add it to the page.

STALENESS IS RENDERED, NOT HIDDEN
---------------------------------
The page shows the candidate commit the verdicts were computed against and the
repo's current HEAD. When they differ the board says so in the header, because
the failure that produced this script was a persisted summary sitting 14 commits
behind HEAD while reporting PASS=11 FAIL=0 -- a board that is only accurate when
someone remembers to refresh it is the same problem with extra steps.

Usage:
    PYTHONPATH=src python tools/build_status_dashboard.py \
        --summary evidence/gate-run-summary.json \
        --out .data/dashboard
"""

from __future__ import annotations

import argparse
import html
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_GATE_FIELDS = (
    "gate_id",
    "name",
    "day",
    "verdict",
    "executability",
    "assertions_executed",
    "assertions_total",
    "blocking",
    "oracle_type",
    "evidence_tier",
    "model_judge_in_verdict_path",
)

# `expected` and `findings` are excluded on purpose -- see the module docstring.
_ASSERTION_FIELDS = ("id", "claim", "status")

_VERDICT_ORDER = {"FAIL": 0, "UNVERIFIABLE": 1, "PASS": 2}


def _git(*args: str, cwd: Path) -> str:
    try:
        out = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=15
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def build_status(summary: dict[str, Any], repo: Path) -> dict[str, Any]:
    """Reduce a gate-run summary to the publishable subset."""
    body = summary["body"]
    envelope = summary.get("envelope", {})

    gates = []
    for gate in body.get("gates", []):
        row = {k: gate.get(k) for k in _GATE_FIELDS}
        row["assertions"] = [
            {k: a.get(k) for k in _ASSERTION_FIELDS} for a in gate.get("assertions", [])
        ]
        # Counts only: which evidence slots are empty, never what is in them.
        row["evidence_missing_count"] = len(gate.get("evidence_missing") or [])
        row["evidence_refused_count"] = len(gate.get("evidence_refused") or [])
        gates.append(row)

    gates.sort(key=lambda g: (_VERDICT_ORDER.get(g["verdict"], 9), g["gate_id"] or ""))

    head = _git("rev-parse", "--short=8", "HEAD", cwd=repo)
    candidate = (body.get("candidate_commit") or "")[:8]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "contract_id": envelope.get("contract_id"),
        "contract_version": envelope.get("contract_version"),
        "computed_at": envelope.get("created_at"),
        "candidate_commit": candidate,
        "repo_head": head,
        "stale": bool(head and candidate and not head.startswith(candidate)),
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo),
        "counts": body.get("counts", {}),
        "assertions_executed": body.get("assertions_executed"),
        "assertions_total": body.get("assertions_total"),
        "gates_loaded": body.get("gates_loaded"),
        "gates_expected": body.get("gates_expected"),
        "gates": gates,
    }


def _bar(counts: dict[str, int]) -> str:
    total = sum(counts.get(k, 0) for k in ("PASS", "FAIL", "UNVERIFIABLE")) or 1
    segs = []
    for key, cls in (("PASS", "pass"), ("FAIL", "fail"), ("UNVERIFIABLE", "unv")):
        n = counts.get(key, 0)
        if n:
            pct = 100 * n / total
            segs.append(f'<span class="seg {cls}" style="width:{pct:.4f}%"></span>')
    return "".join(segs)


def render_html(status: dict[str, Any]) -> str:
    """A verdict ledger, not a SaaS dashboard.

    The design follows the subject's own vernacular -- this system returns
    verdicts, refuses evidence, and abstains. That is closer to a docket than to
    an analytics page, so: a monospace face for every machine-authored value
    (gate ids, verdicts, counts), a proportional face reserved for the
    human-written gate names, and a severity stripe down the left of each row so
    the one FAIL is findable on a phone without reading a word.
    """
    e = html.escape
    counts = status["counts"]

    stale_note = ""
    if status["stale"]:
        stale_note = (
            f'<p class="warn">Verdicts were computed against '
            f'<code>{e(status["candidate_commit"])}</code> but HEAD is '
            f'<code>{e(status["repo_head"] or "unknown")}</code>. '
            f"This board is behind the repo.</p>"
        )

    rows = []
    for g in status["gates"]:
        v = g["verdict"] or "UNKNOWN"
        cls = {"PASS": "pass", "FAIL": "fail"}.get(v, "unv")
        ex, tot = g["assertions_executed"], g["assertions_total"]
        cov = f"{ex}/{tot}" if tot else "—"
        flags = []
        if g["evidence_missing_count"]:
            flags.append(f'{g["evidence_missing_count"]} evidence missing')
        if g["evidence_refused_count"]:
            flags.append(f'{g["evidence_refused_count"]} evidence refused')
        if g.get("model_judge_in_verdict_path"):
            flags.append("model judge in verdict path")
        rows.append(
            f'<tr class="r-{cls}">'
            f'<td class="id">{e(g["gate_id"] or "")}</td>'
            f'<td class="nm">{e(g["name"] or "")}</td>'
            f'<td><span class="pill {cls}">{e(v)}</span></td>'
            f'<td class="num">{e(cov)}</td>'
            f'<td class="exec">{e((g["executability"] or "").replace("_", " ").lower())}</td>'
            f'<td class="flags">{e(" · ".join(flags))}</td>'
            "</tr>"
        )

    return f"""<title>EFAH gate ledger</title>
<style>
:root {{
  --bg:#f4f6f6; --panel:#ffffff; --fg:#101a1d; --dim:#5d6d71; --line:#dbe3e3;
  --rail:#2f7d8c;
  --pass:#1f6b4f; --fail:#b0342a; --unv:#8a6614;
  --passbg:#e4f1ea; --failbg:#fbe9e7; --unvbg:#f8f0da;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg:#0e1416; --panel:#151d1f; --fg:#e6ecec; --dim:#8b9b9e; --line:#263235;
    --rail:#4fb3c4;
    --pass:#5cc79a; --fail:#ff8f84; --unv:#e2c169;
    --passbg:#10281f; --failbg:#2c1512; --unvbg:#282010;
  }}
}}
:root[data-theme="dark"] {{
  --bg:#0e1416; --panel:#151d1f; --fg:#e6ecec; --dim:#8b9b9e; --line:#263235;
  --rail:#4fb3c4;
  --pass:#5cc79a; --fail:#ff8f84; --unv:#e2c169;
  --passbg:#10281f; --failbg:#2c1512; --unvbg:#282010;
}}
:root[data-theme="light"] {{
  --bg:#f4f6f6; --panel:#ffffff; --fg:#101a1d; --dim:#5d6d71; --line:#dbe3e3;
  --rail:#2f7d8c;
  --pass:#1f6b4f; --fail:#b0342a; --unv:#8a6614;
  --passbg:#e4f1ea; --failbg:#fbe9e7; --unvbg:#f8f0da;
}}
* {{ box-sizing:border-box; }}
body {{
  margin:0; padding:2.5rem 1.25rem 4rem; background:var(--bg); color:var(--fg);
  font:15px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Roboto,Helvetica,sans-serif;
  -webkit-text-size-adjust:100%;
}}
.wrap {{ max-width:62rem; margin:0 auto; display:flex; flex-direction:column; gap:1.5rem; }}
.mono {{ font-family:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace; }}
header {{ display:flex; flex-direction:column; gap:.35rem; border-left:3px solid var(--rail); padding-left:.9rem; }}
.eyebrow {{
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  font-size:.7rem; text-transform:uppercase; letter-spacing:.14em; color:var(--rail);
}}
h1 {{ font-size:1.5rem; margin:0; letter-spacing:-.015em; text-wrap:balance; font-weight:620; }}
.meta {{
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  color:var(--dim); font-size:.78rem; margin:0; word-break:break-word;
}}
.warn {{
  background:var(--failbg); border-left:3px solid var(--fail); color:var(--fg);
  padding:.75rem .95rem; font-size:.86rem; margin:0; border-radius:0 3px 3px 0;
}}
.warn code {{ font-family:ui-monospace,Menlo,monospace; }}
.tiles {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(7.5rem,1fr)); gap:1px; background:var(--line); border:1px solid var(--line); border-radius:4px; overflow:hidden; }}
.tile {{ background:var(--panel); padding:.9rem 1rem; display:flex; flex-direction:column; gap:.15rem; }}
.tile .n {{
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  font-size:1.9rem; font-weight:600; line-height:1; letter-spacing:-.03em;
  font-variant-numeric:tabular-nums;
}}
.tile .l {{ font-size:.68rem; text-transform:uppercase; letter-spacing:.1em; color:var(--dim); }}
.tile .n .of {{ font-size:.9rem; color:var(--dim); font-weight:500; }}
.tile.pass .n {{ color:var(--pass); }}
.tile.fail .n {{ color:var(--fail); }}
.tile.unv .n {{ color:var(--unv); }}
.bar {{ display:flex; height:6px; overflow:hidden; background:var(--line); border-radius:3px; }}
.seg {{ display:block; }}
.seg.pass {{ background:var(--pass); }}
.seg.fail {{ background:var(--fail); }}
.seg.unv {{ background:var(--unv); }}
.scroll {{ overflow-x:auto; -webkit-overflow-scrolling:touch; border:1px solid var(--line); border-radius:4px; background:var(--panel); }}
table {{ border-collapse:collapse; width:100%; font-size:.85rem; }}
th {{
  text-align:left; font-size:.66rem; text-transform:uppercase; letter-spacing:.1em;
  color:var(--dim); font-weight:600; padding:.65rem .8rem;
  border-bottom:1px solid var(--line); white-space:nowrap; background:var(--panel);
}}
td {{ padding:.62rem .8rem; border-bottom:1px solid var(--line); vertical-align:top; }}
tbody tr:last-child td {{ border-bottom:none; }}
tbody tr td:first-child {{ border-left:3px solid transparent; }}
tr.r-fail td:first-child {{ border-left-color:var(--fail); }}
tr.r-unv td:first-child {{ border-left-color:var(--unv); }}
tr.r-pass td:first-child {{ border-left-color:var(--pass); }}
.id {{
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  font-size:.8rem; white-space:nowrap;
}}
.nm {{ min-width:16rem; }}
.num {{
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  font-variant-numeric:tabular-nums; white-space:nowrap; color:var(--dim);
}}
.exec {{ white-space:nowrap; color:var(--dim); font-size:.8rem; }}
.flags {{ color:var(--dim); font-size:.78rem; min-width:10rem; }}
.pill {{
  display:inline-block; padding:.15rem .5rem; border-radius:2px;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  font-size:.68rem; font-weight:600; letter-spacing:.06em; white-space:nowrap;
}}
.pill.pass {{ background:var(--passbg); color:var(--pass); }}
.pill.fail {{ background:var(--failbg); color:var(--fail); }}
.pill.unv {{ background:var(--unvbg); color:var(--unv); }}
footer {{ color:var(--dim); font-size:.76rem; border-top:1px solid var(--line); padding-top:1rem; }}
footer p {{ margin:0 0 .3rem; }}
</style>
<div class="wrap">
  <header>
    <span class="eyebrow">{e(status["contract_id"] or "contract")} · v{e(status["contract_version"] or "")}</span>
    <h1>Gate ledger</h1>
    <p class="meta">branch {e(status["branch"] or "?")} · commit {e(status["candidate_commit"] or "?")} · computed {e((status["computed_at"] or "")[:19].replace("T", " "))}Z</p>
  </header>
  {stale_note}
  <div class="tiles">
    <div class="tile pass"><span class="n">{counts.get("PASS", 0)}</span><span class="l">Pass</span></div>
    <div class="tile fail"><span class="n">{counts.get("FAIL", 0)}</span><span class="l">Fail</span></div>
    <div class="tile unv"><span class="n">{counts.get("UNVERIFIABLE", 0)}</span><span class="l">Unverifiable</span></div>
    <div class="tile"><span class="n">{status["assertions_executed"]}<span class="of">/{status["assertions_total"]}</span></span><span class="l">Assertions</span></div>
  </div>
  <div class="bar">{_bar(counts)}</div>
  <div class="scroll">
    <table>
      <thead><tr><th>Gate</th><th>Name</th><th>Verdict</th><th>Asserts</th><th>Executability</th><th>Notes</th></tr></thead>
      <tbody>{"".join(rows)}</tbody>
    </table>
  </div>
  <footer>
    <p>Sorted by severity: FAIL first, then UNVERIFIABLE, then PASS.</p>
    <p><strong>UNVERIFIABLE is not a soft pass.</strong> It means the checker ran and could not reach a verdict, most often because nothing has been submitted to it.</p>
    <p>Assertion expected-values and evidence bodies are deliberately excluded — a published expected-value is a forgeable checker.</p>
    <p>Generated {e(status["generated_at"])}.</p>
  </footer>
</div>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--summary", default="evidence/gate-run-summary.json")
    ap.add_argument("--out", default=".data/dashboard")
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parent.parent))
    args = ap.parse_args()

    repo = Path(args.repo)
    summary_path = Path(args.summary)
    if not summary_path.is_absolute():
        summary_path = repo / summary_path

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    status = build_status(summary, repo)

    out = Path(args.out)
    if not out.is_absolute():
        out = repo / out
    out.mkdir(parents=True, exist_ok=True)

    (out / "status.json").write_text(
        json.dumps(status, indent=2, sort_keys=True), encoding="utf-8"
    )
    (out / "index.html").write_text(render_html(status), encoding="utf-8")

    c = status["counts"]
    print(
        f"dashboard: PASS={c.get('PASS', 0)} FAIL={c.get('FAIL', 0)} "
        f"UNVERIFIABLE={c.get('UNVERIFIABLE', 0)} "
        f"assertions={status['assertions_executed']}/{status['assertions_total']} "
        f"stale={status['stale']} -> {out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
