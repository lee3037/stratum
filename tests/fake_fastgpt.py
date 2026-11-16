"""本地假 FastGPT 服务，供浏览器冒烟测试使用：python tests/fake_fastgpt.py [端口]

只实现 Stratum 用到的接口（逻辑同 test_fastgpt.FakeFastGPT），API 密钥为 secret。
GET /_state 返回当前集合，便于测试检查。
"""
import sys
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_fastgpt import FakeFastGPT  # noqa: E402

fake = FakeFastGPT()
app = FastAPI()


@app.get("/_state")
def state():
    return {"collections": list(fake.collections.values())}


@app.api_route("/{path:path}", methods=["GET", "POST", "DELETE"])
async def proxy(path: str, request: Request):
    req = httpx.Request(request.method, str(request.url), headers=request.headers.raw, content=await request.body())
    res = fake(req)
    return Response(res.content, status_code=res.status_code, media_type="application/json")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]) if len(sys.argv) > 1 else 3999, log_level="warning")
