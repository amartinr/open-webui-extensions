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

## Development

```bash
python3 -m pytest filters/ pipes/agent_loop_guard/tests/ -q
```

## License

MIT — see [LICENSE](./LICENSE).
