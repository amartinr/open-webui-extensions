# open-webui-extensions

Collection of filters, pipes, tools, and adapters for [Open WebUI](https://docs.openwebui.com/).

## Components

### Pipes

| Component | Version | Purpose |
|---|---|---|
| [Agent Loop Guard](pipes/agent_loop_guard/) | 2.17.5 | Intercepts tool-calling loops; proxies requests to the gateway; forces `reasoning_content` on tool-call continuations (DeepSeek contract) and optionally replays the real reasoning text. |

### Filters

| Component | Version | Purpose |
|---|---|---|
| [DeepSeek Reasoning Effort Selector](filters/deepseek_reasoning/) | 1.3.2 | Per-model `reasoning_effort` control for DeepSeek models. |
| [Image to File Storage](filters/image_filter/) | 2.12.3 | Persists pasted images as files and injects `<attached_files>` blocks. |
| [RAG mode selector](filters/rag_mode_selector/) | 1.0.0 | Toggles RAG context injection per request (`rag_default_off` / `rag_enable`). |

### Tools

| Component | Version | Purpose |
|---|---|---|
| [Smart Fetch URL](tools/smart_fetch_url/) | 0.11.2 | Fetches URLs with TLS fingerprinting and content extraction. |
| [YouTube Search](tools/youtube_search/) | 1.2.0 | Searches videos, channels, and playlists via the YouTube API. |

### Adapters

| Component | Purpose |
|---|---|
| [HF Reranker](adapters/hf-reranker/) | Proxy translating Open WebUI rerank requests into Hugging Face Inference API calls. |

## Deployment

Open WebUI loads functions from the database (Admin → Functions), not from
this repository. Copy the component source into the editor and save; restart
the service if `stream()` changed.

## Compatibility

The reasoning handling in [Agent Loop Guard](pipes/agent_loop_guard/) is
validated against this stack:

| Layer | Version | Notes |
|---|---|---|
| Open WebUI | 0.11.1 | `get_reasoning_format()` returns `None` for pipe models → history replay needs the monkey patch (`REPLAY_REASONING_TEXT` valve) |
| LiteLLM | current | OpenAI-compatible responses: native `reasoning_content` in stream and history; warns when the field is missing on tool-call continuations |
| DeepSeek | v4 flash/pro | Requires `reasoning_content` replayed on tool-call continuations (HTTP 400 if missing) |

The DeepSeek `reasoning_content` forcing is the provider contract, not a
gateway quirk — LiteLLM emits its own warning ("DeepSeek thinking mode")
when a tool-call continuation replays an assistant without it. A/B probes
live in `probes/litellm/` (placeholder vs real reasoning replay, thinking
`disabled` behavior, and the reasoning-replay verdict).

## Development

```bash
python3 -m pytest filters/ pipes/agent_loop_guard/tests/ -q
```

## License

MIT — see [LICENSE](./LICENSE).
