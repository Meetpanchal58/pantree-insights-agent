import './workspace.js';
/* app: explicit ES module dependencies; no build step. */
import { ask, editMessage, openChat, refreshSidebar, renderSidebar, renderWelcome } from './chat.js';
import { $, OFFLINE_DEMOS, S } from './state.js';
import { autosize, closeSidebarMobile, toast } from './render.js';
import { aiDialog, backToChat, openDataset } from './workspace.js';
import { init, showSignin } from './session.js';
import { api } from './api.js';
import { chatMenu, closeDialog, emailSubmit, memoryDialog, projectDialog, pulseDialog } from './dialogs.js';
import { replayDemo } from './plan.js';
import { drawChart } from './results.js';

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

export * from './state.js';
export * from './api.js';
export * from './render.js';
export * from './chat.js';
export * from './plan.js';
export * from './results.js';
export * from './dialogs.js';
export * from './workspace.js';
export * from './session.js';
