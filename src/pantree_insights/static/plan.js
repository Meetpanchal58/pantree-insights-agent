/* plan: explicit ES module dependencies; no build step. */
import { $, OFFLINE_DEMOS, S, STATUS_ICON, STEP_LABELS, esc } from './state.js';
import { answerCard, closeSidebarMobile, openPanel, recoveryCard, scrollDown, statusLine, toast, userBubble } from './render.js';
import { displayActiveModel, refreshAI } from './workspace.js';
import { showResult } from './results.js';
import { api } from './api.js';
import { renderUser } from './session.js';
import { ask, leaveView, renderSidebar } from './chat.js';

export function newTimeline(question, replay) {
  S.run = { steps: [], question, replay };
  $("#panel-sub").textContent = question;
  $("#panel-body").innerHTML = `<ol class="steps" id="steps"></ol><div id="result"></div>`;
  openPanelIfWide();
}

export function openPanelIfWide() { if (window.innerWidth > 1180) openPanel(); }

export function onEvent(e, status) {
  if (!S.run) return;
  if (e.ai && e.type !== "final" && typeof displayActiveModel === "function") displayActiveModel(e.ai);
  if (e.type === "answered") {
    for (const item of S.run.steps) if (item.status === "needs_input") item.status = "done";
    if (!S.run.restoring) renderSteps();
  } else if (e.type === "step") {
    const label = STEP_LABELS[e.step] || e.step;
    if (e.status === "started") S.run.steps.push({ step: e.step, status: "running", detail: {}, t: e.t });
    else {
      const item = [...S.run.steps].reverse().find((s) => s.step === e.step) || (S.run.steps.push({ step: e.step }), S.run.steps.at(-1));
      Object.assign(item, { status: e.status, detail: e.detail || {} });
    }
    if (status && e.status === "started") status.querySelector(".txt").textContent = label + "…";
    if (!S.run.restoring) renderSteps();
  } else if (e.type === "result") {
    showResult({ ...e, status: "done", analysis: { headline: "Query result ready. Preparing the explanation…" } });
    if (status) status.querySelector(".txt").textContent = "Result ready. Preparing the explanation…";
    openPanel();
  } else if (e.type === "clarify") {
    if (status) status.remove();
    S.pending = e;
    if (typeof displayActiveModel === "function") displayActiveModel(e.ai);
    if (window.innerWidth <= 1180) $("#app").classList.add("panel-closed");
    const old = [...document.querySelectorAll("[data-proposal]")].some(n => n.dataset.proposal === proposalKey(e));
    if (!old) $("#messages").appendChild(conversationMessage(e));
    $("#question").disabled = false;
    $("#question").placeholder = "Confirm, describe a change, or cancel…";
    scrollDown();
    S.busy = false; $(".send").disabled = false;
  } else if (e.type === "final") {
    if (typeof displayActiveModel === "function") displayActiveModel(null);
    S.pending = null; $("#question").placeholder = "Ask a new question or continue the investigation…";
    if (S.chatId) S.chatCache.delete(S.chatId);
    if (status) status.remove();
    for (const item of S.run.steps) if (item.status === "needs_input") item.status = "done";
    renderSteps();
    $("#messages").appendChild(answerCard(e, S.run && S.run.replay)); scrollDown();
    showResult(e);
    S.busy = false; $(".send").disabled = false;
    if (!S.run.replay && S.me.user) api("/api/me").then((m) => { S.me = m; renderUser(); if (typeof refreshAI === "function") refreshAI(true); }).catch(() => {});
  }
}

