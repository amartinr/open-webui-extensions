#!/usr/bin/env node
/**
 * Bifrost reasoning-loss probe (integration test, requires a live Bifrost).
 *
 * Reproduces the DeepSeek reasoning drop seen in Open WebUI on tool-call
 * continuation turns and pinpoints WHERE the reasoning disappears. This is
 * the empirical counterpart of test_bifrost_reasoning_normalization.py:
 * that file unit-tests the payload rewrite, this script tests the real
 * gateway end-to-end.
 *
 * Background (see repo HANDOFF.md for the full investigation):
 *
 *   - Bifrost core v1.6.3 routed DeepSeek through stripReasoningDetails(),
 *     which nulled `reasoning_content` on EVERY assistant message including
 *     tool-call turns, violating DeepSeek's asymmetric contract (fixed in
 *     core v1.7.10 via stripReasoningDetailsExceptToolCalls, issue #5887).
 *   - Independently of that request-side fix, Bifrost's SSE stream can drop
 *     the reasoning deltas under load: a request that returns full
 *     `reasoning` in non-streaming mode can emit only the empty opening
 *     delta (`{"reasoning":"","reasoning_details":[{"text":""}]}`) in
 *     streaming mode (the `reasoning_deltas=1` signature seen in the pipe
 *     logs, cf. upstream issue #6523 "streaming drops opening role-only
 *     delta").
 *
 * This probe runs both modes on the SAME payload and reports mismatches, so
 * it can tell the two failure modes apart:
 *
 *   - stream drops reasoning but non-stream has it  -> SSE loss in Bifrost
 *   - both modes lack reasoning                     -> request-side issue
 *     (replay shape / DeepSeek refusal)
 *
 * Usage:
 *
 *   BIFROST_BASE_URL=http://bifrost.private/v1 \
 *   BIFROST_API_KEY=bf-vk-... \
 *   node pipes/agent_loop_guard/tests/repro_bifrost_reasoning_loss.mjs [rounds] [mode]
 *
 *   rounds: number of probe iterations (default 6)
 *   mode:   'roundtrip' (default, tool-call continuation) | 'plain' | 'tools'
 *
 * The API key is read from BIFROST_API_KEY (env) or, failing that, from the
 * pi agent models.json at /srv/pi/.pi/agent/models.json (key
 * providers.bifrost.apiKey) — never hardcode credentials in this repo.
 *
 * Exit code 0 = no mismatches, 1 = at least one SSE-reasoning mismatch,
 * 2 = no reasoning at all in either mode (request-side drop).
 */

import { readFileSync } from "node:fs";

const BASE = process.env.BIFROST_BASE_URL ?? "http://bifrost.private/v1";
const MODEL = process.env.BIFROST_MODEL ?? "deepseek/deepseek-v4-flash";
const KEY =
  process.env.BIFROST_API_KEY ??
  (() => {
    try {
      return JSON.parse(
        readFileSync("/srv/pi/.pi/agent/models.json", "utf8"),
      ).providers.bifrost.apiKey;
    } catch {
      return "";
    }
  })();

if (!KEY) {
  console.error(
    "BIFROST_API_KEY is required (or place it in providers.bifrost.apiKey of models.json).",
  );
  process.exit(2);
}

const ROUNDS = Number(process.argv[2] || 6);
const MODE = process.argv[3] || "roundtrip";

const REQUEST_TIMEOUT_MS = 45000;

function tools() {
  return [
    {
      type: "function",
      function: {
        name: "bash",
        description: "run a shell command",
        parameters: {
          type: "object",
          properties: { command: { type: "string" } },
          required: ["command"],
        },
      },
    },
  ];
}

async function call(payload) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const res = await fetch(`${BASE}/chat/completions`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${KEY}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
    const text = await res.text();
    return { status: res.status, text };
  } catch (err) {
    return { status: `ERR:${err.name}`, text: "" };
  } finally {
    clearTimeout(timer);
  }
}

/** Non-streaming: count reasoning chars in message.reasoning(-_content). */
function analyzeNonStream(text) {
  try {
    const json = JSON.parse(text);
    const msg = json.choices?.[0]?.message ?? {};
    return {
      reasoningLen: (msg.reasoning || msg.reasoning_content || "").length,
      finish: json.choices?.[0]?.finish_reason ?? "",
      messageKeys: Object.keys(msg).join(","),
    };
  } catch {
    return { reasoningLen: -1, finish: "PARSE_FAIL", messageKeys: "" };
  }
}

/** Streaming: count reasoning deltas/chars plus presence of content/tools. */
function analyzeStream(text) {
  let reasoningDeltas = 0;
  let reasoningLen = 0;
  let hasContent = false;
  let hasToolCalls = false;
  let finish = "";
  for (const line of text.split("\n")) {
    const s = line.trim();
    if (!s.startsWith("data:") || s === "data: [DONE]") continue;
    try {
      const ev = JSON.parse(s.slice(5).trim());
      const d = ev.choices?.[0]?.delta;
      if (!d) continue;
      if (
        d.reasoning !== undefined ||
        d.reasoning_content !== undefined ||
        d.reasoning_details !== undefined
      ) {
        reasoningDeltas++;
        const rc =
          d.reasoning ?? d.reasoning_content ?? d.reasoning_details?.[0]?.text ?? "";
        if (typeof rc === "string") reasoningLen += rc.length;
      }
      if (d.content) hasContent = true;
      if (d.tool_calls) hasToolCalls = true;
      if (ev.choices?.[0]?.finish_reason) finish = ev.choices[0].finish_reason;
    } catch {
      /* non-JSON SSE line — ignore */
    }
  }
  return { reasoningDeltas, reasoningLen, hasContent, hasToolCalls, finish };
}

