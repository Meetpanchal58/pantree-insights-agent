"use strict";
/* Pantree Insights front end: no framework, no build step. */

const STEP_LABELS = {
  queue: "Waiting for a free slot", prepare: "Preparing the data (first question after a restart)", check_input: "Checking the question", interpret: "Understanding the question",
  execute: "Running the verified query", analyse: "Explaining the result", reflect: "Choosing the next investigation step", chart: "Building the chart",
  context: "Checking relevant context", remember: "Saving to memory",
  retrieve: "Finding relevant data", understand: "Understanding the question", plan: "Planning the analysis",
  write_sql: "Writing SQL", check_sql: "Safety checks", run_query: "Running the query", validate: "Reviewing the query",
  offline_demo: "Loading saved analysis",
};
const STATUS_ICON = { done: "✓", retry: "↻", failed: "✕", blocked: "✕", warning: "!", needs_input: "?", running: "" };
const OFFLINE_DEMOS = window.PANTREE_DEMOS || [];

const S = { me: null, projects: [], chats: [], chatId: null, expanded: new Set(), demoMode: false,
            run: null, pending: null, busy: false, pendingProject: null, view: 0, stream: null, chatRequest: null, demos: OFFLINE_DEMOS,
            chatCache: new Map(), sidebarRequest: 0 };
