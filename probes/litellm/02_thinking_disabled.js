#!/usr/bin/env node
/**
 * Probe: does `thinking: {"type": "disabled"}` (sent by Open WebUI on
 * server-side tool-call continuations) actually kill DeepSeek reasoning
 * when routed through LiteLLM?
 *
 * Test T: continuation WITH thinking disabled in body (OWUI shape).
 * Test C: continuation WITHOUT the thinking field (what the pipe would send).
 * Drop = response has no/empty reasoning_content. High effort to avoid
 * model-chooses-not-to-reason noise on trivial tasks.
 *
 * Usage: node 02_thinking_disabled.js [rounds=4]
 */
const BASE = process.env.LITELLM_BASE || "http://litellm.private";
const KEY =
  process.env.LITELLM_KEY ||
  "sk-lllm-api-OWFkOGY2NDZTc1YQo_ZmI41APN18Ec1KSzYjQ2NTcwNjBhYmQxwMjmOWEwYTA1OWFiY2QKGuk-1KSzsgkw0azF-w118EKSz";
const MODEL = process.env.LITELLM_MODEL || "deepseek/deepseek-v4-flash";
const ROUNDS = parseInt(process.argv[2] || "4", 10);

const TOOLS = [
  {
    type: "function",
    function: {
      name: "get_weather",
      description: "Get current weather for a city",
      parameters: {
        type: "object",
        properties: { city: { type: "string" } },
        required: ["city"],
      },
    },
  },
];

async function chat(messages, extra = {}) {
  const res = await fetch(`${BASE}/v1/chat/completions`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${KEY}`,
    },
    body: JSON.stringify({
      model: MODEL,
      stream: false,
      reasoning_effort: "high",
      tools: TOOLS,
      messages,
      ...extra,
    }),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
  return res.json();
}

function isDrop(msg) {
  return !msg || !msg.reasoning_content || msg.reasoning_content === "";
}

async function oneRound() {
  const first = await chat([
    { role: "user", content: "What is the weather in Madrid? Use the tool." },
  ]);
  const m = first.choices[0].message;
  const tc = m.tool_calls?.[0];
  if (!tc) throw new Error("model did not call the tool");

  const base = [
    { role: "user", content: "What is the weather in Madrid? Use the tool." },
    {
      role: "assistant",
      content: m.content || "",
      reasoning_content: m.reasoning_content || "",
      tool_calls: [
        {
          id: tc.id,
          type: "function",
          function: { name: tc.function.name, arguments: tc.function.arguments },
        },
      ],
    },
    {
      role: "tool",
      tool_call_id: tc.id,
      content: '{"temp": 28, "city": "Madrid", "sky": "sunny"}',
    },
  ];

  const results = {};
  // C: clean (no thinking field) — what the pipe sends after stripping
  const c = await chat(base);
  results.C = {
    drop: isDrop(c.choices[0].message),
    rcLen: c.choices[0].message.reasoning_content?.length || 0,
  };
  // T: thinking disabled — OWUI's raw server-side continuation shape
  const t = await chat(base, { thinking: { type: "disabled" } });
  results.T = {
    drop: isDrop(t.choices[0].message),
    rcLen: t.choices[0].message.reasoning_content?.length || 0,
  };
  return results;
}

(async () => {
  let drops = { C: 0, T: 0 };
  let rounds = 0;
  for (let r = 1; r <= ROUNDS; r++) {
    try {
      const res = await oneRound();
      rounds++;
      for (const tag of ["C", "T"]) {
        if (res[tag].drop) drops[tag]++;
        console.log(
          `round ${r} test ${tag}: drop=${res[tag].drop} rcLen=${res[tag].rcLen}`
        );
      }
    } catch (e) {
      console.log(`round ${r}: ERROR ${e.message}`);
    }
  }
  console.log(`\n=== verdict (${rounds} rounds, high effort) ===`);
  console.log(`clean (no thinking):    ${drops.C}/${rounds} drops`);
  console.log(`thinking disabled:      ${drops.T}/${rounds} drops`);
  console.log(
    drops.T > drops.C
      ? "-> thinking:disabled DOES kill reasoning through LiteLLM (keep _normalize_thinking_for_gateway)"
      : "-> no difference; thinking:disabled is harmless/ignored by LiteLLM (can drop the strip)"
  );
})();
