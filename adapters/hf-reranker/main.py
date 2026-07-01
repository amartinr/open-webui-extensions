from fastapi import FastAPI, Request
import requests

app = FastAPI()


@app.post("/v1/rerank")
async def rerank(request: Request, payload: dict):
    query = payload["query"]
    docs = payload["documents"]
    model = payload["model"]  # Ej: "BAAI/bge-reranker-v2-m3"
    auth = request.headers.get("Authorization")

    inputs = [{"text": query, "text_pair": doc} for doc in docs]

    resp = requests.post(
        f"https://router.huggingface.co/hf-inference/models/{model}",
        headers={"Authorization": auth},
        json={"inputs": inputs},
    )

    scores = resp.json()
    results = [
        {"index": i, "relevance_score": s["score"]}
        for i, s in enumerate(scores[0])
    ]
    return {"results": results}