export function streamRun(runId, since, status, question) {
  if (since === 0) newTimeline(question || $("#chat-title").textContent, false);
  S.stream?.close();
  const view = S.view;
  S.busy = true; $(".send").disabled = true;
  let count = since;
  const es = new EventSource(`/api/runs/${runId}/stream?since=${since}`);
  S.stream = es;
  const receive = (e) => {
    count += 1;
    if (e.type === "clarify") { e.run_id = runId; e.count = count; }
    onEvent(e, status);
    return e.type === "clarify" || e.type === "final";
  };
  es.onmessage = (msg) => {
    if (view === S.view && S.stream === es && receive(JSON.parse(msg.data))) es.close();
  };
  es.onerror = async () => {
    es.close();
    if (view !== S.view || S.stream !== es) return;

    const controller = new AbortController();
    const polling = { close: () => controller.abort() };
    S.stream = polling;
    const current = () => view === S.view && S.stream === polling;
    if (status?.querySelector(".txt")) status.querySelector(".txt").textContent = "Checking analysis progress…";
    const deadline = Date.now() + 5 * 60 * 1000;
    let failures = 0;
    while (current() && Date.now() < deadline) {
      try {
        const update = await api(`/api/runs/${runId}/progress?since=${count}`, {
          signal: controller.signal, timeoutMs: 12000, retries: 0,
        });
        if (!current()) return;
        failures = 0;
        for (const event of update.events) {
          if (receive(event)) return;
        }
        if (!update.active) break;
      } catch (error) {
        if (!current() || error.name === "AbortError") return;
        failures += 1;
        if ([401, 403, 404, 410].includes(error.status) || failures >= 3) break;
        if (error.retryAfter > 10) break;
        await new Promise((resolve) => setTimeout(resolve, Math.max(1000, (error.retryAfter || 0) * 1000)));
      }
    }
    if (current() && S.busy) {
      if (status) status.remove();
      $("#messages").appendChild(recoveryCard("The connection was interrupted. Reopen this chat to check whether the analysis finished."));
      S.busy = false; $(".send").disabled = false;
    }
  };
}

export function proposalKey(e) { return `${e.run_id || ""}:${e.plan_version || 0}:${e.message || e.readback || ""}`; }

export function conversationMessage(e) {
  const el = document.createElement("div"); el.className = "msg";
  el.dataset.proposal = proposalKey(e);
  const text = e.message || e.readback || (e.questions || []).map(q => q.question).join("\n");
  el.innerHTML = `<div class="answer conversation-message">${esc(text).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/\n/g, "<br>")}</div>`;
  if (e.kind === 'confirm' && e.run_id) {
    const button = document.createElement('button'); button.className = 'btn primary confirm-plan';
    button.textContent = 'Run this plan'; button.dataset.confirmVersion = e.plan_version;
    button.onclick = () => {
      if (S.busy || !S.pending || S.pending.plan_version !== e.plan_version ||
          S.pending.run_id !== e.run_id || proposalKey(S.pending) !== proposalKey(e)) return toast('Use the latest pending plan.');
      ask('Go ahead');
    };
    el.querySelector('.answer').appendChild(button);
  }
  return el;
}

export function educationalDemoEvents(demo) {
  const steps = (demo.events || []).filter(e => e.type === "step");
  // Older pinned demo fixtures contain only a summary step. Explain their actual
  // saved SQL, output and chart in order; never pretend these are paid LLM calls.
  if (steps.length >= 3) return demo.events;
  const final = demo.events?.at(-1);
  if (!final || final.type !== "final") return demo.events || [];
  const detail = (step, extra) => ({ type: "step", step, status: "done", detail: extra });
  const result = final.result || { columns: [], data: [] };
  const sample_rows = (result.data || []).slice(0, 3).map(row =>
    Object.fromEntries(row.map((value, i) => [result.columns[i], value])));
  return [
    detail("check_input", { reason: "This is a saved, model-free walkthrough using simulated data." }),
    detail("understand", { understood: demo.question, reason: "The analysis scope was fixed when the snapshot was built." }),
    detail("write_sql", { sql: final.sql, reason: "Read-only, validated SQL saved with the demo." }),
    detail("run_query", { rows: result.data?.length || 0, columns: result.columns || [], sample_rows }),
    detail("analyse", { findings: final.analysis?.findings || [], reason: "Saved findings, not live-generated answers." }),
    detail("chart", { reason: "The chart and downloadable table visualize the saved SQL output." }),
    final,
  ];
}

export function replayDemo(slug, question) {
  const demo = OFFLINE_DEMOS.find((item) => item.slug === slug);
  if (demo) question = demo.question;
  const view = leaveView();
  S.chatId = null;
  closeSidebarMobile();
  renderSidebar();
  $("#chat-title").textContent = demo ? "Offline demo" : "Recorded answer";
  $("#move-project").hidden = true;
  $("#messages").innerHTML = userBubble(question);
  const status = statusLine("Starting…"); $("#messages").appendChild(status);
  newTimeline(question, true);
  S.busy = true; $(".send").disabled = true;
  if (demo) {
    $("#question").disabled = true;
    $("#question").placeholder = "Offline demo. Start a new chat to ask your own question.";
    for (const event of educationalDemoEvents(demo)) {
      if (view !== S.view) return;
      onEvent({ ...event, replay: true }, status);
    }
    $(".send").disabled = true;
    openPanel();
    return;
  }
  const es = new EventSource(`/api/demos/${encodeURIComponent(slug)}/stream?instant=true`);
  S.stream = es;
  es.onmessage = (msg) => { if (view !== S.view || S.stream !== es) return; const e = JSON.parse(msg.data); onEvent(e, status); if (e.type === "final") es.close(); };
  es.onerror = () => { es.close(); if (view !== S.view || S.stream !== es) return; S.busy = false; $(".send").disabled = false; toast("Could not load this demo. Please try again."); };
}

