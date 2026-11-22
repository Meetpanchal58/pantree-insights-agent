import './offline-demos.js';
/* state: explicit ES module dependencies; no build step. */


export const STEP_LABELS = {
  queue: "Waiting for a free slot", prepare: "Preparing the data (first question after a restart)", check_input: "Checking the question", interpret: "Understanding the question",
  execute: "Running the verified query", analyse: "Explaining the result", reflect: "Choosing the next investigation step", chart: "Building the chart",
  context: "Checking relevant context", remember: "Saving to memory",
  retrieve: "Finding relevant data", understand: "Understanding the question", plan: "Planning the analysis",
  write_sql: "Writing SQL", check_sql: "Safety checks", run_query: "Running the query", validate: "Reviewing the query",
  offline_demo: "Loading saved analysis",
};

export const STATUS_ICON = { done: "✓", retry: "↻", failed: "✕", blocked: "✕", warning: "!", needs_input: "?", running: "" };

export const OFFLINE_DEMOS = window.PANTREE_DEMOS || [];

export const S = { me: null, projects: [], chats: [], chatId: null, expanded: new Set(), demoMode: false,
            run: null, pending: null, busy: false, pendingProject: null, view: 0, stream: null, chatRequest: null, demos: OFFLINE_DEMOS,
            chatCache: new Map(), sidebarRequest: 0 };

export const $ = (sel) => document.querySelector(sel);

export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
