"""Batch review page: a local HTML file with every clip awaiting approval."""
import html
import webbrowser
from pathlib import Path

from . import db
from .config import DATA


def build(open_browser: bool = True) -> Path:
    clips = db.rows("clips", "status IN ('rendered','qa_failed') ORDER BY campaign_id, id")
    cards = []
    for c in clips:
        m, qa = c["meta"] or {}, c["qa"] or {}
        rel = Path(c["file"]).resolve().as_uri()
        issues = "".join(f"<li class=bad>{html.escape(i)}</li>" for i in qa.get("issues", []))
        manual = "".join(f"<li>{html.escape(i)}</li>" for i in qa.get("manual_checks", []))
        audit = qa.get("audit")
        if audit:
            rows = "".join(
                f"<tr class={c['verdict']}><td>{'✓' if c['verdict'] == 'pass' else '✗' if c['verdict'] == 'fail' else '?'}</td>"
                f"<td>{html.escape(c['requirement'])}<div class=ev>{html.escape(c['evidence'])}</div></td></tr>"
                for c in audit.get("checks", []))
            audit_html = (f"<details {'open' if not audit.get('overall_pass') else ''}><summary>"
                          f"Compliance audit: {'PASS' if audit.get('overall_pass') else 'FAIL'} "
                          f"({sum(1 for c in audit.get('checks', []) if c['verdict'] == 'pass')}/"
                          f"{len(audit.get('checks', []))} checks)</summary>"
                          + "".join(f"<p class=bad>{html.escape(b)}</p>" for b in audit.get("blocking_issues", []))
                          + f"<table>{rows}</table>"
                          + "".join(f"<p class=todo>You: {html.escape(t)}</p>" for t in audit.get("human_todo", []))
                          + "</details>")
        else:
            audit_html = "<p class=bad>not audited</p>"
        cards.append(f"""
<article class="{'fail' if c['status']=='qa_failed' else ''}">
  <video src="{rel}" controls preload="metadata"></video>
  <div class=meta>
    <code>{html.escape(c['id'])}</code> <span class=pill>{html.escape(c['status'])}</span>
    <h3>{html.escape(m.get('title',''))}</h3>
    <p><b>Hook:</b> {html.escape(m.get('hook_text',''))} <i>({html.escape(m.get('hook_type',''))}, score {m.get('score','?')}/10)</i></p>
    <p>{html.escape(m.get('description',''))}<br>{html.escape(' '.join(m.get('hashtags',[])))}</p>
    <p class=why>{html.escape(m.get('why_viral',''))}</p>
    <ul>{issues}{manual}</ul>
    {audit_html}
  </div>
</article>""")
    page = f"""<!doctype html><meta charset=utf-8><title>Clip Review</title>
<style>
:root{{--bg:#f6f6f4;--card:#fff;--ink:#1b1b1b;--muted:#666;--bad:#b3261e;--line:#e2e2de}}
@media (prefers-color-scheme:dark){{:root{{--bg:#141414;--card:#1e1e1e;--ink:#eee;--muted:#9a9a9a;--bad:#ff8a80;--line:#2c2c2c}}}}
body{{background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,sans-serif;margin:0;padding:24px 16px}}
h1{{margin:0 0 4px}} .hint{{color:var(--muted);margin:0 0 20px}}
main{{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px}}
article{{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}}
article.fail{{border-color:var(--bad)}}
video{{width:100%;aspect-ratio:9/16;background:#000;display:block}}
.meta{{padding:12px 14px}} h3{{margin:6px 0;font-size:16px}} .why{{color:var(--muted)}}
.pill{{font-size:12px;border:1px solid var(--line);border-radius:99px;padding:1px 8px}} .bad{{color:var(--bad)}}
code{{user-select:all}}
details{{margin-top:8px;font-size:13px}} summary{{cursor:pointer;font-weight:600}}
table{{border-collapse:collapse;width:100%;margin-top:6px}} td{{border-top:1px solid var(--line);padding:4px 6px;vertical-align:top}}
tr.fail td{{color:var(--bad)}} tr.needs_human td{{color:var(--muted)}}
.ev{{color:var(--muted);font-size:12px}} .todo{{color:var(--muted);margin:4px 0}}
</style>
<h1>Clip review ({len(clips)} pending)</h1>
<p class=hint>Approve with <code>python -m clipper approve all</code> or <code>python -m clipper approve ID ID…</code>; reject with <code>python -m clipper reject ID…</code>.</p>
<main>{''.join(cards) or '<p>Nothing to review.</p>'}</main>"""
    out = DATA / "review.html"
    out.write_text(page, encoding="utf-8")
    if open_browser:
        webbrowser.open(out.as_uri())
    return out
