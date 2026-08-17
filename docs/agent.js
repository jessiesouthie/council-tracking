// Civic Roll Call — "ask about this site" chat widget.
//
// A launcher button + slide-up panel that streams answers from the agent
// Worker (see worker/). The Worker holds the API key and does the retrieval;
// this file is just the UI. Loaded on every page by site.js.
//
// Endpoint: set window.CT_AGENT_ENDPOINT before this script runs, or edit the
// fallback below to your deployed Worker URL. See worker/README.md.
(() => {
  "use strict";

  const ENDPOINT =
    (typeof window !== "undefined" && window.CT_AGENT_ENDPOINT) ||
    "https://civic-roll-call-agent.jessieoborn.workers.dev/ask";

  const configured = !/YOUR-SUBDOMAIN/.test(ENDPOINT);

  // Which body the visitor is currently viewing, so answers can be scoped.
  function currentBody() {
    try {
      const p = new URL(location.href).searchParams.get("body");
      if (p) return p;
      return localStorage.getItem("ct_body") || "city-council";
    } catch {
      return "city-council";
    }
  }

  const esc = (s) =>
    String(s ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));

  // Tiny, safe Markdown → HTML for the subset the agent produces: links, bold,
  // inline code, and unordered lists. Everything is escaped first; only http(s)
  // and in-site relative links are allowed to become anchors.
  function renderMarkdown(md) {
    const inline = (t) =>
      esc(t)
        .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, text, url) => {
          // `url` is already HTML-escaped (esc ran over the whole string first),
          // so it's safe to drop straight into the attribute — re-escaping here
          // would double-encode the & in ...?id=1&body=... and break the link.
          const ok = /^(https?:\/\/|[\w.\-]+\.html|\.?\/)/.test(url);
          return ok ? `<a href="${url}">${text}</a>` : text;
        })
        .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
        .replace(/`([^`]+)`/g, "<code>$1</code>");

    const out = [];
    let list = null;
    for (const raw of md.split("\n")) {
      const line = raw.replace(/\s+$/, "");
      const li = line.match(/^\s*[-*]\s+(.*)/);
      if (li) {
        if (!list) { list = []; }
        list.push(`<li>${inline(li[1])}</li>`);
        continue;
      }
      if (list) { out.push(`<ul>${list.join("")}</ul>`); list = null; }
      if (!line.trim()) continue;
      out.push(`<p>${inline(line)}</p>`);
    }
    if (list) out.push(`<ul>${list.join("")}</ul>`);
    return out.join("");
  }

  function build() {
    const launcher = document.createElement("button");
    launcher.type = "button";
    launcher.className = "agent-launcher";
    launcher.setAttribute("aria-label", "Ask about this site");
    launcher.innerHTML =
      '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M4 5.5A2.5 2.5 0 0 1 6.5 3h11A2.5 2.5 0 0 1 20 5.5v8A2.5 2.5 0 0 1 17.5 16H9l-4 4v-4H6.5A2.5 2.5 0 0 1 4 13.5z"/></svg><span>Ask</span>';

    const panel = document.createElement("div");
    panel.className = "agent-panel";
    panel.hidden = true;
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-label", "Ask Civic Roll Call");
    panel.innerHTML = `
      <div class="agent-head">
        <div class="agent-title">Ask about Eagle Mountain</div>
        <div class="agent-actions">
          <button type="button" class="agent-hbtn agent-min" aria-label="Minimize">
            <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M6 9l6 6 6-6"/></svg>
          </button>
          <button type="button" class="agent-hbtn agent-close" aria-label="Close and clear conversation">&times;</button>
        </div>
      </div>
      <div class="agent-log" tabindex="-1"></div>
      <form class="agent-form">
        <input class="agent-input" type="text" autocomplete="off"
               placeholder="e.g. How did the council vote on the tax increase?"
               aria-label="Your question" maxlength="600" />
        <button type="submit" class="agent-send" aria-label="Send">Send</button>
      </form>`;

    document.body.appendChild(launcher);
    document.body.appendChild(panel);

    const log = panel.querySelector(".agent-log");
    const form = panel.querySelector(".agent-form");
    const input = panel.querySelector(".agent-input");
    let busy = false;

    // Persist the open state + conversation for this browsing session, so an
    // active chat survives navigation between pages. sessionStorage (not local)
    // means a fresh visit starts minimized rather than popping open unbidden.
    const STORE_KEY = "ct_agent_v1";
    let history = []; // [{ r:"u", t } | { r:"b", md, s }]
    function save() {
      try {
        sessionStorage.setItem(
          STORE_KEY,
          JSON.stringify({ open: !panel.hidden, msgs: history })
        );
      } catch {}
    }
    function loadSaved() {
      try {
        return JSON.parse(sessionStorage.getItem(STORE_KEY) || "null");
      } catch {
        return null;
      }
    }

    function open(opts) {
      panel.hidden = false;
      launcher.setAttribute("aria-expanded", "true");
      if (!log.childElementCount) greet();
      if (!opts || !opts.silent) setTimeout(() => input.focus(), 40);
      save();
    }
    // Minimize: tuck the panel back to the launcher, keeping the conversation.
    function minimize() {
      panel.hidden = true;
      launcher.setAttribute("aria-expanded", "false");
      launcher.focus();
      save();
    }
    // Close: minimize AND clear, so reopening starts a fresh conversation.
    function close() {
      log.innerHTML = "";
      history = [];
      minimize();
    }
    launcher.addEventListener("click", () => (panel.hidden ? open() : minimize()));
    panel.querySelector(".agent-min").addEventListener("click", minimize);
    panel.querySelector(".agent-close").addEventListener("click", close);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !panel.hidden) minimize();
    });

    function bubble(who, html) {
      const el = document.createElement("div");
      el.className = `agent-msg agent-${who}`;
      el.innerHTML = html;
      log.appendChild(el);
      log.scrollTop = log.scrollHeight;
      return el;
    }

    function renderSources(botEl, sources) {
      if (!sources || !sources.length) return;
      const seen = new Set();
      const ok = (u) => /^(https?:\/\/|[\w.\-]+\.html|\.?\/)/.test(u);
      const items = sources
        .filter((s) => s.url && ok(s.url) && !seen.has(s.url) && seen.add(s.url))
        .slice(0, 4)
        // Some older motions carry a whole ordinance as their title; the Worker
        // clips them, but don't trust a source label to be link-sized.
        .map((s) => {
          const t = String(s.title || "");
          const label = t.length > 80 ? t.slice(0, 78).replace(/\s+\S*$/, "") + "…" : t;
          return `<a href="${esc(s.url)}">${esc(label)}</a>`;
        })
        .join("");
      const src = document.createElement("div");
      src.className = "agent-sources";
      src.innerHTML = `<span>Sources</span>${items}`;
      botEl.appendChild(src);
    }

    // Repaint a saved conversation into the panel (markdown is re-rendered
    // through the same safe path, so nothing untrusted is inserted as HTML).
    function restore(msgs) {
      for (const m of msgs || []) {
        if (m.r === "u") bubble("user", esc(m.t));
        else renderSources(bubble("bot", renderMarkdown(m.md || "")), m.s);
      }
    }

    function greet() {
      bubble(
        "bot",
        renderMarkdown(
          "Hi — I can answer questions about Eagle Mountain's council: motions, " +
            "how members voted, meetings, the budget, and the proposed tax change. " +
            "Ask me anything on the site."
        )
      );
    }

    // The conversation so far, in the shape the Worker replays to the model.
    // Without this every question arrives cold and follow-ups like "what about
    // last year?" have nothing to attach to. Trimmed to the recent tail — the
    // Worker caps it again, but no reason to ship the whole log every time.
    function priorTurns() {
      return history.slice(-6).map((m) =>
        m.r === "u"
          ? { role: "user", text: m.t }
          : { role: "assistant", text: m.md }
      );
    }

    async function ask(question) {
      if (busy) return;
      busy = true;
      input.value = "";
      const prior = priorTurns();
      bubble("user", esc(question));
      history.push({ r: "u", t: question });
      save();
      const botEl = bubble("bot", '<span class="agent-typing"><i></i><i></i><i></i></span>');

      if (!configured) {
        botEl.innerHTML = renderMarkdown(
          "The assistant isn't connected yet. (Set the Worker endpoint in " +
            "`agent.js` — see `worker/README.md`.)"
        );
        busy = false;
        return;
      }

      let answer = "";
      let sources = [];
      try {
        const resp = await fetch(ENDPOINT, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ question, body: currentBody(), history: prior }),
        });
        if (!resp.ok || !resp.body) {
          const e = await resp.json().catch(() => ({}));
          throw new Error(e.error || `request failed (${resp.status})`);
        }
        const reader = resp.body.getReader();
        const dec = new TextDecoder();
        let buf = "";
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          const parts = buf.split("\n\n");
          buf = parts.pop();
          for (const part of parts) {
            let ev = "message", data = "";
            for (const line of part.split("\n")) {
              if (line.startsWith("event:")) ev = line.slice(6).trim();
              else if (line.startsWith("data:")) data += line.slice(5).trim();
            }
            if (!data) continue;
            let payload;
            try { payload = JSON.parse(data); } catch { continue; }
            if (ev === "delta") {
              answer += payload.text;
              botEl.innerHTML = renderMarkdown(answer);
              log.scrollTop = log.scrollHeight;
            } else if (ev === "sources") {
              sources = payload.sources || [];
            } else if (ev === "error") {
              throw new Error(payload.message || "stream error");
            }
          }
        }
        const finalMd = answer || "_(no answer)_";
        botEl.innerHTML = renderMarkdown(finalMd);
        renderSources(botEl, sources);
        history.push({ r: "b", md: finalMd, s: sources });
      } catch (err) {
        const errMd =
          "Sorry — something went wrong (" + esc(err.message) + "). Please try again.";
        botEl.innerHTML = renderMarkdown(errMd);
        history.push({ r: "b", md: errMd, s: [] });
      } finally {
        busy = false;
        save();
        log.scrollTop = log.scrollHeight;
      }
    }

    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const q = input.value.trim();
      if (q) ask(q);
    });

    // Rehydrate this session's conversation + open state across page loads.
    const saved = loadSaved();
    if (saved) {
      history = Array.isArray(saved.msgs) ? saved.msgs : [];
      restore(history);
      if (saved.open) open({ silent: true });
    }
  }

  if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", build);
  else build();
})();
