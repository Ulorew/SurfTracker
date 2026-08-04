#!/usr/bin/env python3
"""Универсальные графики обучения (results.csv ultralytics) — сравнение
произвольного числа прогонов в одном self-contained HTML.

    python plot_training_curves.py --run a=path/a/results.csv --run b=path/b/results.csv \
        --title "Дуэль оптимизаторов" --out /tmp/curves.html

До 8 прогонов одновременно (фиксированный порядок палитры — см.
references/palette.md dataviz-скилла; дальше 8 цвета начинают повторяться,
скрипт предупредит и обрежет).

Метрики — те же 10 колонок, что пишет ultralytics в results.csv: train/val
box_loss, cls_loss, dfl_loss, precision, recall, mAP50, mAP50-95.
"""

import argparse
import csv
import json
import os

METRICS = [
    {"key": "train/box_loss", "title": "train / box_loss", "dir": "down"},
    {"key": "train/cls_loss", "title": "train / cls_loss", "dir": "down"},
    {"key": "train/dfl_loss", "title": "train / dfl_loss", "dir": "down"},
    {"key": "val/box_loss", "title": "val / box_loss", "dir": "down"},
    {"key": "val/cls_loss", "title": "val / cls_loss", "dir": "down"},
    {"key": "val/dfl_loss", "title": "val / dfl_loss", "dir": "down"},
    {"key": "metrics/precision(B)", "title": "precision", "dir": "up"},
    {"key": "metrics/recall(B)", "title": "recall", "dir": "up"},
    {"key": "metrics/mAP50(B)", "title": "mAP50", "dir": "up"},
    {"key": "metrics/mAP50-95(B)", "title": "mAP50-95", "dir": "up"},
]

# Категориальная палитра dataviz-скилла (references/palette.md) — фиксированный
# порядок, первые слоты безопасны по CVD на соседних парах.
PALETTE = [
    {"light": "#2a78d6", "dark": "#3987e5"},  # 1 blue
    {"light": "#eb6834", "dark": "#d95926"},  # 2 orange
    {"light": "#1baf7a", "dark": "#199e70"},  # 3 aqua
    {"light": "#eda100", "dark": "#c98500"},  # 4 yellow
    {"light": "#e87ba4", "dark": "#d55181"},  # 5 magenta
    {"light": "#008300", "dark": "#008300"},  # 6 green
    {"light": "#4a3aa7", "dark": "#9085e9"},  # 7 violet
    {"light": "#e34948", "dark": "#e66767"},  # 8 red
]


def load_results_csv(path):
    rows = list(csv.DictReader(open(path)))
    out = {"epoch": [int(r["epoch"]) for r in rows]}
    for m in METRICS:
        out[m["key"]] = [float(r[m["key"]]) if r.get(m["key"]) not in (None, "") else None for r in rows]
    return out


