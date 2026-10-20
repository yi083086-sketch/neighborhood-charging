"""Agent 工作台入口：自然语言查询，支持 JSON 与 SSE 流式输出。"""

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from ..agents.graph import AgentRun
from ..auth import current_user
from ..models import AgentChatIn
from .common import trace_id

router = APIRouter(prefix="/api/agent", tags=["agent"])


@router.post("/chat")
def chat(body: AgentChatIn, request: Request, user: dict = Depends(current_user)):
    run = AgentRun(user, body.message, body.session_id)
    if body.stream:
        def gen():
            for event in run.run():
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"X-Accel-Buffering": "no",
                                          "Cache-Control": "no-cache"})
    for event in run.run():
        if "final" in event:
            return event["final"]
    return {"error": "agent failed"}


@router.get("/history/{session_id}")
def history(session_id: str, user: dict = Depends(current_user)):
    from .. import db

    return {"items": db.store.memory_short.get(session_id, [])}
