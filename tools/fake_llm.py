"""Поддельный OpenAI-совместимый LLM со стримингом — проверить конвейер без ключа API.

    python tools/fake_llm.py            (слушает 127.0.0.1:8099)
    ALICE_LLM_BASE_URL=http://127.0.0.1:8099/v1 python -m alice_server
"""
import asyncio
import json

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

app = FastAPI()


@app.post("/v1/chat/completions")
async def completions(r: Request):
    body = await r.json()
    q = body["messages"][-1]["content"]
    answer = (f"<think>думаю</think>Вы спросили: {q} Это проверочный ответ, он приходит по кусочкам, "
              "как у настоящей модели. Озвучка должна начаться ещё до того, как текст закончится. Что-нибудь ещё?")

    async def gen():
        for i in range(0, len(answer), 4):
            chunk = {"choices": [{"delta": {"content": answer[i:i + 4]}}]}
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            await asyncio.sleep(0.02)  # ~200 символов/с
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8099, log_level="warning")
