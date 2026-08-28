#!/usr/bin/env node
/**
 * Open WebUI tool-call simulation probe (faithful end-to-end round-trips).
 *
 * Reproduces, through the REAL stack (Open WebUI → pipe agent_loop_guard →
 * Bifrost → DeepSeek), the tool-call continuation turns where the HANDOFF
 * places the reasoning drop, and reports the same signature the pipe's
 * SUSPECT-DROP log uses (reasoning_deltas <= 1 with content present).
 *
 * Two modes:
 *
 *   single      — one tool call per round (smart_fetch_url): discovery
 *                 request, real tool execution, OpenAI-style continuation.
 *   interleaved — three INTERLEAVED tool calls per round (get_current_timestamp
 *                 → smart_fetch_url → search_web), all executed for real,
 *                 replayed as one chained tool-call continuation. This is the
 *                 faithful reproduction of the user's real usage: the model
 *                 alternates reasoning and tool calls in a single session.
 *
 * The continuation is the turn that triggers the pipe's
 * `_history_has_tool_calls()` path (ships tools to Bifrost + forces
 * `reasoning_content`) where the drop lives.
 *
 * Tool inventory (verified, see HANDOFF § "Simulating a tool call…"):
 *   - smart_fetch_url (custom, attached to the model) — executed via the
 *     repo copy (tools/smart_fetch_url) with curl_cffi.
 *   - get_current_timestamp (OWUI builtin `time` category) — replicated
 *     locally (same logic: Unix ts + ISO UTC).
 *   - search_web (OWUI builtin `web_search` category) — executed for real
 *     via POST /api/v1/retrieval/process/web/search.
 * Per user instruction only these (no image_generator_pro, no others).
 *
 * Usage:
 *   OWUI_BASE_URL=http://open-webui.private \
 *   OWUI_API_KEY=sk-... \
 *   node pipes/agent_loop_guard/tests/sim_tool_call_owui.mjs [rounds] [mode] [url]
 *     rounds: iterations (default 5)
 *     mode:   interleaved (default) | single
 *     url:    page to fetch (default https://elpais.com)
 *
 * Exit 0 = no drop signature, 1 = at least one drop signature, 2 = setup error.
 *
 * See pipes/agent_loop_guard/tests/README.md for the full test battery.
 */

import { execFileSync } from "node:child_process";
import { resolve } from "node:path";

const BASE = process.env.OWUI_BASE_URL ?? "http://open-webui.private";
const KEY = process.env.OWUI_API_KEY; // required — never hardcode credentials
const MODEL = process.env.OWUI_MODEL ?? "deepseek-v4-flash";
const ROUNDS = Number(process.argv[2] || 5);
const MODE = process.argv[3] || "interleaved";
const URL = process.argv[4] || "https://elpais.com";
const TIMEOUT_MS = 120000;
const TOOL_ID = "call_sim_001"; // stable synthetic tool-call id (history only)

/**
 * Realistic, long, OWUI-style assistant system prompt (English).
 * Mimics the user's long real OWUI prompt — the factor the HANDOFF links to
 * higher drop rates. NO personal data (no user name, no identifiers).
 */
const SYSTEM_PROMPT = `You are an AI assistant integrated into a web chat platform. You help users with a wide range of tasks: answering questions, researching current events, fetching and summarizing web content, and producing clear, well-structured responses.

Guidelines:
- Respond in the same language the user writes in, unless asked otherwise.
- Use Markdown formatting (headings, lists, bold) to keep long answers scannable.
- Be concise but complete: answer the actual question first, then add detail.
- When a task needs up-to-date information, use the available tools: web_search to find sources, smart_fetch_url to retrieve and read full pages, and get_current_timestamp for the current date and time.
- Use tool calls as soon as you have enough context; do not guess facts you can verify.
- Cite your sources when you use web results, and summarize findings in your own words.
- If information is missing, unavailable, or a tool fails, say so clearly and offer alternatives.
- Never fabricate data, references, or tool outputs. If you cannot verify something, state that you could not verify it.
- When summarizing fetched pages, focus on the content most relevant to the user's request and structure the answer with clear sections.
- Respect user privacy: do not request or retain personal information unless the task requires it.`;

const TOOL_DIR = resolve(
  import.meta.dirname, "../../../tools/smart_fetch_url",
);
const PYTHON = process.env.PYTHON ?? "python3";