const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* ---------------------------------------------------------------- helpers */
async function api(path, opts = {}) {
  const controller = new AbortController();
  const cancel = () => controller.abort();
  if (opts.signal?.aborted) controller.abort();
  opts.signal?.addEventListener("abort", cancel, { once: true });
  let timedOut = false;
  const timer = setTimeout(() => { timedOut = true; controller.abort(); }, opts.timeoutMs ?? 30000);
  const init = { method: opts.method || "GET", headers: {}, credentials: "same-origin", signal: controller.signal };
  if (opts.body !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(opts.body); }
  try {
    let res;
    // Retry reads only: replaying a timed-out POST could submit a paid question twice.
    const retries = init.method === "GET" ? (opts.retries ?? 2) : 0;
    for (let attempt = 0; ; attempt++) {
      res = await fetch(path, init);
      if (![429, 502, 503, 504].includes(res.status) || attempt >= retries) break;
      const header = res.headers.get("Retry-After");
      const seconds = header && /^\d+(\.\d+)?$/.test(header) ? Number(header)
        : header ? Math.max(0, (Date.parse(header) - Date.now()) / 1000) : NaN;
      const wait = Number.isFinite(seconds) ? seconds * 1000 : 500 * (2 ** attempt);
      // Do not retry earlier than a long server cooldown, or leave a loading spinner indefinitely.
      if (wait > 10000) break;
      await new Promise((resolve, reject) => {
        const abort = () => { clearTimeout(delay); reject(new DOMException("Cancelled", "AbortError")); };
        const delay = setTimeout(() => { controller.signal.removeEventListener("abort", abort); resolve(); }, wait);
        controller.signal.addEventListener("abort", abort, { once: true });
        if (controller.signal.aborted) abort();
      });
    }
    let data;
    try { data = await res.json(); }
    catch (error) {
      if (error.name === "AbortError") throw error;
      if (res.ok) throw new Error("The server returned an unreadable response. Please retry loading.");
      data = {};
    }
    if (res.ok && (!data || typeof data !== "object")) throw new Error("The server returned an unexpected response.");
    if (!res.ok) {
      const message = res.status === 429 ? (typeof data.detail === "string" ? data.detail : "Too many requests. Please wait and try again; existing chats are kept.")
        : [502, 503, 504].includes(res.status) ? "The server is temporarily unavailable. Your chat is saved; try again."
        : (typeof data.detail === "string" ? data.detail : `Request failed (${res.status})`);
      const error = new Error(message); error.status = res.status;
      const retry = res.headers.get("Retry-After");
      error.retryAfter = retry && /^\d+(\.\d+)?$/.test(retry) ? Number(retry)
        : retry ? Math.max(0, (Date.parse(retry) - Date.now()) / 1000) : 0;
      throw error;
    }
    return data;
  } catch (error) {
    if (timedOut) throw new Error("The request timed out. Your chat is saved. Reopen it to check the result before retrying.");
    throw error;
  } finally {
    clearTimeout(timer); opts.signal?.removeEventListener("abort", cancel);
  }
}
function recoveryCard(message, action = "reopen-chat", retryAfter = 0) {
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

function toast(msg, ms = 2600) {
  const t = $("#toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toast.timer); toast.timer = setTimeout(() => (t.hidden = true), ms);
}
function fmtNum(v, col = "") {
  if (v === null || v === undefined || v === "") return '<span class="muted" title="No value">—</span>';
  if (typeof v !== "number") return esc(v);
  if (/roas/.test(col)) return v.toLocaleString("en-IN", { maximumFractionDigits: 1 }) + "×";
  if (/rate|share|pct|ctr|conversion|margin|lift|growth/.test(col) && Math.abs(v) <= 1.5) return (v * 100).toFixed(1) + "%";
  const volume = /(^|_)(orders?|lines?|sessions|count|units|quantity|qty|customers|buyers|tickets|reviews|impressions|contacts|visits)(_|$)/.test(col);
  if (!volume && /revenue|sales|spend|aov|amount|cost|cac|value|price/.test(col)) return "₹" + v.toLocaleString("en-IN", { maximumFractionDigits: 0 });
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

// Navigation owns its requests and streams; late responses must not change a newer view.
function leaveView() {
  if (typeof displayActiveModel === "function") displayActiveModel(null);
  S.view += 1;
  S.chatRequest?.abort(); S.chatRequest = null;
  S.stream?.close(); S.stream = null;
  S.busy = false; $(".send").disabled = false;
  $("#question").disabled = false;
  $("#question").placeholder = "Ask about sales, returns, marketing, stock, customers…";
  S.run = null; S.pending = null;
  if ($("#chart") && window.Plotly) window.Plotly.purge($("#chart"));
  $("#panel-body").innerHTML = ""; $("#panel-sub").textContent = "";
  return S.view;
}
const scripts = new Map();
function loadScript(url, globalName) {
  if (window[globalName]) return Promise.resolve();
  if (!scripts.has(url)) scripts.set(url, new Promise((resolve, reject) => {
    const script = document.createElement("script"); script.src = url;
    script.onload = resolve;
    script.onerror = () => { script.remove(); scripts.delete(url); reject(new Error("Could not load " + globalName)); };
    document.head.appendChild(script);
  }));
  return scripts.get(url);
}

/* ---------------------------------------------------------------- start */
async function init() {
  if (window.innerWidth <= 1180) $("#app").classList.add("panel-closed");
  if (!S.me) {
    S.me = { user: null, signin: {}, data: null };
    $("#signin").hidden = true;
    renderUser(); renderWelcome();
  }
  const view = S.view;
  try { S.me = await api("/api/me"); }
  catch (e) {
    if (view !== S.view) { if (!S.run?.replay) toast(e.message); return; }
    if (!S.me) S.me = { user: null, signin: {}, data: null };
    S.demoMode = true; $("#signin").hidden = true;
    renderUser(); renderWelcome();
    $("#messages").prepend(recoveryCard(e.message, "retry-start", e.retryAfter)); return;
  }
  if (new URLSearchParams(location.search).get("signin") === "failed") toast("Sign-in did not complete. Please try again.");
  if (!S.me.user) {
    $("#signin").hidden = true; renderUser();
    if (view === S.view) renderWelcome();
    return;
  }
  $("#signin").hidden = true;
  renderUser();
  refreshSidebar();
  if (view === S.view) renderWelcome();
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
    if (typeof clearAI === "function") clearAI();
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
  if (typeof refreshAI === "function") refreshAI();
  if (typeof warmAnalyst === "function") warmAnalyst();
  const l = S.me.limit;
  $("#limit-info").textContent = l.unlimited ? "Admin: unlimited questions" : `${l.used}/${l.per_day} live questions today`;
}

/* ---------------------------------------------------------------- sidebar */
async function refreshSidebar() {
  if (!S.me.user) return;
  const request = ++S.sidebarRequest;
  try {
    const [projects, chats] = await Promise.all([api("/api/projects"), api("/api/chats")]);
    if (request !== S.sidebarRequest) return;
    S.projects = projects; S.chats = chats;
    renderSidebar();
  } catch (e) { if (request === S.sidebarRequest) toast(e.message); }
}
function renderSidebar() {
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
  const view = leaveView();
  S.chatId = null;
  renderSidebar();
  $("#chat-title").textContent = S.pendingProject ? `New chat in ${S.projects.find((p) => p.id === S.pendingProject)?.name || "project"}` : "New chat";
  $("#move-project").hidden = true;
  const paint = (demos) => {
    const d = S.me && S.me.data;
    $("#messages").innerHTML = `<div class="welcome">
        <h2>What would you like to know?</h2>
        <p class="muted">Ask about Pantree's sales, returns, delivery, marketing, stock or customers.
        Data: ${d ? `${esc(d.first_day)} to ${esc(d.last_day)} (${esc(d.size)} shop, simulated)` : ""}.</p>
        ${demos.length ? `<div class="section-label">Four offline business demos</div><p class="muted">Saved sample-data results, charts and SQL. No live model calls.</p><div class="cards">${demos.map((x) => `
          <button class="qcard" data-action="replay" data-slug="${esc(x.slug)}"><div class="kind">${esc(x.topic || "Offline demo")}</div>${esc(x.question)}<div class="muted">${esc(x.data_size)} sample · ${esc(x.data_as_of)}</div></button>`).join("")}</div>` : ""}
      </div>`;
  };
  paint(S.demos || []);
  if (!S.demos) {
    try {
      S.demos = await api("/api/demos");
      if (view === S.view) paint(S.demos);
    } catch (e) { if (view === S.view) toast(e.message); }
  }
}

async function openChat(id, force = false) {
  if (!force && S.chatId === id && S.chatRequest) return;
  const view = leaveView();
  S.chatId = id; S.pendingProject = null;
  closeSidebarMobile(); renderSidebar();
  $("#chat-title").textContent = S.chats.find((c) => c.id === id)?.title || "Loading chat…";
  $("#messages").replaceChildren(statusLine("Loading chat…"));
  S.busy = true; $(".send").disabled = true;
  const cached = S.chatCache.get(id);
  if (cached) {
    S.busy = false; $(".send").disabled = false;
    renderChat(cached);
    if (!force && !cached.pending_run && Date.now() - (cached.cachedAt || 0) < 15000) return;
  }
  const controller = new AbortController(); S.chatRequest = controller;
  try {
    const chat = await api(`/api/chats/${id}`, { signal: controller.signal });
    if (view !== S.view) return;
    chat.cachedAt = Date.now();
    S.chatCache.delete(id); S.chatCache.set(id, chat);
    if (S.chatCache.size > 10) S.chatCache.delete(S.chatCache.keys().next().value);
    S.busy = false; $(".send").disabled = false;
    renderChat(chat, true);
  } catch (e) {
    if (view !== S.view || e.name === "AbortError") return;
    S.busy = false; $(".send").disabled = false;
    if (cached) {
      const warning = recoveryCard("Showing the last loaded conversation. " + e.message, "reopen-chat", e.retryAfter);
      warning.classList.add("read-warning"); $("#messages").prepend(warning);
    } else $("#messages").replaceChildren(recoveryCard(e.message, "reopen-chat", e.retryAfter));
  } finally { if (S.chatRequest === controller) S.chatRequest = null; }
}

function renderChat(chat, connect = false) {
  $("#chat-title").textContent = chat.title;
  const box = $("#messages"); box.innerHTML = "";
  let last = null;
  for (const m of chat.messages) {
    if (m.role === "user") box.insertAdjacentHTML("beforeend", userBubble(m.content, m.id, !!(m.run?.edits?.length || m.run?.original !== undefined)));
    else if (m.run?.superseded_by_edit) {
      const prior = document.createElement('details'); prior.className = 'msg previous-reply';
      prior.innerHTML = `<summary>Previous reply (before edit)</summary><div class="answer">${esc(m.content).replace(/\n/g,'<br>')}</div>`;
      box.appendChild(prior);
    }
    else if (m.run?.type === "clarify") { box.appendChild(conversationMessage(m.run)); }
    else { box.appendChild(answerCard(m.run || {})); last = m.run; }
  }
  if (chat.interrupted_run) {
    box.appendChild(recoveryCard("The server restarted during this analysis. The unfinished response was not saved; you can submit a new question.", "reopen-chat"));
  }
  if (connect && chat.pending_run) {
    const el = statusLine("Working on it…"); box.appendChild(el);
    const question = [...chat.messages].reverse().find((m) => m.role === "user")?.content;
    newTimeline(question, false);
    streamRun(chat.pending_run, chat.pending_since || 0, el, question);
  }
  else if (last) showRunInPanel(last);
  renderSidebar(); scrollDown();
}

const userBubble = (text, id = null, edited = false) => `<div class="msg user" ${id ? `data-message-id="${esc(id)}"` : ""}>
  <div class="user-message"><div class="bubble"><span class="user-text">${esc(text)}</span></div>
  ${id ? `<div class="message-actions">${edited ? '<span class="muted">Edited</span>' : ''}<button class="btn small" data-action="edit-message" aria-label="Edit your message">Edit</button></div>` : ""}</div></div>`;

function editMessage(element) {
  if (!element || S.busy || S.run?.replay) return toast('Wait for the current response before editing.');
  if (element.querySelector('.message-editor')) return;
  const chatId = S.chatId, id = element.dataset.messageId;
  const original = element.querySelector('.user-text').textContent;
  const latest = [...document.querySelectorAll('.msg.user[data-message-id]')].at(-1) === element;
  const pending = S.pending;
  const inline = latest && pending;
  const editor = document.createElement('div'); editor.className = 'message-editor';
  editor.innerHTML = `<label>Edit your message<textarea maxlength="2000" aria-label="Edit your message"></textarea></label>
    <p class="muted">${inline ? 'Send the corrected reply to the pending analysis. If this is the original question, an edited copy will be created.' : 'Creates an edited copy from this point. Later replies will be regenerated; the original chat stays available.'}</p>
    <div class="edit-error" role="alert"></div><div class="row"><button class="btn" data-edit-cancel>Cancel</button><button class="btn primary" data-edit-save>Save & send</button></div>`;
  element.querySelector('.user-message').appendChild(editor);
  const input = editor.querySelector('textarea'), save = editor.querySelector('[data-edit-save]');
  input.value = original; input.focus(); input.setSelectionRange(input.value.length, input.value.length);
  editor.querySelector('[data-edit-cancel]').onclick = () => editor.remove();
  let retryKey = null, lastContent = null;
  save.onclick = async () => {
    const content = input.value.trim();
    if (!content) { editor.querySelector('.edit-error').textContent = 'Enter a message before saving.'; return; }
    if (content === original) { editor.remove(); return; }
    if (S.chatId !== chatId || S.busy) return;
    if (content !== lastContent) { retryKey = crypto.randomUUID(); lastContent = content; }
    save.disabled = true; input.disabled = true; editor.querySelector('[data-edit-cancel]').disabled = true;
    S.busy = true; $('.send').disabled = true;
    editor.querySelector('.edit-error').textContent = '';
    try {
      const result = await api(`/api/chats/${chatId}/messages/${id}/edit`, {method:'POST', body:{
        content, expected_content:original, idempotency_key:retryKey,
        ...(pending ? {pending_run_id:pending.run_id, plan_version:pending.plan_version} : {})}});
      S.chatCache.delete(chatId); S.chatCache.delete(result.chat_id);
      if (S.chatId === chatId && editor.isConnected) { await openChat(result.chat_id, true); refreshSidebar(); }
    } catch (error) {
      if (editor.isConnected) { editor.querySelector('.edit-error').textContent = error.message; save.disabled = false;
        input.disabled = false; editor.querySelector('[data-edit-cancel]').disabled = false; }
    } finally { if (S.chatId === chatId && editor.isConnected) { S.busy = false; $('.send').disabled = false; } }
  };
  input.addEventListener('keydown', e => {if(e.key === 'Escape' && !save.disabled) {e.preventDefault();editor.remove();}});
}
function statusLine(text) {
  const el = document.createElement("div"); el.className = "msg";
  el.innerHTML = `<div class="answer status-line"><span class="spinner"></span><span class="txt">${esc(text)}</span></div>`;
  return el;
}

function answerCard(run, replay = false) {
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

/* ---------------------------------------------------------------- asking */
async function ask(question) {
  question = question.trim();
  if (S.run?.replay || S.busy || !question.trim()) return false;
  if (!S.me.user) { showSignin(); return; }
  const view = ++S.view;
  S.busy = true; $(".send").disabled = true;
  try {
    if (!S.chatId) {
      const chat = await api("/api/chats", { method: "POST", body: { project_id: S.pendingProject } });
      if (view !== S.view) { refreshSidebar(); return; }
      S.chatId = chat.id; S.pendingProject = null;
      $("#messages").innerHTML = "";
      if (chat.project_id) S.expanded.add(chat.project_id);
    }
    $("#messages").insertAdjacentHTML("beforeend", userBubble(question));
    S.chatCache.delete(S.chatId);
    const sentBubble = $("#messages").lastElementChild;
    const status = statusLine("Starting…"); $("#messages").appendChild(status); scrollDown();
    const pending = S.pending;
    const { run_id, since = 0, message_id } = await api(`/api/chats/${S.chatId}/ask`, { method: "POST", body: {
      question, idempotency_key: crypto.randomUUID(),
      ...(pending ? { pending_run_id: pending.run_id, plan_version: pending.plan_version } : {}) } });
    if (view !== S.view) { refreshSidebar(); return; }
    if (message_id && sentBubble.isConnected) sentBubble.outerHTML = userBubble(question, message_id);
    if (!pending) $("#chat-title").textContent = question.slice(0, 80);
    refreshSidebar();
    S.pending = null;
    streamRun(run_id, since, status, question);
  } catch (e) {
    if (view !== S.view) return;
    toast(e.message, 4500); S.busy = false; $(".send").disabled = false;
    if (!$("#question").value) { $("#question").value = question; autosize(); }
    document.querySelectorAll(".status-line").forEach((n) => n.closest(".msg").remove());
    $("#messages").appendChild(recoveryCard(e.message));
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

function streamRun(runId, since, status, question) {
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

function proposalKey(e) { return `${e.run_id || ""}:${e.plan_version || 0}:${e.message || e.readback || ""}`; }
function conversationMessage(e) {
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

/* ---------------------------------------------------------------- demos */
function educationalDemoEvents(demo) {
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

function replayDemo(slug, question) {
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

/* ---------------------------------------------------------------- panel: steps and result */
function stepDetail(s) {
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
function renderSteps() {
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

function showRunInPanel(run) {
  newTimeline(run.question || $("#chat-title").textContent, !!run.replay);
  S.run.restoring = true;
  for (const e of run.events || []) if (["step", "answered"].includes(e.type)) onEvent(e, null);
  renderSteps();
  showResult(run);
  S.run.restoring = false;
  $("#panel-body").scrollTop = 0;
}

function showResult(run, selected = 0) {
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

async function drawChart(chart) {
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

/* ---------------------------------------------------------------- dialogs: projects, memory, pulse */
let dialogVersion = 0;
function dialog(html) { dialogVersion += 1; $("#dialog-body").innerHTML = html; $("#dialog").hidden = false; return $("#dialog-body"); }
function closeDialog() { dialogVersion += 1; $("#dialog").hidden = true; }
async function dialogData(title, path) {
  const d = dialog(`<h2>${esc(title)}</h2><p role="status">Loading…</p><button class="btn" data-close>Close</button>`);
  const version = dialogVersion;
  d.querySelector("[data-close]").onclick = closeDialog;
  try {
    const data = await api(path);
    return version === dialogVersion && !$("#dialog").hidden ? data : null;
  } catch (e) {
    if (version === dialogVersion) { d.querySelector("p").textContent = "Could not load. Close and try again."; toast(e.message); }
    return null;
  }
}

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
  const m = await dialogData("Memory", "/api/memory");
  if (!m) return;
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
  const p = await dialogData("Pulse", "/api/pulse");
  if (!p) return;
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
  d.querySelector("[data-del]").onclick = async () => { await api(`/api/chats/${id}`, { method: "DELETE", body: {} }); closeDialog(); S.chatCache.delete(id); if (S.chatId === id) renderWelcome(); refreshSidebar(); };
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
    case "edit-message": editMessage(t.closest(".msg.user")); break;
    case "edit-question": $("#question").value = t.dataset.q; autosize(); $("#question").focus(); break;
    case "dataset": openDataset(); break;
    case "back-chat": backToChat(); break;
    case "ai-settings": aiDialog(); break;
    case "retry-start": init(); break;
    case "reopen-chat": if (S.chatId) openChat(S.chatId, true); else renderWelcome(); break;
    case "feedback": {
      if (!S.chatId || !t.dataset.run) break;
      const negative = t.dataset.rating === "negative";
      const category = negative ? prompt("What went wrong? metric / filter / period / SQL / data / interpretation / other", "interpretation") : "correct";
      if (category === null) break;
      const comment = negative ? prompt("Optional explanation (avoid personal information)", "") : "";
      try {
        await api("/api/feedback", { method: "POST", body: {
          chat_id: S.chatId, run_id: t.dataset.run, rating: t.dataset.rating,
          category: (category || "other").toLowerCase().slice(0, 40),
          comment: (comment || "").slice(0, 1000)
        } });
        t.closest(".feedback-actions")?.replaceChildren(document.createTextNode("Feedback saved"));
      } catch (e) { toast(e.message); }
      break;
    }
    case "new-chat": S.pendingProject = t.dataset.project || null; closeSidebarMobile(); renderWelcome(); $("#question").focus(); break;
    case "open-chat": openChat(id); break;
    case "chat-menu": chatMenu(id); break;
    case "toggle-project": S.expanded.has(id) ? S.expanded.delete(id) : S.expanded.add(id); renderSidebar(); break;
    case "new-project": if (!S.me.user) return showSignin(); projectDialog(null); break;
    case "edit-project": projectDialog(S.projects.find((p) => p.id === id)); break;
    case "replay": replayDemo(t.dataset.slug, OFFLINE_DEMOS.find((d) => d.slug === t.dataset.slug)?.question || t.textContent.trim()); break;
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
      if (S.run && S.run.final) S.chartReady = drawChart(S.run.final.chart);
      break;
    }
    case "open-sidebar": $("#app").classList.add("sidebar-open"); break;
    case "close-sidebar": closeSidebarMobile(); break;
  }
});
$("#dialog").addEventListener("click", (ev) => { if (ev.target.id === "dialog") closeDialog(); });
document.addEventListener("keydown", (ev) => { if (ev.key === "Escape" && !$("#dialog").hidden) closeDialog(); });
$("#composer").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const q = $("#question").value.trim();
  if (!q || S.run?.replay) return;
  if (S.busy) { toast("The current chat is still loading. Your question stays in the input."); return; }
  if (!S.me?.user) { showSignin(); return; }
  $("#question").value = ""; autosize(); ask(q);
});
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
