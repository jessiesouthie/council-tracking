/**
 * Civic Roll Call — "ask about this site" agent (Cloudflare Worker).
 *
 * The public site is static (GitHub Pages), so it has nowhere safe to keep an
 * Anthropic API key. This Worker is that safe place: it holds the key as a
 * secret, retrieves the most relevant chunks from docs/agent-corpus.json for a
 * visitor's question, and makes ONE streaming Claude call grounded on them —
 * then pipes the answer back to the browser as it's written.
 *
 * Retrieval is plain keyword scoring (BM25-lite) over the corpus, not an
 * embeddings/vector store: the corpus is small and domain-specific, so this is
 * accurate enough and keeps the interim deployment free of extra services.
 *
 * Config (Worker vars / secrets — see wrangler.toml and the README):
 *   ANTHROPIC_API_KEY  (secret, required)  the paid API key
 *   CORPUS_URL         (var, optional)      where to fetch the corpus JSON
 *   ALLOWED_ORIGIN     (var, optional)      site origin for CORS
 *   MODEL              (var, optional)      Claude model id
 */

const DEFAULTS = {
  CORPUS_URL: "https://civicrollcall.com/agent-corpus.json",
  ALLOWED_ORIGIN: "https://civicrollcall.com",
  // Sonnet 5 is the sweet spot for a high-volume public widget: near-Opus
  // quality on grounded Q&A at a fraction of the cost and latency. Swap to
  // claude-opus-4-8 by setting the MODEL var if you want maximum quality.
  MODEL: "claude-sonnet-5",
};

const MAX_QUESTION_CHARS = 600;
const TOP_K = 12; // chunks of context fed to the model
const MAX_TOKENS = 1024;

// ---- corpus + retrieval -------------------------------------------------

// Cached per isolate: the parsed corpus and a precomputed token index.
let CORPUS = null; // { docs, idf: Map, byId }
let CORPUS_PROMISE = null;

const STOP = new Set(
  ("a an the of to in on for and or but is are was were be been being at by " +
    "with from this that these those it its as we you they he she i do does " +
    "did has have had will would can could should what which who whom when " +
    "where why how not no yes about into over under out up down off " +
    // Domain stopwords: this whole site is Eagle Mountain, Utah, so the
    // jurisdiction name heads nearly every motion and never discriminates
    // between them — drop it so scoring keys on the actual question.
    "eagle mountain city utah").split(" ")
);

function tokenize(text) {
  return (String(text || "").toLowerCase().match(/[a-z0-9]+/g) || []).filter(
    (t) => t.length > 1 && !STOP.has(t)
  );
}

async function loadCorpus(env) {
  if (CORPUS) return CORPUS;
  if (CORPUS_PROMISE) return CORPUS_PROMISE;
  const url = env.CORPUS_URL || DEFAULTS.CORPUS_URL;
  CORPUS_PROMISE = (async () => {
    const r = await fetch(url, { cf: { cacheTtl: 300, cacheEverything: true } });
    if (!r.ok) throw new Error(`corpus fetch failed: ${r.status}`);
    const raw = await r.json();
    const docs = raw.docs.map((d) => ({ ...d, _tokens: tokenize(d.title + " " + d.text) }));
    // Document frequency → BM25 inverse document frequency.
    const df = new Map();
    for (const d of docs) {
      for (const t of new Set(d._tokens)) df.set(t, (df.get(t) || 0) + 1);
    }
    const N = docs.length;
    const idf = new Map();
    for (const [t, n] of df) {
      // BM25 idf, floored at a small positive value so that a query whose only
      // content word is common (e.g. "who is on the city council") still ranks
      // something instead of scoring everything zero.
      idf.set(t, Math.max(0.3, Math.log(1 + (N - n + 0.5) / (n + 0.5))));
    }
    const avgdl = docs.reduce((s, d) => s + d._tokens.length, 0) / (N || 1);
    CORPUS = { docs, idf, avgdl, bodies: raw.bodies || [] };
    return CORPUS;
  })();
  return CORPUS_PROMISE;
}

const K1 = 1.2;
const B = 0.75;

function retrieve(corpus, question, body, k) {
  const qTokens = tokenize(question);
  if (!qTokens.length) return [];
  const qSet = new Set(qTokens);
  const scored = [];
  for (const d of corpus.docs) {
    if (body && d.body && d.body !== body) continue;
    const tf = new Map();
    for (const t of d._tokens) if (qSet.has(t)) tf.set(t, (tf.get(t) || 0) + 1);
    if (!tf.size) continue;
    // BM25 with document-length normalization: a term in a short, on-point doc
    // (a member card, the overview) outweighs the same term buried in a long
    // motion's procedural boilerplate.
    const dl = d._tokens.length;
    let score = 0;
    for (const [t, f] of tf) {
      const w = corpus.idf.get(t) || 0;
      score += (w * (f * (K1 + 1))) / (f + K1 * (1 - B + (B * dl) / corpus.avgdl));
    }
    if (tokenize(d.title).some((t) => qSet.has(t))) score *= 1.3;
    // Nudge the concise fact docs that answer "who is…" / "what is…" directly.
    const boost = { overview: 1.35, member: 1.2, tax: 1.2, budget: 1.2 };
    score *= boost[d.kind] || 1;
    scored.push({ d, score });
  }
  scored.sort((a, b) => b.score - a.score);
  return scored.slice(0, k).map((s) => s.d);
}

// ---- prompt -------------------------------------------------------------

