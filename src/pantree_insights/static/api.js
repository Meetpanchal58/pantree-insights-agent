/* api: explicit ES module dependencies; no build step. */
import { init } from './session.js';

export async function api(path, opts = {}) {
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

export const scripts = new Map();

export function loadScript(url, globalName) {
  if (window[globalName]) return Promise.resolve();
  if (!scripts.has(url)) scripts.set(url, new Promise((resolve, reject) => {
    const script = document.createElement("script"); script.src = url;
    script.onload = resolve;
    script.onerror = () => { script.remove(); scripts.delete(url); reject(new Error("Could not load " + globalName)); };
    document.head.appendChild(script);
  }));
  return scripts.get(url);
}
