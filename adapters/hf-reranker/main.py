import os

from fastapi import FastAPI, HTTPException, Request
import requests

DEFAULT_INFERENCE_URL = "https://router.huggingface.co/hf-inference/models"
DEFAULT_TIMEOUT = 30
ERROR_DETAIL_MAX_CHARS = 500

HF_INFERENCE_URL = os.getenv("HF_INFERENCE_URL", DEFAULT_INFERENCE_URL)
HF_TIMEOUT = float(os.getenv("HF_TIMEOUT", DEFAULT_TIMEOUT))

app = FastAPI()


@app.post("/v1/rerank")
async def rerank(request: Request, payload: dict):
    query = payload["query"]
    docs = payload["documents"]
    model = payload["model"]
    auth = request.headers.get("Authorization")

    inputs = [{"text": query, "text_pair": doc} for doc in docs]

    try:
        resp = requests.post(
            f"{HF_INFERENCE_URL}/{model}",
            headers={"Authorization": auth},
            json={"inputs": inputs},
            timeout=HF_TIMEOUT,
        )
        resp.raise_for_status()
        scores = resp.json()
        results = [
            {"index": i, "relevance_score": s["score"]}
            for i, s in enumerate(scores[0])
        ]
    except requests.HTTPError as exc:
        raise HTTPException(
            status_code=exc.response.status_code,
            detail=exc.response.text[:ERROR_DETAIL_MAX_CHARS],
        )
    except requests.Timeout:
        raise HTTPException(status_code=504, detail="HF Inference timed out")
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except (ValueError, IndexError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    return {"results": results}


if __name__ == "__main__":
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="HF Reranker adapter for Open WebUI")
    parser.add_argument(
        "--inference-url",
        default=HF_INFERENCE_URL,
        help="HF Inference base URL (env: HF_INFERENCE_URL)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=HF_TIMEOUT,
        help="Request timeout in seconds (env: HF_TIMEOUT)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("PORT", "8000")),
        help="Port to listen on (env: PORT)",
    )
    args = parser.parse_args()

    HF_INFERENCE_URL = args.inference_url
    HF_TIMEOUT = args.timeout

    uvicorn.run(app, host="0.0.0.0", port=args.port)