function systemPrompt() {
  return [
    "You are the assistant for Civic Roll Call (civicrollcall.com), a site that",
    "tracks the Eagle Mountain City, Utah government — its council and board",
    "meetings, motions, roll-call votes, members, budget, and property tax.",
    "",
    "Answer the visitor's question using ONLY the numbered context passages",
    "provided in the user message. Each passage has a title and a URL.",
    "",
    "Rules:",
    "- Ground every claim in the passages. If they don't contain the answer,",
    "  say so plainly and suggest what the site does cover — do not guess or use",
    "  outside knowledge.",
    "- When you mention a specific motion, meeting, member, or figure, link to it",
    "  in Markdown using that passage's URL, e.g. [what the council decided](URL).",
    "- Be concise and neutral. Lead with the answer. Use short paragraphs or a",
    "  small list. This is civic information, so precision matters — quote vote",
    "  counts and dates exactly as given.",
    "- Respond only with the final answer in Markdown. No preamble.",
  ].join("\n");
}

function contextBlock(chunks) {
  return chunks
    .map(
      (c, i) =>
        `[${i + 1}] ${c.title}\nURL: ${c.url}\n${c.text}`
    )
    .join("\n\n");
}

// ---- HTTP ---------------------------------------------------------------

function corsHeaders(env) {
  return {
    "Access-Control-Allow-Origin": env.ALLOWED_ORIGIN || DEFAULTS.ALLOWED_ORIGIN,
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "86400",
    Vary: "Origin",
  };
}

// Best-effort per-isolate rate limit. Workers isolates are ephemeral and not
// shared globally, so this only blunts bursts — for hard limits, move to a
// Durable Object or KV. Good enough to deter casual abuse of the API key.
const HITS = new Map();
function rateLimited(ip) {
  const now = Date.now();
  const win = 60_000;
  const max = 12;
  const arr = (HITS.get(ip) || []).filter((t) => now - t < win);
  arr.push(now);
  HITS.set(ip, arr);
  return arr.length > max;
}

async function handleAsk(request, env) {
  const cors = corsHeaders(env);
  let payload;
  try {
    payload = await request.json();
  } catch {
    return json({ error: "invalid JSON" }, 400, cors);
  }
  const question = String(payload.question || "").trim();
  const body = payload.body ? String(payload.body) : null;
  if (!question) return json({ error: "question is required" }, 400, cors);
  if (question.length > MAX_QUESTION_CHARS)
    return json({ error: "question too long" }, 400, cors);

  const ip = request.headers.get("CF-Connecting-IP") || "anon";
  if (rateLimited(ip)) return json({ error: "rate limited, try again shortly" }, 429, cors);

  if (!env.ANTHROPIC_API_KEY)
    return json({ error: "server not configured" }, 500, cors);

  let corpus;
  try {
    corpus = await loadCorpus(env);
  } catch (e) {
    return json({ error: "knowledge base unavailable" }, 503, cors);
  }

  const chunks = retrieve(corpus, question, body, TOP_K);
  const userContent =
    "Context passages:\n\n" +
    contextBlock(chunks) +
    `\n\n---\nVisitor's question: ${question}`;

  const upstream = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "x-api-key": env.ANTHROPIC_API_KEY,
      "anthropic-version": "2023-06-01",
    },
    body: JSON.stringify({
      model: env.MODEL || DEFAULTS.MODEL,
      max_tokens: MAX_TOKENS,
      // Grounded RAG over a small context — no need for thinking; keep the
      // widget fast and cheap.
      thinking: { type: "disabled" },
      stream: true,
      system: systemPrompt(),
      messages: [{ role: "user", content: userContent }],
    }),
  });

  if (!upstream.ok || !upstream.body) {
    const detail = await upstream.text().catch(() => "");
    return json({ error: "model request failed", detail: detail.slice(0, 300) }, 502, cors);
  }

  // Translate Anthropic's SSE into a simple text-delta SSE for the browser,
  // and append a `sources` event at the end so the widget can render citations.
  const sources = chunks.map((c) => ({ title: c.title, url: c.url, kind: c.kind }));
  const stream = transformStream(upstream.body, sources);
  return new Response(stream, {
    headers: {
      ...cors,
      "Content-Type": "text/event-stream; charset=utf-8",
      "Cache-Control": "no-cache",
    },
  });
}

function transformStream(upstreamBody, sources) {
  const encoder = new TextEncoder();
  const decoder = new TextDecoder();
  let buf = "";
  return new ReadableStream({
    async start(controller) {
      const reader = upstreamBody.getReader();
      const send = (event, data) =>
        controller.enqueue(encoder.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`));
      try {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          const lines = buf.split("\n");
          buf = lines.pop();
          for (const line of lines) {
            if (!line.startsWith("data:")) continue;
            const json = line.slice(5).trim();
            if (!json) continue;
            let evt;
            try {
              evt = JSON.parse(json);
            } catch {
              continue;
            }
            if (evt.type === "content_block_delta" && evt.delta?.type === "text_delta") {
              send("delta", { text: evt.delta.text });
            } else if (evt.type === "message_stop") {
              send("sources", { sources });
              send("done", {});
            } else if (evt.type === "error") {
              send("error", { message: evt.error?.message || "stream error" });
            }
          }
        }
      } catch (e) {
        send("error", { message: "stream interrupted" });
      } finally {
        controller.close();
      }
    },
  });
}

function json(obj, status, headers) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { ...headers, "Content-Type": "application/json" },
  });
}

export default {
  async fetch(request, env) {
    const cors = corsHeaders(env);
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: cors });
    const url = new URL(request.url);
    if (request.method === "POST" && url.pathname === "/ask") return handleAsk(request, env);
    if (request.method === "GET" && url.pathname === "/health")
      return json({ ok: true }, 200, cors);
    return json({ error: "not found" }, 404, cors);
  },
};
