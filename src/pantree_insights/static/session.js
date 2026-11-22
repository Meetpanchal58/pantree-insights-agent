/* session: explicit ES module dependencies; no build step. */
import { $, S, esc } from './state.js';
import { refreshSidebar, renderWelcome } from './chat.js';
import { api } from './api.js';
import { recoveryCard, toast } from './render.js';
import { loadPulseCount } from './dialogs.js';
import { clearAI, refreshAI, warmAnalyst } from './workspace.js';

export async function init() {
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

export function showSignin() {
  const m = S.me.signin;
  $("#google-btn").hidden = !m.google;
  $("#email-form").hidden = !m.email;
  $("#or-divider").hidden = !(m.google && m.email);
  $("#signin").hidden = false;
}

export function renderUser() {
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
