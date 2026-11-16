"use strict";
/* Pantree Insights front end: no framework, no build step. */

const STEP_LABELS = {
  check_input: "Checking the question", retrieve: "Finding relevant data", understand: "Understanding the question",
  plan: "Planning the analysis",
  write_sql: "Writing SQL", check_sql: "Safety checks", run_query: "Running the query",
  validate: "Reviewing the query", analyse: "Analysing the result", chart: "Building the chart",
  remember: "Saving to memory",
};
const STATUS_ICON = { done: "✓", retry: "↻", failed: "✕", blocked: "✕", warning: "!", needs_input: "?", running: "" };
const EXAMPLES = [
  ["Sales", "How did Diwali 2026 do compared with Diwali 2025?"],
  ["Returns", "Why are footwear returns so high?"],
  ["Delivery", "Is cash on delivery causing more RTO in tier 3 cities?"],
  ["Marketing", "Which marketing channels give the best return on ad spend?"],
  ["Customers", "What share of buyers order again within 90 days?"],
  ["Stock", "Which product sizes lose the most sales to stock-outs?"],
];

const S = { me: null, projects: [], chats: [], chatId: null, expanded: new Set(), demoMode: false,
            run: null, busy: false, pendingProject: null };
const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* ---------------------------------------------------------------- helpers */
async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {}, credentials: "same-origin" };
  if (opts.body !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(opts.body); }
  const res = await fetch(path, init);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `Request failed (${res.status})`);
  return data;
}
function toast(msg, ms = 2600) {
  const t = $("#toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toast.timer); toast.timer = setTimeout(() => (t.hidden = true), ms);
}
function fmtNum(v, col = "") {
  if (v === null || v === undefined || v === "") return "";
  if (typeof v !== "number") return esc(v);
  if (/rate|share|pct|ctr|conversion|margin|lift|growth/.test(col) && Math.abs(v) <= 1.5) return (v * 100).toFixed(1) + "%";
  if (/revenue|sales|spend|aov|amount|cost|cac|value|price/.test(col)) return "₹" + v.toLocaleString("en-IN", { maximumFractionDigits: 0 });
  return v.toLocaleString("en-IN", { maximumFractionDigits: Math.abs(v) < 10 ? 3 : 1 });
}
function download(name, blob) {
  const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}
const fileName = (q) => (q || "pantree-insight").toLowerCase().replace(/[^a-z0-9]+/g, "-").slice(0, 60);
function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
function scrollDown() { const m = $("#messages"); m.scrollTop = m.scrollHeight; }
function openPanel() { $("#app").classList.remove("panel-closed"); }

/* ---------------------------------------------------------------- start */
async function init() {
  if (window.innerWidth <= 1180) $("#app").classList.add("panel-closed");
  try { S.me = await api("/api/me"); } catch (e) { toast(e.message); return; }
  if (new URLSearchParams(location.search).get("signin") === "failed") toast("Sign-in did not complete. Please try again.");
  if (!S.me.user) { showSignin(); renderWelcome(); renderUser(); return; }
  $("#signin").hidden = true;
  renderUser();
  await refreshSidebar();
  renderWelcome();
  loadPulseCount();
}

function showSignin() {
  const m = S.me.signin;
  $("#google-btn").hidden = !m.google;
  $("#email-form").hidden = !m.email;
  $("#or-divider").hidden = !(m.google && m.email);
  $("#signin").hidden = false;
}

function renderUser() {
  const box = $("#user-box");
  const u = S.me && S.me.user;
  if (!u) {
    box.innerHTML = `<button class="btn small" data-action="signin">Sign in</button><span class="user-meta">Demo mode</span>`;
    $("#limit-info").textContent = "Sign in to ask your own questions";
    return;
  }
  const initial = esc((u.name || u.email)[0].toUpperCase());
  box.innerHTML = `<div class="avatar">${u.picture ? `<img src="${esc(u.picture)}" alt="">` : initial}</div>
    <div class="user-info"><div class="user-name">${esc(u.name || u.email)}</div>
    <div class="user-meta">${u.is_admin ? "Admin" : esc(u.email)}</div></div>
    <button class="icon-btn" data-action="theme" title="Light / dark">◐</button>
    <button class="icon-btn" data-action="logout" title="Sign out">⎋</button>`;
  const l = S.me.limit;
  $("#limit-info").textContent = l.unlimited ? "Admin: unlimited questions" : `${l.used}/${l.per_day} live questions today`;
}

/* ---------------------------------------------------------------- sidebar */
async function refreshSidebar() {
  if (!S.me.user) return;
  [S.projects, S.chats] = await Promise.all([api("/api/projects"), api("/api/chats")]);
  const chatItem = (c) => `<button class="nav-item ${c.id === S.chatId ? "active" : ""}" data-action="open-chat" data-id="${c.id}">
      <span class="label">${esc(c.title)}</span><span class="more" data-action="chat-menu" data-id="${c.id}" title="Options">⋯</span></button>`;
  $("#project-list").innerHTML = S.projects.map((p) => {
    const inside = S.chats.filter((c) => c.project_id === p.id);
    const open = S.expanded.has(p.id);
    return `<button class="nav-item" data-action="toggle-project" data-id="${p.id}"><span class="ico">${open ? "▾" : "▸"}</span>
        <span class="label">📁 ${esc(p.name)}</span><span class="more" data-action="edit-project" data-id="${p.id}" title="Project settings">⋯</span></button>
      <div class="project-chats" ${open ? "" : "hidden"}>
        <button class="nav-item muted" data-action="new-chat" data-project="${p.id}">＋ New chat in project</button>
        ${inside.map(chatItem).join("")}</div>`;
  }).join("") || `<div class="empty-hint">Group chats by topic, with their own instructions and memory.</div>`;
  const loose = S.chats.filter((c) => !c.project_id);
  $("#chat-list").innerHTML = loose.map(chatItem).join("") || `<div class="empty-hint">No chats yet.</div>`;
  const sel = $("#move-project");
  const chat = S.chats.find((c) => c.id === S.chatId);
  sel.hidden = !chat;
  sel.innerHTML = `<option value="">No project</option>` + S.projects.map((p) => `<option value="${p.id}">📁 ${esc(p.name)}</option>`).join("");
  if (chat) sel.value = chat.project_id || "";
}

/* ---------------------------------------------------------------- welcome and chats */
async function renderWelcome() {
  S.chatId = null; S.run = null;
  $("#chat-title").textContent = S.pendingProject ? `New chat in ${S.projects.find((p) => p.id === S.pendingProject)?.name || "project"}` : "New chat";
  $("#move-project").hidden = true;
  let demos = [];
  try { demos = await api("/api/demos"); } catch { /* none */ }
  const d = S.me && S.me.data;
  $("#messages").innerHTML = `<div class="welcome">
      <h2>What would you like to know?</h2>
      <p class="muted">Ask about Pantree's sales, returns, delivery, marketing, stock or customers.
      Data: ${d ? `${esc(d.first_day)} to ${esc(d.last_day)} (${esc(d.size)} shop, simulated)` : ""}.</p>
      ${demos.length ? `<div class="section-label">Watch a recorded run</div><div class="cards">${demos.map((x) => `
        <button class="qcard" data-action="replay" data-slug="${esc(x.slug)}"><div class="kind">▶ Demo · ${esc(x.seconds)}s</div>${esc(x.question)}</button>`).join("")}</div>` : ""}
      ${S.me && S.me.user ? `<div class="section-label">Or try</div><div class="cards">${EXAMPLES.map(([k, q]) => `
        <button class="qcard" data-action="example" data-q="${esc(q)}"><div class="kind">${esc(k)}</div>${esc(q)}</button>`).join("")}</div>` : ""}
    </div>`;
  refreshSidebar();
}

async function openChat(id) {
  S.chatId = id; S.pendingProject = null;
  closeSidebarMobile();
  const chat = await api(`/api/chats/${id}`);
  $("#chat-title").textContent = chat.title;
  const box = $("#messages"); box.innerHTML = "";
  let last = null;
  for (const m of chat.messages) {
    if (m.role === "user") box.insertAdjacentHTML("beforeend", userBubble(m.content));
    else { box.appendChild(answerCard(m.run || {})); last = m.run; }
  }
  if (chat.pending_run) { const el = statusLine("Working on it…"); box.appendChild(el); streamRun(chat.pending_run, 0, el); }
  else if (last) showRunInPanel(last);
  refreshSidebar(); scrollDown();
}

const userBubble = (text) => `<div class="msg user"><div class="bubble">${esc(text)}</div></div>`;
function statusLine(text) {
  const el = document.createElement("div"); el.className = "msg";
  el.innerHTML = `<div class="answer status-line"><span class="spinner"></span><span class="txt">${esc(text)}</span></div>`;
  return el;
}

function answerCard(run, replay = false) {
  const el = document.createElement("div"); el.className = "msg";
  if (run.status !== "done") {
    el.innerHTML = `<div class="answer error-box"><h3>${run.status === "declined" ? "I can't answer that" : "Something went wrong"}</h3>
      <div>${esc(run.message || "")}</div></div>`;
    return el;
  }
  const a = run.analysis || {};
  el.innerHTML = `<div class="answer"><h3>${esc(a.headline)}</h3>
    <ul>${(a.findings || []).slice(0, 3).map((f) => `<li>${esc(f)}</li>`).join("")}</ul>
    <div class="row"><button class="btn small" data-action="show-run">View analysis, chart & SQL</button>
    ${replay ? `<span class="tag replay">Replay · recorded run · data as of ${esc(run.data_as_of)}</span>` : `<span class="tag">Data as of ${esc(run.data_as_of)}</span>`}
    ${run.verification && run.verification.status === "verified" ? `<span class="tag ok" title="Each metric was recomputed from its definition for the confirmed scope and matched">✓ Numbers verified against the data</span>` : run.verification && run.verification.status === "mismatch" ? `<span class="tag warn" title="The recomputed metric disagrees with this result; see the warnings">⚠ Numbers do not match the definition</span>` : run.verification ? `<span class="tag" title="This answer's metric could not be recomputed independently">Not independently verified</span>` : ""}
    ${run.cost && run.cost.usd ? `<span class="tag">$${run.cost.usd.toFixed(3)}</span>` : ""}</div></div>`;
  el.querySelector('[data-action="show-run"]').addEventListener("click", () => { showRunInPanel(run); openPanel(); });
  return el;
}

/* ---------------------------------------------------------------- asking */
async function ask(question) {
  if (S.busy || !question.trim()) return;
  if (!S.me.user) { showSignin(); return; }
  S.busy = true; $(".send").disabled = true;
  try {
    if (!S.chatId) {
      const chat = await api("/api/chats", { method: "POST", body: { project_id: S.pendingProject } });
      S.chatId = chat.id; S.pendingProject = null;
      $("#messages").innerHTML = "";
      if (chat.project_id) S.expanded.add(chat.project_id);
    }
    $("#messages").insertAdjacentHTML("beforeend", userBubble(question));
    const status = statusLine("Starting…"); $("#messages").appendChild(status); scrollDown();
    const { run_id } = await api(`/api/chats/${S.chatId}/ask`, { method: "POST", body: { question, use_defaults: $("#use-defaults").checked } });
    $("#chat-title").textContent = question.slice(0, 80);
    refreshSidebar();
    streamRun(run_id, 0, status);
  } catch (e) {
    toast(e.message, 4500); S.busy = false; $(".send").disabled = false;
    document.querySelectorAll(".status-line").forEach((n) => n.closest(".msg").remove());
  }
}

function newTimeline(question, replay) {
  S.run = { steps: [], question, replay };
  $("#panel-sub").textContent = question;
  $("#panel-body").innerHTML = `<ol class="steps" id="steps"></ol><div id="result"></div>`;
  openPanelIfWide();
}
function openPanelIfWide() { if (window.innerWidth > 1180) openPanel(); }

function onEvent(e, status) {
  if (e.type === "step") {
    const label = STEP_LABELS[e.step] || e.step;
    if (e.status === "started") S.run.steps.push({ step: e.step, status: "running", detail: {}, t: e.t });
    else {
      const item = [...S.run.steps].reverse().find((s) => s.step === e.step) || (S.run.steps.push({ step: e.step }), S.run.steps.at(-1));
      Object.assign(item, { status: e.status, detail: e.detail || {} });
    }
    if (status && e.status === "started") status.querySelector(".txt").textContent = label + "…";
    renderSteps();
  } else if (e.type === "clarify") {
    if (status) status.remove();
    $("#messages").appendChild(clarifyCard(e)); scrollDown();
    S.busy = false; $(".send").disabled = false;
  } else if (e.type === "final") {
    if (status) status.remove();
    $("#messages").appendChild(answerCard(e, S.run && S.run.replay)); scrollDown();
    showResult(e);
    S.busy = false; $(".send").disabled = false;
    if (!S.run.replay && S.me.user) api("/api/me").then((m) => { S.me = m; renderUser(); }).catch(() => {});
  }
}

function streamRun(runId, since, status) {
  if (since === 0) newTimeline($("#chat-title").textContent, false);
  let count = since;
  const es = new EventSource(`/api/runs/${runId}/stream?since=${since}`);
  es.onmessage = (msg) => {
    count += 1;
    const e = JSON.parse(msg.data);
    if (e.type === "clarify") { e.run_id = runId; e.count = count; }
    onEvent(e, status);
    if (e.type === "clarify" || e.type === "final") es.close();
  };
  es.onerror = () => { es.close(); if (S.busy) { toast("Connection lost. Reopen the chat to see the result."); S.busy = false; $(".send").disabled = false; } };
}

const clean = (o) => String(o).replace(/\s*\((recommended|default)\)\s*/gi, " ").trim();
function clarifyCard(e) {
  const el = document.createElement("div"); el.className = "msg";
  const qs = e.questions || [];
  const confirm = e.kind === "confirm";
  const title = confirm ? "Here's what I'll compute" : (e.round || 1) > 1 ? `A few more questions (round ${e.round})` : "A few questions so I get this exactly right";
  const readback = confirm && e.readback ? `<div class="readback">${e.readback.split("\n").map((l) => esc(l).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")).join("<br>")}</div>` : "";
  el.innerHTML = `<div class="clarify"><h4>${esc(title)}</h4>${readback}
    ${qs.map((q, i) => `<div class="q">${esc(q.question)}</div><div class="why">${esc(q.why || "")}</div>
      <div class="chips" data-q="${i}">${q.options.map((o) => { const rec = clean(o) === clean(q.default); return `<button class="chip ${rec ? "selected" : ""}" data-opt="${esc(o)}">${esc(clean(o))}${rec ? '<span class="rec">recommended</span>' : ""}</button>`; }).join("")}</div>
      <input class="other" data-q="${i}" maxlength="300" placeholder="${confirm ? "Or tell me what to change" : "Or type your own answer"}">`).join("")}
    <div class="row" style="margin-top:14px"><button class="btn primary small" data-go>Continue</button>
    ${confirm ? "" : '<button class="btn small" data-defaults>Use recommended</button>'}</div></div>`;
  el.querySelectorAll(".chips").forEach((group) => group.addEventListener("click", (ev) => {
    const chip = ev.target.closest(".chip"); if (!chip) return;
    group.querySelectorAll(".chip").forEach((c) => c.classList.remove("selected")); chip.classList.add("selected");
  }));
  const submit = async (useDefaults) => {
    const answers = {};
    qs.forEach((q, i) => {
      const sel = el.querySelector(`.chips[data-q="${i}"] .chip.selected`);
      const own = el.querySelector(`.other[data-q="${i}"]`).value.trim();
      answers[q.question] = useDefaults ? clean(q.default) : own || clean(!sel ? q.default : sel.dataset.opt);
    });
    el.querySelectorAll("button").forEach((b) => (b.disabled = true));
    const summary = Object.entries(answers).map(([k, v]) => `${k} → ${v}`).join("\n");
    el.insertAdjacentHTML("afterend", userBubble(summary));
    const status = statusLine("Continuing…"); el.parentNode.appendChild(status); scrollDown();
    S.busy = true; $(".send").disabled = true;
    try { await api(`/api/runs/${e.run_id}/answer`, { method: "POST", body: { answers } }); streamRun(e.run_id, e.count, status); }
    catch (err) { toast(err.message); status.remove(); S.busy = false; $(".send").disabled = false; }
  };
  el.querySelector("[data-go]").addEventListener("click", () => submit(false));
  const defaults = el.querySelector("[data-defaults]");
  if (defaults) defaults.addEventListener("click", () => submit(true));
  return el;
}

/* ---------------------------------------------------------------- demos */
function replayDemo(slug, question) {
  S.chatId = null;
  closeSidebarMobile();
  $("#chat-title").textContent = "Demo replay";
  $("#move-project").hidden = true;
  $("#messages").innerHTML = userBubble(question);
  const status = statusLine("Starting…"); $("#messages").appendChild(status);
  newTimeline(question, true);
  S.busy = true;
  const es = new EventSource(`/api/demos/${encodeURIComponent(slug)}/stream`);
  es.onmessage = (msg) => { const e = JSON.parse(msg.data); onEvent(e, status); if (e.type === "final") es.close(); };
  es.onerror = () => { es.close(); S.busy = false; };
}

/* ---------------------------------------------------------------- panel: steps and result */
function stepDetail(s) {
  const d = s.detail || {};
  const parts = [];
  if (d.tables) parts.push(`Tables: ${d.tables.map(esc).join(", ")}`);
  if (d.values && d.values.length) parts.push(`Values found: ${d.values.map((v) => `<code>${esc(v)}</code>`).join(", ")}`);
  if (d.understood) parts.push(`Understood: ${esc(d.understood)}`);
  if (d.questions && d.questions.length) parts.push(`Asking (round ${esc(d.round)}): ${d.questions.map(esc).join(" · ")}`);
  if (d.resolved) parts.push(`<div class="plan">${esc(d.resolved)}</div>`);
  if (d.plan) parts.push(`<div class="plan">${esc(d.plan)}</div>`);
  if (d.sql) parts.push(`${d.attempt > 1 ? `Attempt ${d.attempt}. ` : ""}${esc(d.explanation || "")}<pre>${esc(d.sql)}</pre>`);
  if (d.problems) parts.push(d.problems.map((p) => `<div class="problem">⚠ ${esc(p)}</div>`).join(""));
  if (d.rows !== undefined) parts.push(`${d.rows} rows, ${(d.columns || []).length} columns in ${d.query_seconds}s${d.truncated ? " (truncated)" : ""}`);
  if (d.number_check) parts.push(`Number check: ${d.number_check}${d.rewritten ? " (after one rewrite)" : ""}`);
  if (d.reason) parts.push(esc(d.reason));
  if (d.error) parts.push(`<div class="problem">${esc(d.error)}</div>`);
  if (d.gaps_closed) parts.push(`Explained ${d.gaps_closed} open gap(s)`);
  if (d.facts !== undefined) parts.push(`${d.facts} fact(s) saved`);
  return parts.join("<br>");
}
function renderSteps() {
  const ol = $("#steps"); if (!ol) return;
  ol.innerHTML = S.run.steps.map((s, i) => {
    const detail = stepDetail(s);
    const open = s.open ?? (s.status === "running" || ["retry", "failed", "warning", "needs_input"].includes(s.status));
    return `<li class="step ${s.status}"><span class="dot">${STATUS_ICON[s.status] ?? "·"}</span>
      <div class="step-head" data-action="toggle-step" data-i="${i}"><span class="step-name">${esc(STEP_LABELS[s.step] || s.step)}</span>
      <span class="step-time">${s.detail && s.detail.seconds !== undefined ? s.detail.seconds + "s" : s.status === "running" ? "…" : ""}</span></div>
      ${detail ? `<div class="step-detail" ${open ? "" : "hidden"}>${detail}</div>` : ""}</li>`;
  }).join("");
}

function showRunInPanel(run) {
  newTimeline(run.question || $("#chat-title").textContent, false);
  for (const e of run.events || []) if (e.type === "step") onEvent(e, null);
  S.run.restoring = true;
  showResult(run);
  S.run.restoring = false;
  $("#panel-body").scrollTop = 0;
}

function showResult(run) {
  const box = $("#result"); if (!box) return;
  S.run.final = run;
  if (run.status !== "done") { box.innerHTML = `<div class="result"><h3>${esc(run.status)}</h3><p>${esc(run.message || "")}</p></div>`; return; }
  const a = run.analysis || {};
  const list = (title, items) => items && items.length ? `<h4>${title}</h4><ul>${items.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : "";
  const res = run.result || { columns: [], data: [] };
  box.innerHTML = `<div class="result">
    <h3>${esc(a.headline)}</h3>
    <div id="chart-area"></div>
    <div class="exports">
      <button class="btn small" data-action="export" data-kind="copy">⧉ Copy</button>
      <button class="btn small" data-action="export" data-kind="png" ${run.chart && run.chart.figure ? "" : "disabled"}>▣ PNG</button>
      <button class="btn small" data-action="export" data-kind="csv">⇩ CSV</button>
      <button class="btn small" data-action="export" data-kind="ppt">▤ PPT</button>
      <button class="btn small" data-action="export" data-kind="sql">{ } SQL</button>
    </div>
    ${list("Findings", a.findings)}${list("Recommendations", a.recommendations)}${list("Caveats", a.caveats)}
    ${(run.warnings || []).map((w) => `<div class="warning-box">⚠ ${esc(w)}</div>`).join("")}
    ${list("Follow-up questions", a.follow_up_questions)}
    <h4>Result (${res.data.length} rows${res.truncated ? ", truncated" : ""})</h4>
    <div class="table-wrap"><table><thead><tr>${res.columns.map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead>
      <tbody>${res.data.slice(0, 200).map((r) => `<tr>${r.map((v, i) => `<td class="${typeof v === "number" ? "num" : ""}">${fmtNum(v, res.columns[i])}</td>`).join("")}</tr>`).join("")}</tbody></table></div>
    <div class="meta">Data as of ${esc(run.data_as_of)} (${esc(run.data_size)} shop) · ${run.attempts || 1} SQL attempt(s)
      ${run.cost ? ` · ${run.cost.calls} LLM calls · ${run.cost.input + run.cost.output} tokens · $${(run.cost.usd || 0).toFixed(4)}` : ""}</div>
  </div>`;
  box.querySelectorAll("[data-action='export']").forEach((b) => b.addEventListener("click", () => exportRun(b.dataset.kind, run)));
  const followUps = [...box.querySelectorAll("h4")].find((h) => h.textContent === "Follow-up questions");
  if (followUps && S.me.user) followUps.nextElementSibling.querySelectorAll("li").forEach((li) => {
    li.style.cursor = "pointer"; li.title = "Ask this";
    li.addEventListener("click", () => { $("#question").value = li.textContent; $("#question").focus(); });
  });
  drawChart(run.chart);
  if (!S.run.restoring) requestAnimationFrame(() => box.scrollIntoView({ behavior: "smooth", block: "start" }));
}

function drawChart(chart) {
  const area = $("#chart-area"); if (!area || !chart) return;
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
  const draw = () => window.Plotly.newPlot("chart", fig.data, layout, { displaylogo: false, responsive: true, modeBarButtonsToRemove: ["lasso2d", "select2d"] });
  if (window.Plotly) draw(); else window.addEventListener("load", draw, { once: true });
}

/* ---------------------------------------------------------------- exports */
function markdown(run) {
  const a = run.analysis || {};
  const sec = (t, xs) => (xs && xs.length ? `\n**${t}**\n${xs.map((x) => `- ${x}`).join("\n")}\n` : "");
  return `## ${a.headline}\n${sec("Findings", a.findings)}${sec("Recommendations", a.recommendations)}${sec("Caveats", a.caveats)}` +
    `\n_Pantree Insights · data as of ${run.data_as_of} (${run.data_size} shop, simulated)_\n`;
}
function csv(run) {
  const q = (v) => (v === null || v === undefined ? "" : /[",\n]/.test(String(v)) ? `"${String(v).replace(/"/g, '""')}"` : String(v));
  return [run.result.columns.map(q).join(","), ...run.result.data.map((r) => r.map(q).join(","))].join("\n");
}
async function chartPng(width = 1600, height = 900) {
  const el = document.getElementById("chart");
  if (!el || !window.Plotly) return null;
  return window.Plotly.toImage(el, { format: "png", width, height,
    layout: { paper_bgcolor: "#ffffff", plot_bgcolor: "#ffffff", font: { color: "#161827" } } });
}
async function exportRun(kind, run) {
  const name = fileName(run.question || (run.analysis || {}).headline);
  try {
    if (kind === "copy") { await navigator.clipboard.writeText(markdown(run)); toast("Copied as Markdown"); }
    else if (kind === "sql") { await navigator.clipboard.writeText(run.sql || ""); toast("SQL copied"); }
    else if (kind === "csv") download(`${name}.csv`, new Blob([csv(run)], { type: "text/csv" }));
    else if (kind === "png") { const url = await chartPng(); if (url) download(`${name}.png`, await (await fetch(url)).blob()); }
    else if (kind === "ppt") await exportPpt(run, name);
  } catch (e) { toast("Export failed: " + e.message); }
}
async function exportPpt(run, name) {
  if (!window.PptxGenJS) { toast("PowerPoint library still loading, try again"); return; }
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

/* ---------------------------------------------------------------- dialogs: projects, memory, pulse */
function dialog(html) { $("#dialog-body").innerHTML = html; $("#dialog").hidden = false; return $("#dialog-body"); }
function closeDialog() { $("#dialog").hidden = true; }

function projectDialog(project) {
  const d = dialog(`<h2>${project ? "Project settings" : "New project"}</h2>
    <p class="muted">A project groups chats on one topic. Its instructions and insights are given to the agents for every question in it.</p>
    <label>Name</label><input id="p-name" maxlength="200" value="${esc(project ? project.name : "")}" placeholder="e.g. Festive season 2026">
    <label>Instructions for the analyst</label>
    <textarea id="p-instr" maxlength="4000" placeholder="e.g. Focus on Diwali and Navratri. Compare with last year. I report to the marketing head.">${esc(project ? project.instructions : "")}</textarea>
    <div class="dialog-actions">${project ? `<button class="btn danger" data-del>Delete project</button><span style="flex:1"></span>` : ""}
      <button class="btn" data-close>Cancel</button><button class="btn primary" data-save>Save</button></div>`);
  d.querySelector("[data-close]").onclick = closeDialog;
  d.querySelector("[data-save]").onclick = async () => {
    const body = { name: $("#p-name").value.trim(), instructions: $("#p-instr").value };
    if (!body.name) return toast("Give the project a name");
    try {
      const p = project ? await api(`/api/projects/${project.id}`, { method: "PATCH", body }) : await api("/api/projects", { method: "POST", body });
      S.expanded.add(p.id); closeDialog(); refreshSidebar();
    } catch (e) { toast(e.message); }
  };
  if (project) d.querySelector("[data-del]").onclick = async () => {
    if (!confirm("Delete this project? Its chats are kept; its memory is removed.")) return;
    await api(`/api/projects/${project.id}`, { method: "DELETE", body: {} }); closeDialog(); refreshSidebar();
  };
}

async function memoryDialog() {
  if (!S.me.user) return showSignin();
  const m = await api("/api/memory");
  const item = (x, admin) => `<div class="mem-item"><div class="head"><div><div class="q">${esc(x.question)}</div>
      <div class="muted" style="font-size:12px">${x.project_name ? "📁 " + esc(x.project_name) + " · " : ""}data as of ${esc(x.data_as_of)}</div></div>
      <div>${admin && x.scope !== "business" ? `<button class="btn small" data-promote="${x.id}">Share</button>` : ""}
      <button class="btn small danger" data-forget="${x.id}">Forget</button></div></div>
      <ul>${(x.facts || []).map((f) => `<li>${f.type === "hypothesis" ? "<i>Hypothesis:</i> " : ""}${esc(f.statement)}</li>`).join("") || `<li>${esc(x.headline)}</li>`}</ul></div>`;
  const d = dialog(`<h2>Memory</h2>
    <p class="muted">What the analyst remembers. It uses this as background and re-checks numbers against the data.</p>
    <label>About you and your preferences (used in every question)</label>
    <textarea id="prefs" maxlength="4000" placeholder="e.g. I'm the marketing lead. Show money in lakh. I care most about ROAS and new customers.">${esc(m.preferences)}</textarea>
    <div class="dialog-actions" style="margin-top:8px"><button class="btn small primary" data-save-prefs>Save preferences</button></div>
    <label>Your insights (${m.insights.length})</label>${m.insights.map((x) => item(x, m.is_admin)).join("") || `<div class="muted">Nothing yet: insights are saved after each answer.</div>`}
    <label>Shared business knowledge (${m.business.length})</label>
    <div class="muted" style="font-size:12px">${m.is_admin ? "You are an admin: share insights to make them part of everyone's business memory." : "Curated by the admin, used for everyone."}</div>
    ${m.business.map((x) => (m.is_admin ? item(x, true) : `<div class="mem-item"><div class="q">${esc(x.question)}</div><ul>${(x.facts || []).map((f) => `<li>${esc(f.statement)}</li>`).join("")}</ul></div>`)).join("")}
    <div class="dialog-actions"><button class="btn" data-close>Close</button></div>`);
  d.querySelector("[data-close]").onclick = closeDialog;
  d.querySelector("[data-save-prefs]").onclick = async () => { await api("/api/memory/preferences", { method: "PUT", body: { text: $("#prefs").value } }); toast("Preferences saved"); };
  d.querySelectorAll("[data-forget]").forEach((b) => (b.onclick = async () => { await api(`/api/memory/${b.dataset.forget}`, { method: "DELETE", body: {} }); memoryDialog(); }));
  d.querySelectorAll("[data-promote]").forEach((b) => (b.onclick = async () => { await api(`/api/memory/${b.dataset.promote}/promote`, { method: "POST", body: {} }); toast("Shared with everyone"); memoryDialog(); }));
}

async function loadPulseCount() {
  try {
    const p = await api("/api/pulse");
    const open = p.flags.filter((f) => !f.explained && !f.resolved).length;
    $("#pulse-count").textContent = open; $("#pulse-count").hidden = !open;
  } catch { /* pulse not ready yet */ }
}

async function pulseDialog() {
  const p = await api("/api/pulse");
  const pct = (f) => `${f.change_pct > 0 ? "+" : ""}${f.change_pct}%`;
  const row = (f) => `<div class="flag"><div class="chg ${f.direction}">${pct(f)}</div><div class="txt">
      <div><b>${esc(f.label)}</b> ${f.breakdown !== "total" ? `· ${esc(f.breakdown)} = ${esc(f.segment)}` : ""}</div>
      <div class="muted" style="font-size:12px">Week ${esc(f.window_start)} to ${esc(f.window_end)} vs the previous 8 weeks
      ${f.explained ? ` · ${esc(f.explained)}` : f.resolved ? " · explained by an earlier analysis" : ""}</div></div>
      ${!f.explained && !f.resolved && S.me.user ? `<button class="btn small" data-investigate='${esc(JSON.stringify(f))}'>Investigate</button>` : ""}</div>`;
  const open = p.flags.filter((f) => !f.explained && !f.resolved), other = p.flags.filter((f) => f.explained || f.resolved);
  const d = dialog(`<h2>Pulse</h2>
    <p class="muted">${p.series || 0} KPI series checked daily (data as of ${esc(p.data_as_of || "?")}). The latest week is compared with the 8 weeks before it.</p>
    <label>Needs a look (${open.length})</label>${open.map(row).join("") || `<div class="muted">Nothing unusual this week.</div>`}
    ${other.length ? `<label>Explained (${other.length})</label>${other.map(row).join("")}` : ""}
    <div class="dialog-actions"><button class="btn" data-close>Close</button></div>`);
  d.querySelector("[data-close]").onclick = closeDialog;
  d.querySelectorAll("[data-investigate]").forEach((b) => (b.onclick = () => {
    const f = JSON.parse(b.dataset.investigate);
    closeDialog(); S.pendingProject = null; renderWelcome();
    $("#question").value = `Why did ${f.label.toLowerCase()}${f.breakdown !== "total" ? ` for ${f.breakdown} ${f.segment}` : ""} change ${pct(f)} in the week ending ${f.window_end} compared with the previous 8 weeks?`;
    $("#question").focus();
  }));
}

function chatMenu(id) {
  const chat = S.chats.find((c) => c.id === id); if (!chat) return;
  const d = dialog(`<h2>Chat</h2><label>Title</label><input id="c-title" maxlength="200" value="${esc(chat.title)}">
    <div class="dialog-actions"><button class="btn danger" data-del>Delete chat</button><span style="flex:1"></span>
    <button class="btn" data-close>Cancel</button><button class="btn primary" data-save>Save</button></div>`);
  d.querySelector("[data-close]").onclick = closeDialog;
  d.querySelector("[data-save]").onclick = async () => { await api(`/api/chats/${id}`, { method: "PATCH", body: { title: $("#c-title").value } }); closeDialog(); refreshSidebar(); if (S.chatId === id) $("#chat-title").textContent = $("#c-title").value; };
  d.querySelector("[data-del]").onclick = async () => { await api(`/api/chats/${id}`, { method: "DELETE", body: {} }); closeDialog(); if (S.chatId === id) renderWelcome(); else refreshSidebar(); };
}

/* ---------------------------------------------------------------- sign-in */
async function emailSubmit(ev) {
  ev.preventDefault();
  const email = $("#email").value.trim(), codeBox = $("#code"), msg = $("#email-msg");
  msg.className = "form-msg"; msg.textContent = "";
  try {
    if (codeBox.hidden) {
      await api("/auth/email/request", { method: "POST", body: { email } });
      codeBox.hidden = false; codeBox.focus(); $("#email-btn").textContent = "Sign in";
      msg.textContent = `We sent a 6-digit code to ${email}.`;
    } else {
      await api("/auth/email/verify", { method: "POST", body: { email, code: codeBox.value } });
      location.href = "/";
    }
  } catch (e) { msg.className = "form-msg err"; msg.textContent = e.message; }
}

function closeSidebarMobile() { $("#app").classList.remove("sidebar-open"); }

/* ---------------------------------------------------------------- events */
document.addEventListener("click", async (ev) => {
  const t = ev.target.closest("[data-action]"); if (!t) return;
  const a = t.dataset.action, id = t.dataset.id;
  if (["chat-menu", "edit-project"].includes(a)) ev.stopPropagation();
  switch (a) {
    case "new-chat": S.pendingProject = t.dataset.project || null; closeSidebarMobile(); renderWelcome(); $("#question").focus(); break;
    case "open-chat": openChat(id); break;
    case "chat-menu": chatMenu(id); break;
    case "toggle-project": S.expanded.has(id) ? S.expanded.delete(id) : S.expanded.add(id); refreshSidebar(); break;
    case "new-project": if (!S.me.user) return showSignin(); projectDialog(null); break;
    case "edit-project": projectDialog(S.projects.find((p) => p.id === id)); break;
    case "example": $("#question").value = t.dataset.q; ask(t.dataset.q); $("#question").value = ""; break;
    case "replay": replayDemo(t.dataset.slug, t.textContent.replace(/^▶ Demo · [\d.]+s/, "").trim()); break;
    case "toggle-panel": $("#app").classList.toggle("panel-closed"); break;
    case "toggle-step": {
      const det = t.parentElement.querySelector(".step-detail");
      if (det) { det.hidden = !det.hidden; if (S.run && S.run.steps[t.dataset.i]) S.run.steps[t.dataset.i].open = !det.hidden; }
      break;
    }
    case "show-memory": memoryDialog(); break;
    case "show-pulse": pulseDialog(); break;
    case "signin": showSignin(); break;
    case "demo-mode": S.demoMode = true; $("#signin").hidden = true; break;
    case "logout": await api("/auth/logout", { method: "POST", body: {} }); location.href = "/"; break;
    case "theme": {
      const root = document.documentElement;
      const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
      root.dataset.theme = dark ? "light" : "dark";
      try { localStorage.setItem("theme", root.dataset.theme); } catch { /* private mode */ }
      if (S.run && S.run.final) drawChart(S.run.final.chart);
      break;
    }
    case "open-sidebar": $("#app").classList.add("sidebar-open"); break;
    case "close-sidebar": closeSidebarMobile(); break;
  }
});
$("#dialog").addEventListener("click", (ev) => { if (ev.target.id === "dialog") closeDialog(); });
$("#composer").addEventListener("submit", (ev) => { ev.preventDefault(); const q = $("#question").value; $("#question").value = ""; autosize(); ask(q); });
const autosize = () => { const ta = $("#question"); ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight, 160) + "px"; };
$("#question").addEventListener("input", autosize);
$("#question").addEventListener("keydown", (ev) => { if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); $("#composer").requestSubmit(); } });
$("#email-form").addEventListener("submit", emailSubmit);
$("#move-project").addEventListener("change", async (ev) => {
  if (!S.chatId) return;
  await api(`/api/chats/${S.chatId}`, { method: "PATCH", body: { project_id: ev.target.value || null, move: true } });
  if (ev.target.value) S.expanded.add(ev.target.value);
  toast(ev.target.value ? "Moved to project: its instructions and memory now apply" : "Moved out of the project");
  refreshSidebar();
});
try { const th = localStorage.getItem("theme"); if (th) document.documentElement.dataset.theme = th; } catch { /* private mode */ }
init();
