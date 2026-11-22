/* chat: explicit ES module dependencies; no build step. */
import { displayActiveModel } from './workspace.js';
import { $, S, esc } from './state.js';
import { api } from './api.js';
import { answerCard, autosize, closeSidebarMobile, recoveryCard, scrollDown, statusLine, toast, userBubble } from './render.js';
import { conversationMessage, newTimeline, showRunInPanel, streamRun } from './plan.js';
import { showSignin } from './session.js';

export function leaveView() {
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

export async function refreshSidebar() {
  if (!S.me.user) return;
  const request = ++S.sidebarRequest;
  try {
    const [projects, chats] = await Promise.all([api("/api/projects"), api("/api/chats")]);
    if (request !== S.sidebarRequest) return;
    S.projects = projects; S.chats = chats;
    renderSidebar();
  } catch (e) { if (request === S.sidebarRequest) toast(e.message); }
}

export function renderSidebar() {
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

export async function renderWelcome() {
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

export async function openChat(id, force = false) {
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

export function renderChat(chat, connect = false) {
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

export function editMessage(element) {
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

export async function ask(question) {
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
