"""A/B probe: replaying REAL reasoning_content (what the OWUI monkey patch
enables) vs a single-space placeholder (pipe forcing without patch) — does it
change the model's reasoning on tool-call continuations through LiteLLM?

Scenario (multi-tool-call, DeepSeek thinking-mode style):
    user: "What will the weather be in Madrid tomorrow? Use the tools."
    Turn 1: model reasons + calls get_date
    Continuation A (no patch): assistant carries reasoning_content: " "
    Continuation B (patch):    assistant carries the REAL reasoning from turn 1
    Turn 2 (both legs, same tool result): model calls get_weather, answers

Metric: reasoning presence/length on the continuation (turn 2).

Usage: .venv/bin/python probes/litellm/03_replay_ab.py [rounds=6]
"""

import asyncio
import os
import sys

import httpx

BASE = os.environ.get("LITELLM_BASE", "http://litellm.private")
KEY = os.environ.get(
    "LITELLM_KEY",
    "sk-lllm-api-OWFkOGY2NDZTc1YQo_ZmI41APN18Ec1KSzYjQ2NTcwNjBhYmQxwMjmOWEwYTA1OWFiY2QKGuk-1KSzsgkw0azF-w118EKSz",
)
MODEL = os.environ.get("LITELLM_MODEL", "deepseek/deepseek-v4-flash")
ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 6
EFFORT = os.environ.get("LITELLM_EFFORT", "high")

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_date",
            "description": "Get the current date (ISO).",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the weather for a city on a date.",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string"},
                    "date": {"type": "string", "description": "ISO date"},
                },
                "required": ["city", "date"],
            },
        },
    },
]


async def chat(client: httpx.AsyncClient, messages: list) -> dict:
    r = await client.post(
        f"{BASE}/v1/chat/completions",
        json={
            "model": MODEL,
            "stream": False,
            "reasoning_effort": EFFORT,
            "thinking": {"type": "enabled"},
            "tools": TOOLS,
            "messages": messages,
        },
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"},
        timeout=120,
    )
    r.raise_for_status()
    return r.json()


def rlen(m: dict) -> int:
    return len(m.get("reasoning_content") or "")


async def one_round(client: httpx.AsyncClient) -> dict:
    # Turn 1: model reasons and calls get_date
    t1 = await chat(client, [
        {"role": "user", "content": "What will the weather be in Madrid tomorrow? Use the tools."},
    ])
    m1 = t1["choices"][0]["message"]
    tc = (m1.get("tool_calls") or [None])[0]
    if not tc:
        raise RuntimeError("turn 1 did not produce a tool call")

    base = [
        {"role": "user", "content": "What will the weather be in Madrid tomorrow? Use the tools."},
        {
            "role": "assistant",
            "content": m1.get("content") or "",
            "tool_calls": [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["function"]["name"],
                        "arguments": tc["function"]["arguments"],
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": tc["id"], "content": '{"date": "2026-09-01"}'},
    ]

    results = {}
    for tag in ("A", "B"):
        messages = [dict(m) for m in base]
        messages[1]["reasoning_content"] = (
            " " if tag == "A" else (m1.get("reasoning_content") or " ")
        )
        t2 = await chat(client, messages)
        m2 = t2["choices"][0]["message"]
        results[tag] = {
            "t1Len": rlen(m1),
            "t2Reasoned": rlen(m2) > 0,
            "t2Len": rlen(m2),
            "t2Preview": (m2.get("reasoning_content") or "")[:70],
            "t2Tool": (m2.get("tool_calls") or [{}])[0].get("function", {}).get("name", "(final)"),
        }
    return results


async def main() -> None:
    summary = {"A": {"reasoned": 0, "lens": []}, "B": {"reasoned": 0, "lens": []}}
    rounds = 0
    async with httpx.AsyncClient() as client:
        for r in range(1, ROUNDS + 1):
            try:
                res = await one_round(client)
                rounds += 1
                for tag in ("A", "B"):
                    if res[tag]["t2Reasoned"]:
                        summary[tag]["reasoned"] += 1
                    summary[tag]["lens"].append(res[tag]["t2Len"])
                    print(
                        f"round {r} {tag}: t1Len={res[tag]['t1Len']} "
                        f"t2Reasoned={res[tag]['t2Reasoned']} t2Len={res[tag]['t2Len']} "
                        f"t2Tool={res[tag]['t2Tool']} preview={res[tag]['t2Preview']!r}"
                    )
            except Exception as e:
                print(f"round {r}: ERROR {e}")

    print(f"\n=== verdict ({rounds} rounds, effort={EFFORT}) ===")
    for tag in ("A", "B"):
        s = summary[tag]
        avg = sum(s["lens"]) / len(s["lens"]) if s["lens"] else 0
        label = "placeholder ' '" if tag == "A" else "real reasoning"
        print(f"{tag} ({label}): reasoned {s['reasoned']}/{len(s['lens'])} times, avg reasoning len {avg:.1f}")
    sumA = sum(summary["A"]["lens"])
    sumB = sum(summary["B"]["lens"])
    better = summary["B"]["reasoned"] > summary["A"]["reasoned"] or (
        summary["B"]["reasoned"] == summary["A"]["reasoned"] and sumB > sumA
    )
    print(
        "-> replaying real reasoning HELPS (continuation reasons more/richer)"
        if better
        else "-> no measurable benefit from replaying real reasoning"
    )


if __name__ == "__main__":
    asyncio.run(main())