/**
 * Build the tool-call continuation payload: first a live call that reasons
 * and emits a tool call, then a continuation that replays the REAL reasoning
 * + the REAL tool_calls (what Open WebUI / pi actually send back).
 */
async function buildRoundtripPayload() {
  const first = await call({
    model: MODEL,
    stream: true,
    reasoning_effort: "low",
    thinking: { type: "enabled" },
    tools: tools(),
    messages: [
      { role: "system", content: "agent" },
      { role: "user", content: "run ls in the current directory and tell me what you saw" },
    ],
  });

  let reasoning = "";
  const tcAcc = [];
  for (const line of first.text.split("\n")) {
    const s = line.trim();
    if (!s.startsWith("data:")) continue;
    try {
      const ev = JSON.parse(s.slice(5).trim());
      const d = ev.choices?.[0]?.delta;
      if (!d) continue;
      const rc =
        d.reasoning ?? d.reasoning_content ?? d.reasoning_details?.[0]?.text ?? "";
      if (typeof rc === "string") reasoning += rc;
      if (d.tool_calls) {
        for (const tcd of d.tool_calls) {
          if (!tcAcc[tcd.index]) tcAcc[tcd.index] = { id: "", name: "", args: "" };
          if (tcd.id) tcAcc[tcd.index].id += tcd.id;
          if (tcd.function?.name) tcAcc[tcd.index].name += tcd.function.name;
          if (tcd.function?.arguments) tcAcc[tcd.index].args += tcd.function.arguments;
        }
      }
    } catch {
      /* ignore */
    }
  }

  const realToolCalls = tcAcc.filter(Boolean).map((t) => ({
    id: t.id || "call_x",
    type: "function",
    function: { name: t.name || "bash", arguments: t.args || "{}" },
  }));

  return {
    model: MODEL,
    reasoning_effort: "low",
    thinking: { type: "enabled" },
    tools: tools(),
    messages: [
      { role: "system", content: "agent" },
      { role: "user", content: "run ls in the current directory and tell me what you saw" },
      {
        role: "assistant",
        content: "",
        reasoning_content: reasoning,
        tool_calls: realToolCalls,
      },
      {
        role: "tool",
        tool_call_id: realToolCalls[0]?.id ?? "call_x",
        name: realToolCalls[0]?.function.name ?? "bash",
        content: "proc.c readme.md",
      },
      { role: "user", content: "now analyze step by step and summarize" },
    ],
  };
}

function buildSimplePayload(withTools) {
  const payload = {
    model: MODEL,
    reasoning_effort: "low",
    thinking: { type: "enabled" },
    messages: [
      { role: "system", content: "agent" },
      { role: "user", content: "analyze step by step and summarize what this project contains" },
    ],
  };
  if (withTools) payload.tools = tools();
  return payload;
}

async function probe(mode) {
  const payload =
    mode === "roundtrip"
      ? await buildRoundtripPayload()
      : buildSimplePayload(mode === "tools");
  const nonStream = await call({ ...payload, stream: false });
  const stream = await call({ ...payload, stream: true });

  const ns = analyzeNonStream(nonStream.text);
  const s = analyzeStream(stream.text);

  const sseMismatch = ns.reasoningLen > 0 && s.reasoningLen === 0;
  const requestDrop = ns.reasoningLen === 0 && s.reasoningLen === 0;

  console.log(
    `#${mode.padEnd(10)} NS(len=${ns.reasoningLen},fin=${ns.finish}) | ` +
      `S(rd=${s.reasoningDeltas},len=${s.reasoningLen},content=${s.hasContent},` +
      `tool=${s.hasToolCalls},fin=${s.finish})` +
      (sseMismatch
        ? "  <-- SSE LOST REASONING (Bifrost stream dropped it; non-stream has it)"
        : requestDrop
          ? "  <-- REQUEST-SIDE DROP (no reasoning in either mode)"
          : "  ok"),
  );
  return { sseMismatch, requestDrop, ns, s };
}

async function main() {
  console.log(`Bifrost: ${BASE} | model: ${MODEL} | mode: ${MODE} | rounds: ${ROUNDS}\n`);
  let sseMismatches = 0;
  let requestDrops = 0;
  for (let i = 0; i < ROUNDS; i++) {
    const r = await probe(MODE);
    if (r.sseMismatch) sseMismatches++;
    if (r.requestDrop) requestDrops++;
  }
  console.log(`\nSSE-reasoning mismatches: ${sseMismatches}/${ROUNDS}`);
  console.log(`Request-side drops:       ${requestDrops}/${ROUNDS}`);
  if (sseMismatches > 0) {
    console.log(
      "Conclusion: Bifrost SSE drops reasoning deltas under load (cf. issue #6523). " +
        "Not a payload/model issue — the same request has reasoning in non-stream mode.",
    );
    process.exit(1);
  }
  if (requestDrops > 0) {
    console.log(
      "Conclusion: no reasoning in either mode — request-side / DeepSeek refusal. " +
        "Check the replayed reasoning_content and the Bifrost core version " +
        "(core >= 1.7.10 required for stripReasoningDetailsExceptToolCalls, #5887).",
    );
    process.exit(2);
  }
  console.log("No reasoning loss detected on this run.");
  process.exit(0);
}

await main();
