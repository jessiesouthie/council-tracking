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
 * Three things make the difference between BM25 that works here and BM25 that
 * doesn't, and all three are load-bearing:
 *   1. Transcripts are 57% of the corpus and are raw speech-to-text. Left
 *      unweighted they win nearly every query on sheer repetition, and the
 *      model ends up answering from meeting chatter instead of the record.
 *      They're damped and quota'd.
 *   2. The roster and body overview are ~2KB total, so they're PINNED into
 *      every prompt rather than left to compete for a retrieval slot. "Who is
 *      the mayor?" must never depend on out-ranking a thousand motions that
 *      mention the word "mayor".
 *   3. Civic questions are overwhelmingly about now ("last meeting", "this
 *      year"), but the corpus reaches back to 2019. Scoring carries a recency
 *      prior that sharpens when the question has a temporal cue.
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
  // claude-opus-5 by setting the MODEL var if you want maximum quality.
  MODEL: "claude-sonnet-5",
};

const MAX_QUESTION_CHARS = 600;
const TOP_K = 8; // retrieved passages fed to the model (roster is pinned on top)
const MAX_PASSAGE_CHARS = 1400; // clip any single passage; some run to 54KB
// Older motions carry their entire ordinance text as the "title" — 191 of them
// run past 200 chars and one hits 16KB. Unclipped, a single passage's Title:
// line could outweigh the whole rest of the prompt, and the widget rendered it
// as the clickable label of a source link.
const MAX_TITLE_CHARS = 160;
const MAX_CONTEXT_CHARS = 16000; // hard ceiling on the retrieved block
const MAX_TOKENS = 700; // answers should be short — see systemPrompt()
const MAX_HISTORY_TURNS = 6; // conversation turns replayed to the model

// ---- corpus + retrieval -------------------------------------------------

// Cached per isolate: the parsed corpus and a precomputed token index.
let CORPUS = null; // { docs, idf, avgdl, bodies, facts, maxDate }
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

/**
 * Very light suffix stripping. Not a real Porter stemmer — just enough to stop
 * "subdivisions"/"subdivision" and "voted"/"votes"/"voting"/"vote" from being
 * four unrelated terms, which was costing real recall on ordinary questions.
 * The trailing-e strip is what collapses vote/voted/voting onto one stem.
 */
function stem(w) {
  if (w.length <= 3) return w;
  if (w.endsWith("ies") && w.length > 4) w = w.slice(0, -3) + "y";
  else if (/(ch|sh|ss|x|z)es$/.test(w)) w = w.slice(0, -2);
  else if (w.endsWith("s") && !w.endsWith("ss") && !w.endsWith("us")) w = w.slice(0, -1);
  if (w.endsWith("ing") && w.length > 5) w = w.slice(0, -3);
  else if (w.endsWith("ed") && w.length > 4) w = w.slice(0, -2);
  if (w.length > 3 && w.endsWith("e")) w = w.slice(0, -1);
  return w;
}

function tokenize(text) {
  const out = [];
  for (const t of String(text || "").toLowerCase().match(/[a-z0-9]+/g) || []) {
    if (t.length > 1 && !STOP.has(t)) out.push(stem(t));
  }
  return out;
}

// Query-side only, so it can never pollute the index. Covers the names the
// speech-to-text and the public spell differently, plus a few civic synonyms
// where the site's wording and a resident's wording reliably diverge.
const ALIASES = {
  zac: ["zachory"],
  zack: ["zachory"],
  zachary: ["zachory"],
  hewish: ["huish"],
  councilman: ["councilmember"],
  councilwoman: ["councilmember"],
  councilor: ["councilmember"],
  councillor: ["councilmember"],
  alderman: ["councilmember"],
  rezone: ["zoning"],
  rezoning: ["zoning"],
  plat: ["subdivision"],
  levy: ["tax"],
  millage: ["tax", "rate"],
  raise: ["increase"],
  hike: ["increase"],
  cop: ["police", "sheriff"],
  cops: ["police", "sheriff"],
  home: ["housing", "residential"],
  house: ["housing", "residential"],
};

function expand(tokens) {
  const out = new Set(tokens);
  for (const raw of tokens) {
    const alts = ALIASES[raw];
    if (alts) for (const a of alts) out.add(stem(a));
  }
  return out;
}

