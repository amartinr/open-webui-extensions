import argparse
import os

from fastapi import FastAPI, HTTPException, Request
import requests

DEFAULT_INFERENCE_URL = "https://router.huggingface.co/hf-inference/models"
DEFAULT_TIMEOUT = 30

parser = argparse.ArgumentParser(description="HF Reranker adapter for Open WebUI")
parser.add_argument(
    "--inference-url",
    default=os.getenv("HF_INFERENCE_URL", DEFAULT_INFERENCE_URL),
    help="HF Inference base URL (env: HF_INFERENCE_URL)",
)
parser.add_argument(
    "--timeout",
    type=float,
    default=float(os.getenv("HF_TIMEOUT", DEFAULT_TIMEOUT)),
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

app = FastAPI()


@app.post("/v1/rerank")
async def rerank(request: Request, payload: dict):
    query = payload["query"]
    docs = payload["documents"]
    model = payload["model"]
    auth = request.headers.get("Authorization")

    inputs = [{"text": query, "text_pair": doc} for doc in docs]

    resp = requests.post(
        f"{HF_INFERENCE_URL}/{model}",
        headers={"Authorization": auth},
        json={"inputs": inputs},
        timeout=HF_TIMEOUT,
    )

    if not resp.ok:
        raise HTTPException(
            status_code=502,
            detail=f"HF Inference error ({resp.status_code}): {resp.text[:500]}",
        )

    scores = resp.json()
    results = [
        {"index": i, "relevance_score": s["score"]}
        for i, s in enumerate(scores[0])
    ]
    return {"results": results}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=args.port)
