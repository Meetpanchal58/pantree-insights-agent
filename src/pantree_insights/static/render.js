/* render: explicit ES module dependencies; no build step. */
import { $, S, esc } from './state.js';
import { showRunInPanel } from './plan.js';

export function recoveryCard(message, action = "reopen-chat", retryAfter = 0) {
  const el = document.createElement("div"); el.className = "msg";
  el.innerHTML = `<div class="answer error-box" role="alert"><h3>Could not finish loading</h3><p>${esc(message)}</p>
    <button class="btn small" data-action="${action}">${action === "retry-start" ? "Retry loading" : "Reopen chat"}</button></div>`;
  if (Number.isFinite(retryAfter) && retryAfter > 0) {
    const button = el.querySelector("button"), label = button.textContent;
    const until = Date.now() + retryAfter * 1000;
    const update = () => {
      const remaining = Math.max(0, Math.ceil((until - Date.now()) / 1000));
      button.disabled = remaining > 0;
      button.textContent = remaining ? `${label} in ${remaining}s` : label;
      if (remaining && el.isConnected) setTimeout(update, 1000);
    };
    button.disabled = true;
    setTimeout(update, 0);
  }
  return el;
}

export function toast(msg, ms = 2600) {
  const t = $("#toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toast.timer); toast.timer = setTimeout(() => (t.hidden = true), ms);
}

export function fmtNum(v, col = "") {
  if (v === null || v === undefined || v === "") return '<span class="muted" title="No value">—</span>';
  if (typeof v !== "number") return esc(v);
  if (/roas/.test(col)) return v.toLocaleString("en-IN", { maximumFractionDigits: 1 }) + "×";
  if (/rate|share|pct|ctr|conversion|margin|lift|growth/.test(col) && Math.abs(v) <= 1.5) return (v * 100).toFixed(1) + "%";
  const volume = /(^|_)(orders?|lines?|sessions|count|units|quantity|qty|customers|buyers|tickets|reviews|impressions|contacts|visits)(_|$)/.test(col);
  if (!volume && /revenue|sales|spend|aov|amount|cost|cac|value|price/.test(col)) return "₹" + v.toLocaleString("en-IN", { maximumFractionDigits: 0 });
  return v.toLocaleString("en-IN", { maximumFractionDigits: Math.abs(v) < 10 ? 3 : 1 });
}

export function download(name, blob) {
  const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

export const fileName = (q) => (q || "pantree-insight").toLowerCase().replace(/[^a-z0-9]+/g, "-").slice(0, 60);

export function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

export function scrollDown() { const m = $("#messages"); m.scrollTop = m.scrollHeight; }

export function openPanel() { $("#app").classList.remove("panel-closed"); }

export const userBubble = (text, id = null, edited = false) => `<div class="msg user" ${id ? `data-message-id="${esc(id)}"` : ""}>
  <div class="user-message"><div class="bubble"><span class="user-text">${esc(text)}</span></div>
  ${id ? `<div class="message-actions">${edited ? '<span class="muted">Edited</span>' : ''}<button class="btn small" data-action="edit-message" aria-label="Edit your message">Edit</button></div>` : ""}</div></div>`;

export function statusLine(text) {
  const el = document.createElement("div"); el.className = "msg";
  el.innerHTML = `<div class="answer status-line"><span class="spinner"></span><span class="txt">${esc(text)}</span></div>`;
  return el;
}

export function answerCard(run, replay = false) {
  const el = document.createElement("div"); el.className = "msg";
  if (run.status !== "done") {
    el.innerHTML = `<div class="answer error-box"><h3>${run.status === "cancelled" ? "Cancelled" : run.status === "declined" ? "I can't answer that" : "Something went wrong"}</h3>
      <div>${esc(run.message || "")}</div>
      ${run.status === "failed" && run.question ? `<button class="btn small" data-action="edit-question" data-q="${esc(run.question)}">Edit and retry</button>` : ""}</div>`;
    return el;
  }
  const a = run.analysis || {};
  el.innerHTML = `<div class="answer"><h3>${esc(a.headline)}</h3>
    ${run.analyses?.length > 1 ? `<ul>${run.analyses.map(item => `<li><strong>${esc(item.purpose)}:</strong> ${esc(item.analysis.headline)}</li>`).join("")}</ul>` : ""}
    <ul>${(a.findings || []).slice(0, 3).map((f) => `<li>${esc(f)}</li>`).join("")}</ul>
    <div class="row"><button class="btn small" data-action="show-run">View analysis, chart & SQL</button>
    ${replay ? `<span class="tag replay">${run.offline ? "Offline demo · saved results" : "Replay · recorded run"} · data as of ${esc(run.data_as_of)}</span>` : `<span class="tag">Data as of ${esc(run.data_as_of)}</span>`}
    ${run.trust === "verified" ? `<span class="tag ok" title="Computed by code from the governed metric definitions for the reading you confirmed; no model wrote this SQL">✓ Verified</span>` : run.verification && run.verification.status === "verified" ? `<span class="tag ok" title="Each metric was recomputed from its definition for the confirmed scope and matched">✓ Numbers verified against the data</span>` : run.verification && run.verification.status === "mismatch" ? `<span class="tag warn" title="The recomputed metric disagrees with this result; see the warnings">⚠ Numbers do not match the definition</span>` : run.verification ? `<span class="tag" title="This answer's metric could not be recomputed independently">Not independently verified</span>` : ""}
    ${run.cost?.unknown_cost_calls ? `<span class="tag">Cost partly unknown</span>` : run.cost?.usd ? `<span class="tag">$${run.cost.usd.toFixed(3)}</span>` : ""}
    ${!replay && !run.offline && run.run_id && S.me?.user ? `<span class="feedback-actions"><span class="muted">Was this correct?</span> <button class="btn small" data-action="feedback" data-run="${esc(run.run_id)}" data-rating="positive">👍 Yes</button> <button class="btn small" data-action="feedback" data-run="${esc(run.run_id)}" data-rating="negative">👎 No</button></span>` : ""}</div></div>`;
  el.querySelector('[data-action="show-run"]').addEventListener("click", () => { showRunInPanel(run); openPanel(); });
  return el;
}

export function closeSidebarMobile() { $("#app").classList.remove("sidebar-open"); }

export const autosize = () => { const ta = $("#question"); ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight, 160) + "px"; };