/** POST a chat completion; returns {status, text (full SSE body)}. */
async function chat(payload) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const res = await fetch(`${BASE}/api/chat/completions`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${KEY}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
    return { status: res.status, text: await res.text() };
  } catch (err) {
    return { status: `ERR:${err.name}`, text: "" };
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Count reasoning deltas/chars + content presence in an SSE body.
 * Counts a reasoning delta when ANY of the three gateway fields is present
 * (reasoning / reasoning_content / reasoning_details) and accumulates the
 * text of the first non-empty one — same lens the pipe uses.
 */
function analyze(text) {
  let rd = 0, rlen = 0, clen = 0, toolEvents = 0, finish = "";
  for (const line of text.split("\n")) {
    const s = line.trim();
    if (!s.startsWith("data:") || s === "data: [DONE]") continue;
    try {
      const ev = JSON.parse(s.slice(5).trim());
      const d = ev.choices?.[0]?.delta;
      if (!d) continue;
      if (d.reasoning_content !== undefined || d.reasoning !== undefined || d.reasoning_details !== undefined) {
        rd++;
        rlen += (d.reasoning_content ?? d.reasoning ?? d.reasoning_details?.[0]?.text ?? "").length;
      }
      if (d.content) clen += d.content.length;
      if (d.tool_calls) toolEvents++;
      if (ev.choices?.[0]?.finish_reason) finish = ev.choices[0].finish_reason;
    } catch { /* non-JSON SSE line */ }
  }
  return { rd, rlen, clen, toolEvents, finish };
}

/** Drop signature — same condition as the pipe's SUSPECT-DROP log. */
function isDrop(s) {
  return s.rd <= 1 && s.clen > 0;
}

/**
 * Execute the REAL smart_fetch_url tool (repo copy) via python, exactly as
 * OWUI would call it in the container (curl_cffi TLS fingerprinting +
 * trafilatura/selectolax extraction). Requires those deps in the local
 * python (pip install curl_cffi trafilatura selectolax).
 * Returns the tool's text output (metadata header + extracted content).
 */
function execTool(url, maxChars = 4096) {
  // Inline runner: import the tool module from TOOL_DIR, instantiate Tools,
  // call smart_fetch_url with the REAL schema (urls/format/max_chars — NOT
  // the parameter names the model hallucinated), close the session.
  const code = `
import asyncio, sys
sys.path.insert(0, ${JSON.stringify(TOOL_DIR)})
from smart_fetch_url import Tools
async def main():
    t = Tools()
    try:
        print(await t.smart_fetch_url(urls=[${JSON.stringify(url)}], format="skimmd", max_chars=${maxChars}))
    finally:
        try: await t._aclose()
        except Exception: pass
asyncio.run(main())`;
  return execFileSync(PYTHON, ["-c", code], {
    encoding: "utf8",
    timeout: 60000,
  });
}

/**
 * Faithful replica of OWUI's builtin get_current_timestamp (tools/builtin.py):
 * JSON with current_timestamp (Unix s), current_iso (UTC ISO). User-local
 * timezone is omitted (no user context outside OWUI).
 */
function currentTimestampResult() {
  const now = new Date();
  return JSON.stringify({
    current_timestamp: Math.floor(now.getTime() / 1000),
    current_iso: now.toISOString(),
  });
}

/**
 * Execute OWUI's builtin search_web for real via the retrieval API, then
 * format the top results (link/title/snippet) the way the tool would.
 */
async function searchWeb(query, count = 3) {
  const res = await fetch(`${BASE}/api/v1/retrieval/process/web/search`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ queries: [query], count }),
  });
  const body = await res.json().catch(() => ({}));
  const items = body.items ?? [];
  if (!items.length) return JSON.stringify({ error: "search returned no results", status: res.status });
  return JSON.stringify({
    query,
    results: items.slice(0, count).map((it) => ({
      title: it.title,
      link: it.link,
      snippet: (it.snippet ?? "").slice(0, 300),
    })),
  });
}

/** Build an OpenAI-style assistant message carrying a structured tool call. */
function assistantToolCall(name, args) {
  return {
    role: "assistant",
    content: null,
    reasoning_content: "", // empty seed — matches real OWUI history (H1: not the cause)
    tool_calls: [{
      id: TOOL_ID,
      type: "function",
      function: { name, arguments: JSON.stringify(args) },
    }],
  };
}

/** Tool-result message (OpenAI `tool` role) with the executed output. */
function toolResult(content) {
  return { role: "tool", tool_call_id: TOOL_ID, name: "smart_fetch_url", content };
}

/**
 * INTERLEAVED round — 3 real tool calls chained in one conversation, the
 * faithful reproduction of real agent sessions:
 *   get_current_timestamp → smart_fetch_url → search_web → final answer.
 * The whole chain is replayed as one streamed continuation request (same
 * shape as the HANDOFF roundtrip probe, through the real stack); the drop
 * signature is measured on the final turn.
 */
