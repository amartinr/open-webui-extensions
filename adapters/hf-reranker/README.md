# HF Reranker Adapter

Proxy service that translates Open WebUI rerank requests into Hugging Face
Inference API calls.

## Why this adapter is needed

Open WebUI expects reranking services to expose a `/v1/rerank` endpoint
compatible with the [OpenAI-compatible rerank API](https://platform.openai.com/docs/api-reference/rerank).
However, Hugging Face's Inference API does **not** provide a `/v1/rerank`
endpoint — it only exposes a generic inference endpoint at:

```
POST https://router.huggingface.co/hf-inference/models/{model}
```

This adapter bridges that gap:
- It exposes a `/v1/rerank` endpoint that Open WebUI can call.
- It translates the request into the format expected by HF's inference API.
- It maps the raw HF response back into the structure Open WebUI expects.

```
Open WebUI                    This adapter                   HF Inference API
─────────                    ─────────────                   ────────────────
POST /v1/rerank           →  POST /v1/rerank             →  POST /hf-inference/models/{model}
{ query, documents, model }  { query, documents, model }    { inputs: [{text, text_pair}, ...] }
                             ←  { results: [...] }       ←  [{ score, ... }, ...]
```

## Setup

```bash
pip install -r requirements.txt
uvicorn main:app --port 8000
```

Then configure Open WebUI to use `http://localhost:8000/v1/rerank` as the
reranking endpoint.

## Endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/rerank` | Rerank documents for a query |

### Request

```json
{
  "query": "string",
  "documents": ["doc1", "doc2", "..."],
  "model": "BAAI/bge-reranker-v2-m3"
}
```

### Response

```json
{
  "results": [
    { "index": 0, "relevance_score": 0.95 },
    { "index": 1, "relevance_score": 0.42 }
  ]
}
```
