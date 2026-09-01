#!/usr/bin/env node
/**
 * A/B probe: does DeepSeek (via LiteLLM) drop reasoning on a tool-call
 * continuation when the replayed assistant message lacks `reasoning_content`?
 *
 * Test A: assistant replayed WITH reasoning_content (gateway-native form).
 * Test B: assistant replayed WITHOUT it (how Open WebUI reconstructs the
 *         assistant from stored output items).
 *
 * A drop = response message has no/empty `reasoning_content`.
 * Run several rounds; the Bifrost symptom was intermittent (~4/12, 1/8).
 *
 * Usage: node 01_toolcall_ab.js [rounds=6]
 */
const fs = require("fs");
const path = require("path");

const BASE = process.env.LITELLM_BASE || "http://litellm.private";
const KEY =
  process.env.LITELLM_KEY ||
  "sk-lllm-api-OWFkOGY2NDZTc1YQo_ZmI41APN18Ec1KSzYjQ2NTcwNjBhYmQxwMjmOWEwYTA1OWFiY2QKGuk-1KSzsgkw0azF-w118EKSz";
const MODEL = process.env.LITELLM_MODEL || "deepseek/deepseek-v4-flash";
const ROUNDS = parseInt(process.argv[2] || "6", 10);

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

async function chat(messages, stream = false) {
  const res = await fetch(`${BASE}/v1/chat/completions`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${KEY}`,
    },
    body: JSON.stringify({
      model: MODEL,
      stream,
      reasoning_effort: "low",
      tools: TOOLS,
      messages,
    }),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
  return res.json();
}

function isDrop(msg) {
  return !msg || !msg.reasoning_content || msg.reasoning_content === "";
}

async function oneRound() {
  // 1) first call -> tool call (produce a real tool_call id)
  const first = await chat([
    { role: "user", content: "¿Qué tiempo hace en Madrid? Usa la herramienta." },
  ]);
  const m = first.choices[0].message;
  const tc = m.tool_calls?.[0];
  if (!tc) throw new Error("model did not call the tool");

  const base = [
    { role: "user", content: "¿Qué tiempo hace en Madrid? Usa la herramienta." },
    {
      role: "assistant",
      content: m.content || "",
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
  for (const tag of ["A", "B"]) {
    const messages = JSON.parse(JSON.stringify(base));
    if (tag === "A") {
      messages[1].reasoning_content = m.reasoning_content || "";
    }
    const out = await chat(messages);
    const msg = out.choices[0].message;
    results[tag] = {
      drop: isDrop(msg),
      rcLen: msg.reasoning_content ? msg.reasoning_content.length : 0,
      preview: (msg.reasoning_content || "").slice(0, 60),
      finish: out.choices[0].finish_reason,
    };
  }
  return results;
}

(async () => {
  let drops = { A: 0, B: 0 };
  let rounds = 0;
  for (let r = 1; r <= ROUNDS; r++) {
    try {
      const res = await oneRound();
      rounds++;
      for (const tag of ["A", "B"]) {
        if (res[tag].drop) drops[tag]++;
        console.log(
          `round ${r} test ${tag}: drop=${res[tag].drop} rcLen=${res[tag].rcLen} ` +
            `preview=${JSON.stringify(res[tag].preview)} finish=${res[tag].finish}`
        );
      }
    } catch (e) {
      console.log(`round ${r}: ERROR ${e.message}`);
    }
  }
  console.log(`\n=== verdict (${rounds} rounds) ===`);
  for (const tag of ["A", "B"]) {
    console.log(`test ${tag}: ${drops[tag]}/${rounds} drops`);
  }
  console.log(
    drops.B > drops.A
      ? "-> forcing reasoning_content MATTERS with LiteLLM (keep the forcing step)"
      : "-> no measurable difference (forcing may be redundant on LiteLLM)"
  );
})();