async function interleavedRound(roundNo, url) {
  // Execute all three tools for real BEFORE the continuation.
  const timeResult = currentTimestampResult();
  const fetchResult = execTool(url).trim();
  const searchResult = await searchWeb("portada de el pais actualidad");
  const question = `¿Qué hora es ahora en UTC? Después haz fetch de ${url} y busca en la web qué es lo más destacado de su portada hoy, y resume todo en español.`;

  const c = await chat({
    model: MODEL,
    stream: true,
    messages: [
      { role: "system", content: SYSTEM_PROMPT },
      { role: "user", content: question },
      assistantToolCall("get_current_timestamp", {}),
      toolResult(timeResult),
      assistantToolCall("smart_fetch_url", { urls: [url], format: "skimmd", max_chars: 4096 }),
      toolResult(fetchResult),
      assistantToolCall("search_web", { query: "portada el pais noticias destacadas", count: 3 }),
      toolResult(searchResult),
      { role: "user", content: "Resume ahora" },
    ],
  });

  const s = analyze(c.text);
  const drop = isDrop(s);
  console.log(
    `#${String(roundNo).padEnd(3)} interleaved: rd=${s.rd} rlen=${s.rlen} clen=${s.clen} ` +
      `toolEvents=${s.toolEvents} fin=${s.finish}` +
      (drop ? "  <-- DROP SIGNATURE (rd<=1 with content)" : "  ok"),
  );
  return { drop, s };
}

/**
 * SINGLE round — one tool call (smart_fetch_url): discovery request (no
 * `tools`; the OWUI harness injects the model's attached tools), real tool
 * execution, OpenAI-style continuation.
 */
async function singleRound(roundNo, url) {
  // ── Step 1 · discovery ────────────────────────────────────────────────
  // Plain request WITHOUT a `tools` field: the OWUI harness injects the
  // model's attached tools (meta.toolIds → smart_fetch_url) into the pipe
  // payload, so the model can call them. The call arrives as markdown
  // <tool_calls> in `content` (OWUI pipe convention), not structured
  // tool_calls deltas. Skip the round if the model did not call it.
  const d = await chat({
    model: MODEL, stream: true,
    messages: [{ role: "user", content: `Haz fetch de ${url} y cuéntame qué contiene` }],
  });
  const disc = analyze(d.text);
  if (!/smart_fetch_url/.test(d.text)) {
    console.log(`#${String(roundNo).padEnd(3)} single: no smart_fetch_url call (rd=${disc.rd},clen=${disc.clen}) — skipping round`);
    return null;
  }

  // ── Step 2 · execute the real tool ────────────────────────────────────
  // The client plays the OWUI frontend: run the actual smart_fetch_url
  // (curl_cffi beats the anti-bot blocks my plain fetch hit with 403).
  const toolOut = execTool(url).trim();

  // ── Step 3 · continuation (the drop scenario) ─────────────────────────
  // OpenAI-style round-trip: assistant message with structured tool_calls
  // + `tool` role result. THIS is what makes the pipe take the
  // `_history_has_tool_calls()` path (ships tools + forces
  // reasoning_content) where the HANDOFF says the reasoning drop lives.
  const c = await chat({
    model: MODEL, stream: true,
    messages: [
      { role: "system", content: SYSTEM_PROMPT },
      { role: "user", content: `Haz fetch de ${url} y cuéntame qué contiene` },
      assistantToolCall("smart_fetch_url", { urls: [url], format: "skimmd", max_chars: 4096 }),
      toolResult(toolOut),
      { role: "user", content: "Resume ahora lo que contiene según el resultado de la herramienta" },
    ],
  });
  const s = analyze(c.text);
  const drop = isDrop(s);
  console.log(
    `#${String(roundNo).padEnd(3)} single: rd=${s.rd} rlen=${s.rlen} clen=${s.clen} ` +
      `toolEvents=${s.toolEvents} fin=${s.finish}` +
      (drop ? "  <-- DROP SIGNATURE (rd<=1 with content)" : "  ok"),
  );
  return { drop, s };
}

async function main() {
  if (!KEY) {
    console.error("OWUI_API_KEY is required (env) — do not hardcode credentials.");
    process.exit(2);
  }
  if (!["interleaved", "single"].includes(MODE)) {
    console.error(`unknown mode '${MODE}' (interleaved | single)`);
    process.exit(2);
  }
  console.log(`OWUI: ${BASE}/api/chat/completions | model: ${MODEL} | mode: ${MODE} | rounds: ${ROUNDS} | url: ${URL}\n`);

  const roundFn = MODE === "interleaved" ? interleavedRound : singleRound;
  let drops = 0, done = 0;
  for (let i = 1; i <= ROUNDS; i++) {
    const r = await roundFn(i, URL);
    if (r?.drop) drops++;
    if (r) done++;
  }
  console.log(`\nRounds completed: ${done}/${ROUNDS} | drop signature (rd<=1, clen>0): ${drops}/${done}`);
  if (drops > 0) {
    console.log("Conclusion: reasoning drop signature reproduced on the real OWUI→pipe→Bifrost stack.");
    process.exit(1);
  }
  console.log("No drop signature on this run (bug is intermittent — see HANDOFF).");
  process.exit(0);
}

await main();
