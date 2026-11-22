/* dialogs: explicit ES module dependencies; no build step. */
import { $, S, esc } from './state.js';
import { api } from './api.js';
import { toast } from './render.js';
import { refreshSidebar, renderWelcome } from './chat.js';
import { showSignin } from './session.js';

export let dialogVersion = 0;

export function dialog(html) { dialogVersion += 1; $("#dialog-body").innerHTML = html; $("#dialog").hidden = false; return $("#dialog-body"); }

export function closeDialog() { dialogVersion += 1; $("#dialog").hidden = true; }

export async function dialogData(title, path) {
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

export function projectDialog(project) {
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

export async function memoryDialog() {
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

export async function loadPulseCount() {
  try {
    const p = await api("/api/pulse");
    const open = p.flags.filter((f) => !f.explained && !f.resolved).length;
    $("#pulse-count").textContent = open; $("#pulse-count").hidden = !open;
  } catch { /* pulse not ready yet */ }
}

export async function pulseDialog() {
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

export function chatMenu(id) {
  const chat = S.chats.find((c) => c.id === id); if (!chat) return;
  const d = dialog(`<h2>Chat</h2><label>Title</label><input id="c-title" maxlength="200" value="${esc(chat.title)}">
    <div class="dialog-actions"><button class="btn danger" data-del>Delete chat</button><span style="flex:1"></span>
    <button class="btn" data-close>Cancel</button><button class="btn primary" data-save>Save</button></div>`);
  d.querySelector("[data-close]").onclick = closeDialog;
  d.querySelector("[data-save]").onclick = async () => { await api(`/api/chats/${id}`, { method: "PATCH", body: { title: $("#c-title").value } }); closeDialog(); refreshSidebar(); if (S.chatId === id) $("#chat-title").textContent = $("#c-title").value; };
  d.querySelector("[data-del]").onclick = async () => { await api(`/api/chats/${id}`, { method: "DELETE", body: {} }); closeDialog(); S.chatCache.delete(id); if (S.chatId === id) renderWelcome(); refreshSidebar(); };
}

export async function emailSubmit(ev) {
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
