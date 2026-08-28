# open-webui-extensions

Collection of filters, pipes, tools, and adapters for [Open WebUI](https://docs.openwebui.com/).

## Components

### Pipes

| Component | Version | Purpose |
|---|---|---|
| [Agent Loop Guard](pipes/agent_loop_guard/) | 2.15.0 | Intercepts tool-calling loops; proxies requests to the gateway; normalizes Bifrost/DeepSeek reasoning on tool-call continuations. |

### Filters

| Component | Version | Purpose |
|---|---|---|
| [Bifrost reasoning_content fix](filters/bifrost_reasoning_content_fix/) | 3.5.0 | Converts Bifrost's non-standard `reasoning`/`reasoning_details` fields to `reasoning_content` in stream and history. |
| [DeepSeek Reasoning Effort Selector](filters/deepseek_reasoning/) | 1.3.2 | Per-model `reasoning_effort` control for DeepSeek models. |
| [Image to File Storage](filters/image_filter/) | 2.12.3 | Persists pasted images as files and injects `<attached_files>` blocks. |
| [RAG mode selector](filters/rag_mode_selector/) | 1.0.0 | Toggles RAG context injection per request (`rag_default_off` / `rag_enable`). |

### Tools

| Component | Version | Purpose |
|---|---|---|
| [Smart Fetch URL](tools/smart_fetch_url/) | 0.10.0 | Fetches URLs with TLS fingerprinting and content extraction. |
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

The reasoning components ([Agent Loop Guard](pipes/agent_loop_guard/),
[Bifrost reasoning_content fix](filters/bifrost_reasoning_content_fix/)) are
validated against this stack:

| Layer | Version | Notes |
|---|---|---|
| Open WebUI | 0.11.1 | `get_reasoning_format()` returns `None` for pipe models → history replay needs the monkey patch |
| Bifrost | 2.0.0 (core 1.8.3) | Requires core ≥ 1.8.0 for SSE `reasoning_content` in stream deltas ([#6523](https://github.com/maximhq/bifrost/issues/6523)); core ≥ 1.7.10 for tool-call reasoning replay ([#5887](https://github.com/maximhq/bifrost/issues/5887)) |
| DeepSeek | v4 flash/pro | Requires `reasoning_content` replayed on tool-call continuations |

On Bifrost 2.0.0 the reasoning path is verified clean (0/34 SSE mismatches
with `pipes/agent_loop_guard/tests/repro_bifrost_reasoning_loss.mjs`). The
pipe and filter remain necessary: stream deltas still carry
`reasoning_details`, which Open WebUI v0.11.1 suppresses from the live
reasoning event unless stripped.

## Development

```bash
python3 -m pytest filters/ pipes/agent_loop_guard/tests/ -q
```

## License

MIT — see [LICENSE](./LICENSE).