// Ordinary keyword tokens can't tell "what did they decide last month" from
// "what did they decide in 2021". These two cues change how scoring weights age
// and how much raw meeting speech is allowed into the context.
// "Who is X" is already answered in full by the pinned roster — role, term and
// vote record. Handed eight motion passages on top of that, the model pads the
// answer with whatever it can see the person doing in them, which for a
// councilmember is mostly seconding other people's motions. That tells a
// visitor nothing about who they are, so these questions get a much smaller
// retrieval and an explicit instruction to stop at the facts.
const IDENTITY_RE = /^\s*who(?:'s|\s+(?:is|was|are|were))\b|\b(?:tell me|what do you know)\s+about\b/i;
const IDENTITY_TOP_K = 3;

const TEMPORAL_RE =
  /\b(latest|recent|recently|current|currently|now|today|tonight|this (?:year|month|week)|upcoming|next|new|lately|so far|still|last (?:meeting|night|week|month|year))\b/i;
const DISCUSSION_RE =
  /\b(said|say|says|saying|discuss|discussed|discussion|debate|debated|comment|comments|argue|argued|testimony|complain|concern|concerns|quote|explain|explained|reaction|resident|residents|speak|spoke|spoken|public comment|hearing)\b/i;

const DAY = 86400000;

async function loadCorpus(env) {
  if (CORPUS) return CORPUS;
  if (CORPUS_PROMISE) return CORPUS_PROMISE;
  const url = env.CORPUS_URL || DEFAULTS.CORPUS_URL;
  CORPUS_PROMISE = (async () => {
    // Only cache successful responses at the edge — cacheEverything would pin a
    // transient 404 (e.g. before the corpus is first published) for its whole
    // TTL. Per-isolate CORPUS memo below avoids most refetches anyway.
    const r = await fetch(url, { cf: { cacheTtlByStatus: { "200-299": 300, "300-599": 0 } } });
    if (!r.ok) throw new Error(`corpus fetch failed: ${r.status}`);
    const raw = await r.json();
    const docs = raw.docs.map((d) => {
      const tokens = tokenize(d.title + " " + d.text);
      return {
        ...d,
        _tokens: tokens,
        _dl: tokens.length,
        // Only the leading part of the title counts toward the title boost:
        // a motion whose "title" is 16KB of ordinance text would otherwise
        // match nearly any query term and collect the bonus every time.
        _title: new Set(tokenize(String(d.title).slice(0, 200))),
        _tags: new Set((d.tags || []).map((t) => stem(String(t).toLowerCase()))),
        _at: d.date ? Date.parse(d.date) : NaN,
      };
    });
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
    const avgdl = docs.reduce((s, d) => s + d._dl, 0) / (N || 1);
    const bodies = raw.bodies || [];
    CORPUS = {
      docs,
      idf,
      avgdl,
      bodies,
      facts: buildFacts(docs, bodies),
      maxDate: docs.reduce((m, d) => (d._at > m ? d._at : m), 0),
    };
    return CORPUS;
  })().catch((e) => {
    // Don't let a failed load poison the isolate — clear the promise so the
    // next request retries instead of replaying the rejection forever.
    CORPUS_PROMISE = null;
    throw e;
  });
  return CORPUS_PROMISE;
}

/**
 * Per-body reference block, pinned into every prompt. Everything here is short,
 * always relevant, and answers the questions visitors actually open the widget
 * with — who serves, how they vote, what the site holds, when it last updated.
 * Total is ~2KB per body, cheaper than losing a retrieval slot to it.
 */
function buildFacts(docs, bodies) {
  const facts = new Map();
  for (const b of bodies) {
    const mine = docs.filter((d) => d.body === b.id);
    const overview = mine.find((d) => d.kind === "overview");
    const members = mine.filter((d) => d.kind === "member");
    const dated = (kind) =>
      mine
        .filter((d) => d.kind === kind && d._at)
        .sort((x, y) => y._at - x._at)[0];
    const lastMeeting = dated("meeting");
    const lastTranscript = dated("transcript");
    const lastMotion = dated("motion");

    const lines = [`This is the ${b.label} of Eagle Mountain City, Utah.`];
    if (overview) lines.push(overview.text);
    if (members.length) {
      lines.push("", "People on record (name — role, term, recorded votes):");
      for (const m of members) lines.push(`- ${m.text} <${m.url}>`);
    }
    // The next meeting is the only thing on the site a visitor can still act
    // on, and "when is the next meeting" is a question the record itself can
    // never answer — so it's stated up front rather than left to retrieval.
    const upcoming = mine.find((d) => d.kind === "upcoming");
    if (upcoming)
      lines.push("", "Next scheduled meeting:", clip(upcoming.text, 1200), `<${upcoming.url}>`);

    lines.push("", "How current the record is:");
    if (lastMeeting)
      // Deliberately "on file" and not "summarized": a meeting record may be a
      // full summary, a transcript account, or just the posted agenda, and
      // promising a summary the corpus doesn't hold invites the model to
      // apologize for missing one.
      lines.push(`- Most recent meeting on file: ${lastMeeting.date} <${lastMeeting.url}>`);
    if (lastMotion) lines.push(`- Most recent motion on file: ${lastMotion.date}`);
    if (lastTranscript) lines.push(`- Most recent meeting transcript: ${lastTranscript.date}`);
    if (
      lastTranscript &&
      lastMeeting &&
      lastTranscript._at > lastMeeting._at
    ) {
      lines.push(
        `- Meetings after ${lastMeeting.date} have a transcript but no written summary or motion list yet.`
      );
    }
    facts.set(b.id, lines.join("\n"));
  }
  return facts;
}

// True when the visitor is asking who a person on the roster is — which the
// pinned facts answer outright, so there is nothing for retrieval to add.
function isIdentityQuestion(corpus, body, question) {
  if (!IDENTITY_RE.test(question)) return false;
  const asked = new Set(tokenize(question));
  return corpus.docs.some(
    (d) =>
      d.kind === "member" &&
      d.body === body &&
      tokenize(d.title).some((t) => asked.has(t))
  );
}

// If the visitor names another body, honor that over whichever page they
// happen to be on — otherwise the page-derived filter silently hides the only
// docs that could answer them.
function resolveBody(corpus, question, fallback) {
  const q = question.toLowerCase();
  for (const b of corpus.bodies) {
    if (b.label && q.includes(b.label.toLowerCase())) return b.id;
  }
  if (/\bplanning\b/.test(q)) return "planning-commission";
  if (/\bcommunity services\b/.test(q)) return "community-services-board";
  if (fallback && corpus.facts?.has(fallback)) return fallback;
  // A caller that sends no body (or an unknown one) still gets a grounded
  // prompt rather than an unscoped search with no roster pinned to it.
  const def = corpus.bodies.find((b) => b.default) || corpus.bodies[0];
  return def ? def.id : null;
}

const K1 = 1.2;
const B = 0.75;

// Transcripts are unedited speech-to-text and by far the largest slice of the
// corpus; without this they crowd out the documents that hold the actual
// record. Concise fact docs get a nudge in the other direction.
const KIND_WEIGHT = {
  tax: 1.25,
  budget: 1.25,
  meeting: 1.1,
  motion: 1.0,
  transcript: 0.55,
};

// These are already pinned into every prompt by buildFacts(), so retrieving
// them would spend a passage slot on text the model has in front of it either
// way. Skipping them buys three more slots of actual record per question.
const PINNED_KINDS = new Set(["overview", "member", "upcoming"]);

/**
 * Weighted query terms: the current question at full weight, earlier turns in
 * the conversation at a fraction, so "what about last year?" still retrieves
 * against whatever subject the visitor established two questions ago.
 */
function queryTerms(question, priorQuestions) {
  const now = expand(tokenize(question));
  const weights = new Map();
  for (const t of now) weights.set(t, 1);
  for (const p of priorQuestions) {
    for (const t of expand(tokenize(p))) {
      if (!weights.has(t)) weights.set(t, 0.35);
    }
  }
  return { weights, primary: now };
}

function retrieve(corpus, question, priorQuestions, body, k) {
  const { weights, primary } = queryTerms(question, priorQuestions);
  if (!weights.size) return [];
  const temporal = TEMPORAL_RE.test(question);
  const discussion = DISCUSSION_RE.test(question);
  // A question with content words must be answered by docs that contain at
  // least one of THEM — carried-over context can sharpen a ranking, never
  // create one on its own.
  const required = primary.size ? primary : null;
  const now = Math.max(Date.now(), corpus.maxDate || 0);
  // Half-life on the recency prior: about a year when the question points at
  // the present, ~5 years otherwise so that old business still surfaces.
  const halfLife = (temporal ? 365 : 1800) * DAY;
  const recencyWeight = temporal ? 1.1 : 0.3;

  const scored = [];
  for (const d of corpus.docs) {
    if (body && d.body && d.body !== body) continue;
    if (PINNED_KINDS.has(d.kind)) continue;
    let hitsRequired = !required;
    const tf = new Map();
    for (const t of d._tokens) {
      if (!weights.has(t)) continue;
      tf.set(t, (tf.get(t) || 0) + 1);
      if (required && required.has(t)) hitsRequired = true;
    }
    if (!tf.size || !hitsRequired) continue;

    // BM25 with document-length normalization: a term in a short, on-point doc
    // (a member card, the overview) outweighs the same term buried in a long
    // motion's procedural boilerplate.
    let score = 0;
    for (const [t, f] of tf) {
      const w = (corpus.idf.get(t) || 0) * weights.get(t);
      score += (w * (f * (K1 + 1))) / (f + K1 * (1 - B + (B * d._dl) / corpus.avgdl));
    }
    for (const t of d._title) if (weights.has(t)) { score *= 1.3; break; }
    for (const t of d._tags) if (weights.has(t)) { score *= 1.15; break; }
    score *= KIND_WEIGHT[d.kind] || 1;
    if (d._at) {
      score *= 1 + recencyWeight * Math.exp(-Math.max(0, now - d._at) / halfLife);
    }
    scored.push({ d, score });
  }
  scored.sort((a, b) => b.score - a.score);

  // Selection, not just top-k: hold the transcript share down, and never let
  // one long meeting contribute more than a couple of chunks — both failure
  // modes fill the whole context with a single afternoon of talking.
  const maxTranscripts = discussion ? 4 : 2;
  const picked = [];
  const perUrl = new Map();
  let transcripts = 0;
  let chars = 0;
  for (const { d } of scored) {
    if (picked.length >= k || chars >= MAX_CONTEXT_CHARS) break;
    if (d.kind === "transcript" && transcripts >= maxTranscripts) continue;
    const seen = perUrl.get(d.url) || 0;
    if (seen >= 2) continue;
    perUrl.set(d.url, seen + 1);
    if (d.kind === "transcript") transcripts++;
    picked.push(d);
    chars +=
      Math.min(d.text.length, MAX_PASSAGE_CHARS) +
      Math.min(d.title.length, MAX_TITLE_CHARS);
  }
  return picked;
}

// ---- prompt -------------------------------------------------------------

function clip(text, max) {
  const s = String(text || "");
  if (s.length <= max) return s;
  const cut = s.slice(0, max);
  const stop = cut.lastIndexOf(" ");
  return (stop > max * 0.6 ? cut.slice(0, stop) : cut) + " …";
}

const KIND_LABEL = {
  upcoming: "Upcoming meeting (scheduled, not yet held)",
  motion: "Motion",
  meeting: "Meeting summary",
  transcript: "Transcript (raw speech-to-text)",
  member: "Member",
  overview: "Site overview",
  tax: "Property tax",
  budget: "Budget",
};

function systemPrompt(today) {
  return [
    "You are the assistant for Civic Roll Call (civicrollcall.com), which tracks",
    "the Eagle Mountain City, Utah government — council and board meetings,",
    "motions, roll-call votes, members, the budget, and property tax.",
    "",
    `Today is ${today}.`,
    "",
    "The user message holds two things:",
    "- STANDING FACTS: always-true reference for the body being asked about —",
    "  who serves, their terms and vote tallies, what the site covers, and how",
    "  current the record is. Use it freely; it is not in dispute.",
    "- PASSAGES: numbered excerpts retrieved for this question, each with a kind,",
    "  a title and a URL. Some will be irrelevant — ignore those rather than",
    "  working them in.",
    "",
    "Passages marked Transcript are unedited machine speech-to-text of a meeting",
    "recording. They mis-hear names, and they record what someone SAID, not what",
    "the body DECIDED. Never assert a decision, vote count, or dollar figure on a",
    "transcript's authority alone — motions and meeting summaries are the record.",
    "",
    "How to answer:",
    "- Lead with the direct answer in the first sentence. No preamble, no",
    "  restating the question, no 'based on the passages'.",
    "- Stay under about 120 words. One or two short paragraphs, or at most four",
    "  bullets. Never a wall of text — a visitor asked a question, not for a",
    "  briefing.",
    "- Link sparingly: at most two or three Markdown links, each on the specific",
    "  thing worth clicking, e.g. [the 5–0 vote on 6 August](URL). Do not list",
    "  every passage you were given — the interface shows sources separately.",
    "- Asked who someone is, give their role and their term, and stop there —",
    "  add their vote record only if the visitor asked about it. Never pad the",
    "  answer with motions they moved or seconded: that a member seconded",
    "  something is procedural noise and says nothing about who they are.",
    "- Quote vote counts, dates, names and dollar figures exactly as written.",
    "  Never round, estimate, or infer a total the passages don't state.",
    "- If the passages don't answer it, say so in one sentence and name the",
    "  nearest thing the site does hold. Never guess or fall back on outside",
    "  knowledge about Eagle Mountain.",
    "- Neutral and factual: report what the record shows, don't characterize",
    "  motives or take a side.",
  ].join("\n");
}

function buildUserContent(facts, chunks, question) {
  const parts = [];
  if (facts) parts.push("STANDING FACTS\n" + facts);
  parts.push(
    chunks.length
      ? "PASSAGES\n\n" +
          chunks
            .map(
              (c, i) =>
                `[${i + 1}] ${KIND_LABEL[c.kind] || "Record"}${c.date ? ` — ${c.date}` : ""}\n` +
                `Title: ${clip(c.title, MAX_TITLE_CHARS)}\nURL: ${c.url}\n` +
                clip(c.text, MAX_PASSAGE_CHARS)
            )
            .join("\n\n")
      : "PASSAGES\n\n(none matched this question)"
  );
  parts.push(`---\nVisitor's question: ${question}`);
  return parts.join("\n\n");
}

/**
 * Normalize the widget's conversation log into Anthropic messages. Only the
 * text is replayed — earlier source lists would just re-spend context — and
 * the tail is capped so a long chat can't crowd out the passages.
 */
function normalizeHistory(raw) {
  if (!Array.isArray(raw)) return [];
  const turns = [];
  for (const m of raw) {
    const role = m && (m.role === "assistant" || m.r === "b") ? "assistant" : "user";
    const text = String((m && (m.text ?? m.t ?? m.md)) || "").trim();
    if (text) turns.push({ role, content: clip(text, 1200) });
  }
  // Must start with a user turn and alternate cleanly for the API.
  const tail = turns.slice(-MAX_HISTORY_TURNS);
  while (tail.length && tail[0].role !== "user") tail.shift();
  const out = [];
  for (const t of tail) {
    if (out.length && out[out.length - 1].role === t.role) out[out.length - 1] = t;
    else out.push(t);
  }
  if (out.length && out[out.length - 1].role === "user") out.pop();
  return out;
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

  const history = normalizeHistory(payload.history);
  const priorQuestions = history.filter((m) => m.role === "user").map((m) => m.content);
  const body = resolveBody(corpus, question, payload.body ? String(payload.body) : null);

  const k = isIdentityQuestion(corpus, body, question) ? IDENTITY_TOP_K : TOP_K;
  const chunks = retrieve(corpus, question, priorQuestions, body, k);
  const today = new Date().toISOString().slice(0, 10);
  const userContent = buildUserContent(
    body ? corpus.facts.get(body) : null,
    chunks,
    question
  );

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
      system: systemPrompt(today),
      messages: [...history, { role: "user", content: userContent }],
    }),
  });

  if (!upstream.ok || !upstream.body) {
    const detail = await upstream.text().catch(() => "");
    return json({ error: "model request failed", detail: detail.slice(0, 300) }, 502, cors);
  }

  // Translate Anthropic's SSE into a simple text-delta SSE for the browser,
  // and append a `sources` event at the end so the widget can render citations.
  const sources = chunks.map((c) => ({
    title: clip(c.title, 90),
    url: c.url,
    kind: c.kind,
  }));
  const stream = transformStream(upstream.body, sources);
  return new Response(stream, {
    headers: {
      ...cors,
      "Content-Type": "text/event-stream; charset=utf-8",
      "Cache-Control": "no-cache",
    },
  });
}

/**
 * Show the sources the answer actually leaned on, not everything retrieved.
 * The old behavior listed all twelve passages under every reply, which read as
 * a wall of links and buried whichever one mattered.
 */
function usedSources(sources, answer) {
  const cited = sources.filter((s) => s.url && answer.includes(s.url));
  return (cited.length ? cited : sources.slice(0, 3)).slice(0, 4);
}

function transformStream(upstreamBody, sources) {
  const encoder = new TextEncoder();
  const decoder = new TextDecoder();
  let buf = "";
  let answer = "";
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
            const raw = line.slice(5).trim();
            if (!raw) continue;
            let evt;
            try {
              evt = JSON.parse(raw);
            } catch {
              continue;
            }
            if (evt.type === "content_block_delta" && evt.delta?.type === "text_delta") {
              answer += evt.delta.text;
              send("delta", { text: evt.delta.text });
            } else if (evt.type === "message_stop") {
              send("sources", { sources: usedSources(sources, answer) });
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
