"""Generate a single-page HTML physics budget dashboard."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def build_dashboard_html(
    physics: dict[str, Any],
    *,
    title: str = "Physics budget",
    board_meta: dict[str, Any] | None = None,
    routes: dict[str, Any] | None = None,
    comparison: dict[str, Any] | None = None,
    viewer_url: str = "viewer/index.html",
) -> str:
    score = physics.get("score") or physics
    notes = physics.get("notes") or []
    board_meta = board_meta or {}
    routes = routes or {}

    # Chart.js via CDN
    labels = [k for k in score if k != "total"]
    values = [score.get(k, 0) for k in labels]
    total = score.get("total", sum(values))

    route_rows = ""
    for name, r in routes.items():
        if not isinstance(r, dict):
            continue
        route_rows += (
            f"<tr><td>{name}</td><td>{r.get('total_length_mm', '—')}</td>"
            f"<td>{r.get('via_count', '—')}</td>"
            f"<td>{r.get('clearance_violations', '—')}</td>"
            f"<td>{len(r.get('unrouted_nets') or [])}</td></tr>"
        )

    notes_html = "".join(f"<li>{n}</li>" for n in notes)
    meta_html = "".join(f"<li><strong>{k}:</strong> {v}</li>" for k, v in board_meta.items())

    cmp_html = ""
    if comparison:
        cmp_html = f"<pre class='cmp'>{json.dumps(comparison, indent=2)}</pre>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{title} — physicsRouter</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
  <style>
    :root {{
      --bg: #0f1419; --card: #1a2332; --text: #e7ecf3; --muted: #8b9bb4;
      --accent: #5b9fd4; --good: #3ecf8e; --warn: #f0b429;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; font-family: "IBM Plex Sans", system-ui, sans-serif;
      background: var(--bg); color: var(--text); line-height: 1.45;
    }}
    header {{
      padding: 1.25rem 2rem; border-bottom: 1px solid #2a3548;
      display: flex; flex-wrap: wrap; gap: 1rem; align-items: baseline;
      justify-content: space-between;
    }}
    header h1 {{ margin: 0; font-size: 1.35rem; font-weight: 600; }}
    header a {{ color: var(--accent); text-decoration: none; }}
    main {{
      display: grid; gap: 1rem; padding: 1rem 2rem 2rem;
      grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
    }}
    .card {{
      background: var(--card); border-radius: 12px; padding: 1rem 1.25rem;
      border: 1px solid #2a3548;
    }}
    .card h2 {{ margin: 0 0 0.75rem; font-size: 0.95rem; color: var(--muted);
      text-transform: uppercase; letter-spacing: 0.04em; }}
    .total {{ font-size: 2rem; font-weight: 700; color: var(--good); }}
    .muted {{ color: var(--muted); font-size: 0.9rem; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; }}
    th, td {{ text-align: left; padding: 0.35rem 0.25rem; border-bottom: 1px solid #2a3548; }}
    th {{ color: var(--muted); font-weight: 500; }}
    ul {{ margin: 0; padding-left: 1.1rem; }}
    pre.cmp {{ overflow: auto; font-size: 0.75rem; color: var(--muted); max-height: 240px; }}
    canvas {{ max-height: 280px; }}
    .wide {{ grid-column: 1 / -1; }}
  </style>
</head>
<body>
  <header>
    <h1>{title}</h1>
    <div>
      <a href="{viewer_url}">Open interactive viewer →</a>
    </div>
  </header>
  <main>
    <section class="card">
      <h2>Total physics cost</h2>
      <div class="total">{total:.2f}</div>
      <p class="muted">Lower is better (weighted multi-objective)</p>
    </section>
    <section class="card">
      <h2>Board</h2>
      <ul class="muted">{meta_html or "<li>n/a</li>"}</ul>
    </section>
    <section class="card wide">
      <h2>Score breakdown</h2>
      <canvas id="scoreChart"></canvas>
    </section>
    <section class="card">
      <h2>IR / EMI / loop notes</h2>
      <ul>{notes_html or "<li>No notes</li>"}</ul>
    </section>
    <section class="card">
      <h2>Route variants</h2>
      <table>
        <thead><tr><th>Name</th><th>Length mm</th><th>Vias</th><th>Viol.</th><th>Unrouted</th></tr></thead>
        <tbody>{route_rows or "<tr><td colspan=5 class=muted>No routes in payload</td></tr>"}</tbody>
      </table>
    </section>
    <section class="card wide">
      <h2>TopoR vs FreeRouting</h2>
      {cmp_html or "<p class='muted'>Run <code>physics-router compare-routes</code> to populate.</p>"}
    </section>
  </main>
  <script>
    const labels = {json.dumps(labels)};
    const values = {json.dumps(values)};
    new Chart(document.getElementById('scoreChart'), {{
      type: 'bar',
      data: {{
        labels,
        datasets: [{{
          label: 'Cost',
          data: values,
          backgroundColor: 'rgba(91, 159, 212, 0.75)',
          borderRadius: 4,
        }}]
      }},
      options: {{
        indexAxis: 'y',
        plugins: {{ legend: {{ display: false }} }},
        scales: {{
          x: {{ ticks: {{ color: '#8b9bb4' }}, grid: {{ color: '#2a3548' }} }},
          y: {{ ticks: {{ color: '#e7ecf3' }}, grid: {{ display: false }} }}
        }}
      }}
    }});
  </script>
</body>
</html>
"""


def write_dashboard(
    out_html: str | Path,
    physics: dict[str, Any],
    **kwargs: Any,
) -> Path:
    out_html = Path(out_html)
    out_html.parent.mkdir(parents=True, exist_ok=True)
    out_html.write_text(build_dashboard_html(physics, **kwargs), encoding="utf-8")
    return out_html
