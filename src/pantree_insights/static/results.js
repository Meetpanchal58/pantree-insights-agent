/* results: explicit ES module dependencies; no build step. */
import { $, S, esc } from './state.js';
import { autosize, cssVar, download, fileName, fmtNum, toast } from './render.js';
import { loadScript } from './api.js';

export function showResult(run, selected = 0) {
  const whole = run;
  if (run.analyses?.length) run = { ...run, ...run.analyses[selected] };
  const box = $("#result"); if (!box) return;
  S.run.final = run;
  if (run.status !== "done" && !whole.analyses?.length) { box.innerHTML = `<div class="result"><h3>${esc(run.status)}</h3><p>${esc(run.message || "")}</p></div>`; return; }
  const a = run.analysis || {};
  const list = (title, items) => items && items.length ? `<h4>${title}</h4><ul>${items.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : "";
  const res = run.result || { columns: [], data: [] };
  box.innerHTML = `<div class="result">
    ${whole.analyses?.length > 1 ? `<nav aria-label="Approved analyses">${whole.analyses.map((item, i) => `<button class="btn small" data-analysis="${i}" aria-pressed="${i === selected}">${i + 1}. ${esc(item.purpose)}</button>`).join("")}</nav>` : ""}
    <h3>${esc(a.headline)}</h3>
    <div id="chart-area"></div>
    <div class="exports">
      <button class="btn small" data-action="export" data-kind="copy">⧉ Copy</button>
      <button class="btn small" data-action="export" data-kind="png" ${run.chart && (run.chart.figure || run.chart.kind === "offline_bar") ? "" : "disabled"}>▣ PNG</button>
      <button class="btn small" data-action="export" data-kind="csv">⇩ CSV</button>
      ${run.offline ? "" : '<button class="btn small" data-action="export" data-kind="ppt">▤ PPT</button>'}
      <button class="btn small" data-action="export" data-kind="sql">{ } SQL</button>
    </div>
    ${list("Findings", a.findings)}${list("Hypotheses (unproven)", a.hypotheses)}${list("Recommendations", a.recommendations)}${list("Caveats", a.caveats)}
    ${(run.warnings || []).map((w) => `<div class="warning-box">⚠ ${esc(w)}</div>`).join("")}
    ${whole.status !== "done" ? `<div class="warning-box">${esc(whole.message || "Investigation stopped. Completed evidence is shown below.")}</div>` : ""}
    ${whole.context_sources?.length ? `<details><summary>Earlier analyses retrieved as context</summary><ul>${whole.context_sources.map(s => `<li>${esc(s.question)} · ${esc(s.run_id)}${s.stale ? " · older dataset; recomputation required" : ""}</li>`).join("")}</ul></details>` : ""}
    ${whole.investigation_stop ? `<div class="warning-box">${esc(whole.investigation_stop)}</div>` : ""}
    ${list("Follow-up questions", a.follow_up_questions)}
    <h4>Result (${res.data.length} rows${res.truncated ? ", truncated" : ""})</h4>
    <div class="table-wrap"><table><thead><tr>${res.columns.map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead>
      <tbody>${res.data.slice(0, 200).map((r) => `<tr>${r.map((v, i) => `<td class="${typeof v === "number" ? "num" : ""}">${fmtNum(v, res.columns[i])}</td>`).join("")}</tr>`).join("")}</tbody></table></div>
    <div class="meta">Data as of ${esc(run.data_as_of)} (${esc(run.data_size)} shop) · ${run.trust === "verified" ? "verified query" : `${run.attempts || 1} SQL attempt(s)`}
      ${run.cost ? ` · ${run.cost.calls} LLM calls · ${run.cost.input + run.cost.output} tokens · $${(run.cost.usd || 0).toFixed(4)}${run.cost.unknown_cost_calls ? " estimated / held · some cost unknown" : " estimated"}` : ""}</div>
  </div>`;
  box.querySelectorAll("[data-analysis]").forEach((b) => b.addEventListener("click", () => showResult(whole, Number(b.dataset.analysis))));
  box.querySelectorAll("[data-action='export']").forEach((b) => b.addEventListener("click", () => exportRun(b.dataset.kind, run)));
  const followUps = [...box.querySelectorAll("h4")].find((h) => h.textContent === "Follow-up questions");
  if (followUps && S.me.user && !run.replay) followUps.nextElementSibling.querySelectorAll("li").forEach((li) => {
    li.style.cursor = "pointer"; li.title = "Ask this";
    li.addEventListener("click", () => { $("#question").value = li.textContent; $("#question").focus(); });
  });
  S.chartReady = drawChart(run.chart);
  if (!S.run.restoring) requestAnimationFrame(() => box.scrollIntoView({ behavior: "smooth", block: "start" }));
}

export async function drawChart(chart) {
  const area = $("#chart-area"); if (!area || !chart) return;
  if (chart.kind === "offline_bar") {
    const maximum = Math.max(1, ...chart.values.filter((value) => typeof value === "number"));
    const height = chart.labels.length * 58 + 16;
    area.innerHTML = `<svg id="offline-chart" role="img" aria-label="${esc(chart.title)}" viewBox="0 0 640 ${height}" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto">
      <title>${esc(chart.title)}</title><rect width="640" height="${height}" fill="white"/>
      ${chart.labels.map((label, i) => `<g transform="translate(12,${i * 58 + 12})">
        <text x="0" y="15" font-family="sans-serif" font-size="13" fill="#161827">${esc(label)}</text>
        <text x="616" y="15" text-anchor="end" font-family="sans-serif" font-size="13" fill="#161827">${fmtNum(chart.values[i], chart.metric)}</text>
        <rect x="0" y="24" width="616" height="14" rx="3" fill="#eeeefa"/>
        <rect x="0" y="24" width="${Math.max(0, (chart.values[i] || 0) / maximum * 616)}" height="14" rx="3" fill="#6557dc"/>
      </g>`).join("")}</svg>`;
    return;
  }
  if (chart.kind === "kpi") {
    area.innerHTML = `<div class="kpis">${chart.values.map((v) => `<div class="kpi"><div class="v">${fmtNum(v.value, v.label.toLowerCase().replace(/ /g, "_"))}</div><div class="l">${esc(v.label)}</div></div>`).join("")}</div>`;
    return;
  }
  area.innerHTML = `<div class="chart" id="chart"></div>`;
  const fig = chart.figure;
  const layout = Object.assign({}, fig.layout, {
    paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)", autosize: true, height: Math.min((fig.layout && fig.layout.height) || 380, 900),
    font: { family: "Inter, system-ui, sans-serif", color: cssVar("--text"), size: 12 },
  });
  ["xaxis", "yaxis"].forEach((ax) => { layout[ax] = Object.assign({}, layout[ax], { gridcolor: cssVar("--border"), zerolinecolor: cssVar("--border") }); });
  const target = area.querySelector("#chart");
  target.textContent = "Loading chart…";
  try {
    await loadScript("https://cdn.jsdelivr.net/npm/plotly.js-dist-min@2.35.2/plotly.min.js", "Plotly");
    if (!target.isConnected || target !== $("#chart")) return;
    target.textContent = "";
    await window.Plotly.newPlot(target, fig.data, layout, { displaylogo: false, responsive: true, modeBarButtonsToRemove: ["lasso2d", "select2d"] });
  } catch (e) { if (target.isConnected) target.textContent = "Chart unavailable. Reopen the analysis to retry."; }
}

export function markdown(run) {
  const a = run.analysis || {};
  const sec = (t, xs) => (xs && xs.length ? `\n**${t}**\n${xs.map((x) => `- ${x}`).join("\n")}\n` : "");
  return `## ${a.headline}\n${sec("Findings", a.findings)}${sec("Recommendations", a.recommendations)}${sec("Caveats", a.caveats)}` +
    `\n_Pantree Insights · data as of ${run.data_as_of} (${run.data_size} shop, simulated)_\n`;
}

export function csv(run) {
  const q = (v) => (v === null || v === undefined ? "" : /[",\n]/.test(String(v)) ? `"${String(v).replace(/"/g, '""')}"` : String(v));
  return [run.result.columns.map(q).join(","), ...run.result.data.map((r) => r.map(q).join(","))].join("\n");
}

export async function chartPng(width = 1600, height = 900) {
  await S.chartReady;
  const svg = document.getElementById("offline-chart");
  if (svg) {
    const source = new XMLSerializer().serializeToString(svg);
    const img = new Image(); img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(source);
    await img.decode();
    const canvas = document.createElement("canvas"); canvas.width = width; canvas.height = height;
    const ctx = canvas.getContext("2d"); ctx.fillStyle = "white"; ctx.fillRect(0, 0, width, height);
    const scale = Math.min(width / img.width, height / img.height);
    ctx.drawImage(img, (width - img.width * scale) / 2, 0, img.width * scale, img.height * scale);
    return canvas.toDataURL("image/png");
  }
  const el = document.getElementById("chart");
  if (!el || !window.Plotly) return null;
  return window.Plotly.toImage(el, { format: "png", width, height,
    layout: { paper_bgcolor: "#ffffff", plot_bgcolor: "#ffffff", font: { color: "#161827" } } });
}

export async function exportRun(kind, run) {
  const name = fileName(run.question || (run.analysis || {}).headline);
  try {
    if (kind === "copy") { await navigator.clipboard.writeText(markdown(run)); toast("Copied as Markdown"); }
    else if (kind === "sql") { await navigator.clipboard.writeText(run.sql || ""); toast("SQL copied"); }
    else if (kind === "csv") download(`${name}.csv`, new Blob([csv(run)], { type: "text/csv" }));
    else if (kind === "png") { const url = await chartPng(); if (url) download(`${name}.png`, await (await fetch(url)).blob()); }
    else if (kind === "ppt") await exportPpt(run, name);
  } catch (e) { toast("Export failed: " + e.message); }
}

export async function exportPpt(run, name) {
  await loadScript("https://cdn.jsdelivr.net/npm/pptxgenjs@3.12.0/dist/pptxgen.bundle.js", "PptxGenJS");
  const a = run.analysis || {};
  const pptx = new window.PptxGenJS(); pptx.layout = "LAYOUT_WIDE";
  const accent = "4F46E5", grey = "6B6F85";
  const title = pptx.addSlide();
  title.addText(run.question || a.headline, { x: 0.6, y: 2.2, w: 12, h: 1.4, fontSize: 32, bold: true, color: "161827" });
  title.addText(a.headline, { x: 0.6, y: 3.7, w: 12, h: 1, fontSize: 18, color: accent });
  title.addText(`Pantree Insights · data as of ${run.data_as_of} (${run.data_size} shop, simulated)`, { x: 0.6, y: 6.6, w: 12, h: 0.4, fontSize: 11, color: grey });
  const png = await chartPng(1600, 900);
  const chartSlide = pptx.addSlide();
  chartSlide.addText(a.chart_title || a.headline, { x: 0.5, y: 0.3, w: 12.3, h: 0.6, fontSize: 20, bold: true, color: "161827" });
  if (png) chartSlide.addImage({ data: png, x: 0.9, y: 1.1, w: 11.5, h: 6.47 });
  else if (run.chart && run.chart.kind === "kpi") chartSlide.addText(run.chart.values.map((v) => `${v.label}: ${fmtNum(v.value, v.label.toLowerCase())}`).join("\n"), { x: 1, y: 2, w: 11, h: 3, fontSize: 28, color: accent });
  const bullets = (xs) => (xs || []).map((t) => ({ text: t, options: { bullet: true, breakLine: true } }));
  const s3 = pptx.addSlide();
  s3.addText("Key findings", { x: 0.5, y: 0.3, w: 12, h: 0.6, fontSize: 20, bold: true, color: "161827" });
  s3.addText(bullets(a.findings), { x: 0.6, y: 1.1, w: 12, h: 3.4, fontSize: 15, color: "161827", valign: "top" });
  if (a.recommendations && a.recommendations.length) {
    s3.addText("Recommendations", { x: 0.5, y: 4.6, w: 12, h: 0.5, fontSize: 16, bold: true, color: accent });
    s3.addText(bullets(a.recommendations), { x: 0.6, y: 5.1, w: 12, h: 2, fontSize: 14, color: "161827", valign: "top" });
  }
  const s4 = pptx.addSlide();
  s4.addText("How this was calculated", { x: 0.5, y: 0.3, w: 12, h: 0.6, fontSize: 20, bold: true, color: "161827" });
  s4.addText(run.sql || "", { x: 0.5, y: 1.0, w: 12.3, h: 4.6, fontSize: 9, fontFace: "Courier New", color: "161827", fill: { color: "F1F2F7" }, valign: "top" });
  s4.addText(bullets(a.caveats), { x: 0.5, y: 5.8, w: 12.3, h: 1.4, fontSize: 11, color: grey, valign: "top" });
  await pptx.writeFile({ fileName: `${name}.pptx` });
}