export function stepDetail(s) {
  const d = s.detail || {};
  const parts = [];
  if (d.tables) parts.push(`Tables: ${d.tables.map(esc).join(", ")}`);
  if (d.values && d.values.length) parts.push(`Values found: ${d.values.map((v) => `<code>${esc(v)}</code>`).join(", ")}`);
  if (d.understood) parts.push(`Understood: ${esc(d.understood)}`);
  if (d.questions && d.questions.length) parts.push(`Asking (round ${esc(d.round)}): ${d.questions.map(esc).join(" · ")}`);
  if (d.position) parts.push(`Position in queue: ${esc(d.position)}`);
  if (d.readback) parts.push(`<div class="plan">${esc(d.readback.replace(/\*\*/g, ""))}</div>`);
  if (d.resolved) parts.push(`<div class="plan">${esc(d.resolved)}</div>`);
  if (d.plan) parts.push(`<div class="plan">${esc(d.plan)}</div>`);
  if (d.sql) parts.push(`${d.attempt > 1 ? `Attempt ${d.attempt}. ` : ""}${esc(d.explanation || "")}<pre>${esc(d.sql)}</pre>`);
  if (d.problems) parts.push(d.problems.map((p) => `<div class="problem">⚠ ${esc(p)}</div>`).join(""));
  if (d.rows !== undefined) parts.push(`${d.rows} rows, ${(d.columns || []).length} columns in ${d.query_seconds}s${d.truncated ? " (truncated)" : ""}`);
  if (d.number_check) parts.push(`Number check: ${d.number_check}${d.rewritten ? " (after one rewrite)" : ""}`);
  if (d.reason) parts.push(esc(d.reason));
  if (d.sample_rows?.length) parts.push("<b>Saved result preview:</b><pre>" + esc(JSON.stringify(d.sample_rows, null, 2)) + "</pre>");
  if (d.findings?.length) parts.push("<b>Findings:</b> " + d.findings.map(esc).join(" · "));
  if (d.error) parts.push(`<div class="problem">${esc(d.error)}</div>`);
  if (d.gaps_closed) parts.push(`Explained ${d.gaps_closed} open gap(s)`);
  if (d.facts !== undefined) parts.push(`${d.facts} fact(s) saved`);
  return parts.join("<br>");
}

export function renderSteps() {
  const ol = $("#steps"); if (!ol) return;
  ol.innerHTML = S.run.steps.map((s, i) => {
    const detail = stepDetail(s);
    const open = s.open ?? (S.run?.replay || s.status === "running" || ["retry", "failed", "warning", "needs_input"].includes(s.status));
    const label = s.status === "needs_input" ? "Waiting for your confirmation or answer" : s.status;
    return `<li class="step ${s.status}"><span class="dot" title="${esc(label)}" aria-label="${esc(label)}">${s.status === "needs_input" ? "…" : STATUS_ICON[s.status] ?? "·"}</span>
      <div class="step-head" data-action="toggle-step" data-i="${i}"><span class="step-name">${esc(STEP_LABELS[s.step] || s.step)}</span>
      <span class="step-time">${s.detail && s.detail.seconds !== undefined ? s.detail.seconds + "s" : s.status === "running" ? "…" : ""}</span></div>
      ${detail ? `<div class="step-detail" ${open ? "" : "hidden"}>${detail}</div>` : ""}</li>`;
  }).join("");
}

export function showRunInPanel(run) {
  newTimeline(run.question || $("#chat-title").textContent, !!run.replay);
  S.run.restoring = true;
  for (const e of run.events || []) if (["step", "answered"].includes(e.type)) onEvent(e, null);
  renderSteps();
  showResult(run);
  S.run.restoring = false;
  $("#panel-body").scrollTop = 0;
}