HTML_TEMPLATE = r"""<title>__TITLE__</title>
<style>
  .viz-root {
    color-scheme: light;
    --surface-1: #fcfcfb; --page: #f9f9f7;
    --text-primary: #0b0b0b; --text-secondary: #52514e; --text-muted: #898781;
    --grid: #e1e0d9; --axis: #c3c2b7; --border: rgba(11,11,11,0.10);
__SERIES_VARS_LIGHT__
  }
  @media (prefers-color-scheme: dark) {
    :root:where(:not([data-theme="light"])) .viz-root {
      color-scheme: dark;
      --surface-1: #1a1a19; --page: #0d0d0d;
      --text-primary: #ffffff; --text-secondary: #c3c2b7; --text-muted: #898781;
      --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
__SERIES_VARS_DARK__
    }
  }
  :root[data-theme="dark"] .viz-root {
    color-scheme: dark;
    --surface-1: #1a1a19; --page: #0d0d0d;
    --text-primary: #ffffff; --text-secondary: #c3c2b7; --text-muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
__SERIES_VARS_DARK__
  }

  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  body { background: var(--page); }
  .viz-root {
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    background: var(--page); color: var(--text-primary);
    padding: 24px 20px 40px; max-width: 1180px; margin: 0 auto;
  }
  h1 { font-size: 18px; font-weight: 600; margin: 0 0 2px; }
  .subtitle { color: var(--text-secondary); font-size: 13px; margin: 0 0 16px; }
  .legend {
    display: flex; gap: 20px; align-items: center; flex-wrap: wrap;
    margin: 0 0 20px; padding: 10px 14px; background: var(--surface-1);
    border: 1px solid var(--border); border-radius: 8px; width: fit-content;
  }
  .legend-item { display: flex; align-items: center; gap: 7px; font-size: 13px; color: var(--text-secondary); }
  .legend-swatch { width: 14px; height: 2px; border-radius: 1px; display: inline-block; }
  .legend-note { font-size: 12px; color: var(--text-muted); }

  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 14px; }
  .panel { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; padding: 14px 14px 10px; position: relative; }
  .panel-head { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 6px; }
  .panel-title { font-size: 13px; font-weight: 600; color: var(--text-primary); }
  .panel-hint { font-size: 11px; color: var(--text-muted); }
  svg.chart { width: 100%; height: 150px; display: block; overflow: visible; }
  .gridline { stroke: var(--grid); stroke-width: 1; }
  .axisline { stroke: var(--axis); stroke-width: 1; }
  .tick-label { fill: var(--text-muted); font-size: 9.5px; }
  .end-label { font-size: 9.5px; font-variant-numeric: tabular-nums; }
  .hover-dot { fill: var(--surface-1); stroke-width: 2; r: 4; opacity: 0; pointer-events: none; }
  .crosshair { stroke: var(--axis); stroke-width: 1; opacity: 0; pointer-events: none; }
  .overlay { fill: transparent; cursor: crosshair; }

  .tooltip {
    position: fixed; pointer-events: none; background: var(--surface-1);
    border: 1px solid var(--border); border-radius: 6px; padding: 6px 9px;
    font-size: 11.5px; color: var(--text-primary); box-shadow: 0 2px 10px rgba(0,0,0,0.15);
    opacity: 0; transition: opacity 0.06s; z-index: 10; white-space: nowrap;
  }
  .tooltip .row { display: flex; align-items: center; gap: 6px; }
  .tooltip .sw { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
  .tooltip .epoch { color: var(--text-muted); margin-bottom: 3px; }

  details.tablewrap { margin-top: 22px; }
  summary { cursor: pointer; font-size: 13px; color: var(--text-secondary); padding: 8px 0; user-select: none; }
  .tablescroll { overflow-x: auto; border: 1px solid var(--border); border-radius: 8px; margin-bottom: 12px; }
  table.data { border-collapse: collapse; font-size: 11px; font-variant-numeric: tabular-nums; white-space: nowrap; }
  table.data th, table.data td { padding: 4px 8px; text-align: right; border-bottom: 1px solid var(--grid); }
  table.data th { color: var(--text-muted); font-weight: 600; position: sticky; top: 0; background: var(--surface-1); }
  table.data td:first-child, table.data th:first-child { text-align: left; color: var(--text-secondary); }
  table.data tr:hover td { background: rgba(128,128,128,0.06); }
</style>

<div class="viz-root">
  <h1>__TITLE__</h1>
  <p class="subtitle">__SUBTITLE__</p>
  <div class="legend" id="legend"></div>
  <div class="grid" id="grid"></div>
  <details class="tablewrap">
    <summary>Показать таблицу данных (все эпохи, все прогоны)</summary>
    <div id="tables"></div>
  </details>

  <div class="tooltip" id="tooltip"></div>
</div>

<script>
const RUNS = __RUNS_JSON__;   // [{label, color_light, color_dark, data: {epoch:[...], "train/box_loss":[...], ...}}]
const METRICS = __METRICS_JSON__;

const grid = document.getElementById('grid');
const tooltip = document.getElementById('tooltip');
const legendEl = document.getElementById('legend');
const NS = 'http://www.w3.org/2000/svg';

function fmt(v) { return v === null || v === undefined ? '—' : v.toFixed(3); }
function el(tag, attrs, ns) { const e = document.createElementNS(ns || NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); return e; }

RUNS.forEach((r, i) => {
  const item = document.createElement('span');
  item.className = 'legend-item';
  item.innerHTML = `<span class="legend-swatch" style="background:${r.color_var}"></span>${r.label}`;
  legendEl.appendChild(item);
});
const note = document.createElement('span');
note.className = 'legend-note';
note.textContent = 'наведите на график — точное значение по эпохам';
legendEl.appendChild(note);

function renderPanel(metric) {
  const key = metric.key;
  const n = Math.max(...RUNS.map(r => r.data.epoch.length));

  const panel = document.createElement('div');
  panel.className = 'panel';
  const head = document.createElement('div');
  head.className = 'panel-head';
  const title = document.createElement('div'); title.className = 'panel-title'; title.textContent = metric.title;
  const hint = document.createElement('div'); hint.className = 'panel-hint';
  hint.textContent = metric.dir === 'down' ? 'меньше — лучше' : 'больше — лучше';
  head.appendChild(title); head.appendChild(hint);
  panel.appendChild(head);

  // Пустой svg вставляем в живой DOM ПЕРЕД тем, как рисовать содержимое —
  // иначе неизвестна реальная ширина панели (grid-колонка), и viewBox с
  // preserveAspectRatio='none' на фиксированных 600 растягивает текст
  // непропорционально при любой другой ширине рендера (сжатые подписи).
  const H = 150;
  const svg = el('svg', {class: 'chart'});
  panel.appendChild(svg);
  grid.appendChild(panel);

  const W = svg.getBoundingClientRect().width || 600;  // 1 svg-юнит = 1 css-пиксель, без искажений
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);

  const margin = {top: 8, right: 46, bottom: 16, left: 4};
  const plotW = W - margin.left - margin.right, plotH = H - margin.top - margin.bottom;

  let allVals = [];
  RUNS.forEach(r => allVals = allVals.concat(r.data[key].filter(v => v !== null)));
  let minV = Math.min(...allVals), maxV = Math.max(...allVals);
  const pad = (maxV - minV) * 0.1 || Math.abs(maxV) * 0.05 || 1;
  minV -= pad; maxV += pad;

  const x = i => margin.left + (i / (n - 1)) * plotW;
  const y = v => margin.top + (1 - (v - minV) / (maxV - minV)) * plotH;

  [minV + pad, (minV + maxV) / 2, maxV - pad].forEach(v => {
    const gy = y(v);
    svg.appendChild(el('line', {class: 'gridline', x1: margin.left, x2: W - margin.right, y1: gy, y2: gy}));
    const t = el('text', {class: 'tick-label', x: W - margin.right + 5, y: gy + 3});
    t.textContent = fmt(v);
    svg.appendChild(t);
  });
  svg.appendChild(el('line', {class: 'axisline', x1: margin.left, x2: W - margin.right, y1: H - margin.bottom, y2: H - margin.bottom}));

  const endLabels = [];
  RUNS.forEach(r => {
    const arr = r.data[key];
    const pathD = arr.map((v, i) => v === null ? null : `${i === 0 || arr[i-1] === null ? 'M' : 'L'} ${x(i).toFixed(2)} ${y(v).toFixed(2)}`)
      .filter(Boolean).join(' ');
    const path = el('path', {d: pathD, fill: 'none', stroke: r.color_var, 'stroke-width': 2,
      'stroke-linejoin': 'round', 'stroke-linecap': 'round'});
    svg.appendChild(path);

    let lastIdx = arr.length - 1;
    while (lastIdx >= 0 && arr[lastIdx] === null) lastIdx--;
    if (lastIdx >= 0) {
      const ey = y(arr[lastIdx]);
      svg.appendChild(el('circle', {cx: x(lastIdx), cy: ey, r: 4, fill: r.color_var, stroke: 'var(--surface-1)', 'stroke-width': 2}));
      endLabels.push({y: ey, val: arr[lastIdx], color: r.color_var});
    }
  });
  endLabels.sort((a, b) => a.y - b.y);
  for (let i = 1; i < endLabels.length; i++) {
    if (endLabels[i].y - endLabels[i - 1].y < 10) endLabels[i].y = endLabels[i - 1].y + 10;
  }
  endLabels.forEach(l => {
    const t = el('text', {class: 'end-label', x: W - margin.right + 5, y: l.y + 3, fill: l.color});
    t.textContent = fmt(l.val);
    svg.appendChild(t);
  });

  const crosshair = el('line', {class: 'crosshair', x1: 0, x2: 0, y1: margin.top, y2: H - margin.bottom});
  svg.appendChild(crosshair);
  const hoverDots = RUNS.map(r => { const d = el('circle', {class: 'hover-dot', style: `stroke:${r.color_var}`}); svg.appendChild(d); return d; });

  const overlay = el('rect', {class: 'overlay', x: 0, y: 0, width: W, height: H});
  svg.appendChild(overlay);

  overlay.addEventListener('mousemove', (evt) => {
    const rect = svg.getBoundingClientRect();
    const relX = (evt.clientX - rect.left) / rect.width * W;
    let i = Math.round(((relX - margin.left) / plotW) * (n - 1));
    i = Math.max(0, Math.min(n - 1, i));
    const cx = x(i);
    crosshair.setAttribute('x1', cx); crosshair.setAttribute('x2', cx); crosshair.style.opacity = 1;

    let rows = `<div class="epoch">эпоха ${i + 1}</div>`;
    RUNS.forEach((r, ri) => {
      const v = r.data[key][i];
      const dot = hoverDots[ri];
      if (v === null || v === undefined) { dot.style.opacity = 0; }
      else { dot.setAttribute('cx', cx); dot.setAttribute('cy', y(v)); dot.style.opacity = 1; }
      rows += `<div class="row"><span class="sw" style="background:${r.color_var}"></span>${r.label}: ${fmt(v)}</div>`;
    });
    tooltip.innerHTML = rows;
    tooltip.style.opacity = 1;
    tooltip.style.left = (evt.clientX + 14) + 'px';
    tooltip.style.top = (evt.clientY + 14) + 'px';
  });
  overlay.addEventListener('mouseleave', () => {
    crosshair.style.opacity = 0; hoverDots.forEach(d => d.style.opacity = 0); tooltip.style.opacity = 0;
  });

}

METRICS.forEach(m => renderPanel(m));

function buildTable(run) {
  const wrap = document.createElement('div'); wrap.className = 'tablescroll';
  const cap = document.createElement('div');
  cap.style.cssText = 'font-size:12px;color:var(--text-secondary);padding:8px 10px 4px;';
  cap.textContent = run.label;
  const table = document.createElement('table'); table.className = 'data';
  const thead = document.createElement('tr');
  thead.innerHTML = '<th>epoch</th>' + METRICS.map(m => `<th>${m.title}</th>`).join('');
  table.appendChild(thead);
  run.data.epoch.forEach((e, i) => {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${e}</td>` + METRICS.map(m => `<td>${fmt(run.data[m.key][i])}</td>`).join('');
    table.appendChild(tr);
  });
  const container = document.createElement('div');
  container.appendChild(cap); wrap.appendChild(table); container.appendChild(wrap);
  return container;
}
const tablesDiv = document.getElementById('tables');
RUNS.forEach(r => tablesDiv.appendChild(buildTable(r)));
</script>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="append", required=True, metavar="LABEL::PATH",
                     help="повторяемый флаг: метка::путь к results.csv (:: — метки часто "
                          "содержат '=', напр. 'lr0=0.01'). До 8 штук.")
    ap.add_argument("--title", default="Кривые обучения")
    ap.add_argument("--subtitle", default="")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    if len(args.run) > len(PALETTE):
        print(f"WARNING: {len(args.run)} прогонов > {len(PALETTE)} цветов палитры — цвета начнут повторяться")

    runs = []
    for i, spec in enumerate(args.run):
        label, path = spec.split("::", 1)
        data = load_results_csv(path)
        color = PALETTE[i % len(PALETTE)]
        runs.append({"label": label, "data": data, "color_var": f"var(--series-{i+1})",
                      "_light": color["light"], "_dark": color["dark"]})

    series_vars_light = "\n".join(f'    --series-{i+1}: {r["_light"]};' for i, r in enumerate(runs))
    series_vars_dark = "\n".join(f'    --series-{i+1}: {r["_dark"]};' for i, r in enumerate(runs))

    runs_json = json.dumps([{"label": r["label"], "color_var": r["color_var"], "data": r["data"]} for r in runs],
                            separators=(",", ":"))
    metrics_json = json.dumps(METRICS, ensure_ascii=False)

    html = (HTML_TEMPLATE
            .replace("__TITLE__", args.title)
            .replace("__SUBTITLE__", args.subtitle)
            .replace("__SERIES_VARS_LIGHT__", series_vars_light)
            .replace("__SERIES_VARS_DARK__", series_vars_dark)
            .replace("__RUNS_JSON__", runs_json)
            .replace("__METRICS_JSON__", metrics_json))

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as f:
        f.write(html)
    print(f"written {args.out} ({len(runs)} runs, {len(html)} bytes)")


if __name__ == "__main__":
    main()
