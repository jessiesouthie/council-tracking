/* =============================================================================
   Tests that the three prompts write in one voice — node --test docs/house-style.test.mjs

   Two thirds of the words on this site come out of a model: 440 motion notes,
   19 meeting summaries, and every answer the "ask about this site" widget gives.
   Each was written to its own prompt, and each prompt was careful about accuracy
   and silent about prose. What came back was accurate and repetitive: 243 of the
   440 impact notes contained "nothing changes", 88 of them opening with the same
   five words, because three of the motion prompt's own exemplars handed the model
   that phrase.

   ingest/house_style.txt is now the one copy of the five rules.
   summarize_motions.py and transcribe_meeting.sh read it at run time, so those
   two cannot drift. The Worker deploys to Cloudflare on its own and cannot reach
   the repo, so it mirrors the rules instead — and a mirror is exactly the thing
   that goes stale quietly. This is what notices.

   What is covered:
     · house_style.txt still states five numbered rules
     · both repo-side prompts read that file rather than carrying a copy
     · the Worker's mirrored block covers every rule in it
     · no prompt demonstrates an em dash in its own running prose
     · the "nothing changes" formula has not crept back into any of them
   ============================================================================= */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const DOCS = fileURLToPath(new URL("./", import.meta.url));
const ROOT = path.resolve(DOCS, "..");
const read = (p) => readFileSync(path.join(ROOT, p), "utf8");

const STYLE = read("ingest/house_style.txt");
const MOTIONS = read("ingest/summarize_motions.py");
const TRANSCRIBE = read("scripts/transcribe_meeting.sh");
const WORKER = read("worker/src/index.js");

// The prompt text only, not the Python or shell around it. An em dash in a code
// comment is nobody's business; one inside the instructions is a demonstration.
const motionPrompt = MOTIONS.match(/^PROMPT = """\\\n([\s\S]*?)^"""$/m)[1];
const transcribePrompt = TRANSCRIBE.match(/<<'PROMPT'\n([\s\S]*?)\nPROMPT\n/)[1];
const workerPrompt = WORKER.match(/function systemPrompt[\s\S]*?\n}/)[0];

test("the style file states five numbered rules", () => {
  const numbered = STYLE.match(/^\d\. /gm) || [];
  assert.equal(numbered.length, 5, "house_style.txt should carry five rules");
  assert.match(STYLE, /HOUSE STYLE/);
});

test("both repo-side prompts read the style file instead of copying it", () => {
  assert.match(MOTIONS, /house_style\.txt/,
    "summarize_motions.py should load ingest/house_style.txt");
  assert.match(motionPrompt, /\{house_style\}/,
    "the motion prompt needs the placeholder the loader fills");
  assert.match(TRANSCRIBE, /cat "\$ROOT\/ingest\/house_style\.txt"/,
    "transcribe_meeting.sh should cat ingest/house_style.txt into its prompt");
});

test("the Worker's mirror covers every rule in the file", () => {
  // Matched on the load-bearing phrase of each rule rather than on wording, so
  // the mirror may stay shorter than the file without failing here.
  const mustCover = [
    /No em dashes/,
    /No stock opening/,
    /Cut an adverb when the sentence survives without it/,
    /Name who acted/,
    /State the thing itself/,
  ];
  assert.match(workerPrompt, /HOUSE STYLE/,
    "worker/src/index.js systemPrompt() should carry the mirrored block");
  for (const rule of mustCover) {
    assert.match(STYLE, rule, `house_style.txt lost: ${rule}`);
    assert.match(workerPrompt, rule, `the Worker mirror lost: ${rule}`);
  }
});

test("no prompt demonstrates an em dash in its own prose", () => {
  // Two exceptions, both layout rather than prose: the motion prompt sets its
  // four field definitions on a hanging indent the dash holds open, and the
  // transcript prompt specifies "### <ref> — <title>" as a literal output
  // heading, which every published summary already uses.
  const layout = [
    /^\s*"(headline|summary|impact|significance)"\s*—/,
    /^\s*"(notable|routine)"\s*—/,
    /### <ref> — <title>/,
  ];
  const offenders = [];
  for (const [name, text] of Object.entries({
    "summarize_motions.py": motionPrompt,
    "transcribe_meeting.sh": transcribePrompt,
    "worker systemPrompt()": workerPrompt,
  })) {
    for (const line of text.split("\n")) {
      if (!line.includes("—")) continue;
      if (layout.some((re) => re.test(line))) continue;
      offenders.push(`${name}: ${line.trim()}`);
    }
  }
  assert.deepEqual(offenders, [], "em dash in prompt prose");
});

test("the formula that started this has not crept back", () => {
  for (const [name, text] of Object.entries({
    "summarize_motions.py": motionPrompt,
    "transcribe_meeting.sh": transcribePrompt,
    "worker systemPrompt()": workerPrompt,
  })) {
    assert.doesNotMatch(text, /nothing changes/i,
      `${name} is handing the model the stock opening again`);
  }
});
