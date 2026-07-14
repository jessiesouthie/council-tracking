# Civic Roll Call — site agent (go-live guide)

This is the key-holding proxy for the "Ask about this site" widget. The public
site is static (GitHub Pages), so it can't safely hold an Anthropic API key —
this Cloudflare Worker does. It retrieves the most relevant chunks from
`docs/agent-corpus.json`, makes one streaming Claude call grounded on them, and
streams the answer back to the browser.

```
Browser widget ──POST /ask──▶  Worker (holds API key)
   (agent.js)                    │  1. keyword-retrieve from agent-corpus.json
                                 │  2. one streaming Claude call
   ◀──── SSE (answer) ──────────┘  3. pipe deltas back
```

Everything in the repo is already built. What's left is three things only you
can do: get an API key, deploy the Worker, and point the widget at it.

---

## 1. Get an Anthropic API key

This is the **paid API**, separate from any Claude subscription. It's what the
widget bills against.

1. Go to <https://console.anthropic.com> → sign in / create an org.
2. **Billing** → add a payment method and buy a small starting credit.
3. **Limits** → set a **monthly spend limit** (e.g. $10–20 to start). This is a
   public widget, so the cap is your safety net against a traffic spike.
4. **API keys** → *Create key*. Copy it (starts with `sk-ant-…`) — you'll paste
   it into Cloudflare in step 2, never into this repo.

Cost is modest: the default model is `claude-sonnet-5` and each question is one
short grounded call. Watch the console's usage graph for the first week and
raise or lower the limit from there.

## 2. Deploy the Worker

You need a (free) Cloudflare account.

```bash
cd worker
npm install -g wrangler          # or: npm i -D wrangler && npx wrangler ...
wrangler login                   # opens a browser to authorize

# Store the API key as an encrypted secret (paste it when prompted):
wrangler secret put ANTHROPIC_API_KEY

wrangler deploy
```

`wrangler deploy` prints the Worker's URL, e.g.
`https://civic-roll-call-agent.your-subdomain.workers.dev`.

Test it:

```bash
curl -N https://civic-roll-call-agent.your-subdomain.workers.dev/ask \
  -H "Content-Type: application/json" \
  -d '{"question":"How did the council vote on the proposed tax increase?"}'
```

You should see an SSE stream of `event: delta` lines with the answer.

## 3. Point the widget at the Worker

Edit `docs/agent.js`, near the top, and replace the placeholder with your
Worker URL (keep the `/ask` path):

```js
const ENDPOINT =
  (typeof window !== "undefined" && window.CT_AGENT_ENDPOINT) ||
  "https://civic-roll-call-agent.your-subdomain.workers.dev/ask";
```

Commit and push. GitHub Pages redeploys, and the "Ask" button goes live on
every page.

> Until this URL is set, the widget still appears but replies that it isn't
> connected yet — so it's safe to ship the frontend before the Worker exists.

---

## CORS / same-origin (optional, nicer)

By default the Worker allows requests from `https://civicrollcall.com`
(`ALLOWED_ORIGIN` in `wrangler.toml`). If `civicrollcall.com`'s DNS is on
Cloudflare, you can instead route the Worker at `civicrollcall.com/api/ask`
(Workers → *Add route*), set `CT_AGENT_ENDPOINT = "/api/ask"` on the site, and
drop CORS entirely. Not required — the `*.workers.dev` URL works as-is.

## Keeping the knowledge fresh

`docs/agent-corpus.json` is what the agent knows. It's rebuilt automatically by
the weekly ingest (`.github/workflows/weekly-ingest.yml`), so the agent stays
current as new meetings land. To rebuild by hand:

```bash
python -m ingest.build_agent_corpus --all-bodies
```

## Config reference (`wrangler.toml`)

| Name | Kind | Purpose |
|------|------|---------|
| `ANTHROPIC_API_KEY` | secret | The paid API key. Set via `wrangler secret put`. |
| `CORPUS_URL` | var | Where to fetch the corpus JSON. Defaults to the live site. |
| `ALLOWED_ORIGIN` | var | Site origin allowed to call the Worker (CORS). |
| `MODEL` | var | Claude model id. `claude-sonnet-5` (default) or `claude-opus-4-8`. |

## Notes & limits

- **Rate limiting** is best-effort per Worker isolate (12 req/min/IP). For hard
  limits, move it to a Durable Object or KV — fine to skip for launch.
- **Model choice:** `claude-sonnet-5` is chosen for a public widget — near-Opus
  quality on grounded Q&A at lower cost/latency. Set `MODEL=claude-opus-4-8` for
  maximum quality.
- **When you move to WordPress:** the same corpus + prompt + retrieval logic
  ports directly. Only this Worker's HTTP shell gets swapped for a small
  PHP/plugin endpoint that holds the key; `agent.js` just points at the new URL.
