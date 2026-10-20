"""应用入口和路由装配。

- 统一 trace_id 中间件与错误码规范（401/403/409/422）。
- /api/health 健康检查（含数据库探测）。
- 本地运行时同时托管 static/ 前端；Netlify 上静态资源由 CDN 托管，
  本应用以 Serverless Function 形式提供 /api。
"""

import logging
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .config import settings
from .routers import (agent_routes, approval_routes, auth_routes, device_routes,
                      order_routes, report_routes, reservation_routes,
                      ticket_routes)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("charging")

app = FastAPI(title=settings.APP_NAME, version="1.0.0",
              docs_url="/docs" if settings.DEMO_MODE else None,
              redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 演示模式放开；生产环境应配置为工作台域名
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Trace-ID"],
)


@app.middleware("http")
async def trace_context(request: Request, call_next):
    request.state.trace_id = request.headers.get("X-Trace-ID") or uuid.uuid4().hex
    response = await call_next(request)
    response.headers["X-Trace-ID"] = request.state.trace_id
    return response


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    tid = getattr(request.state, "trace_id", "")
    logger.exception("unhandled error trace_id=%s", tid)
    return JSONResponse(status_code=500,
                        content={"detail": {"code": "INTERNAL", "message": "服务内部错误",
                                            "trace_id": tid}},
                        headers={"X-Trace-ID": tid})


@app.get("/api/health")
def health():
    services = db.check_external_services()
    degraded = any(str(v).startswith("error") for v in services.values())
    if degraded:
        return JSONResponse(status_code=503, content={
            "status": "degraded", "code": "DB_UNAVAILABLE",
            "message": "数据库不可用，请查看运维手册故障处理章节", "services": services})
    return {"status": "ok", "app": settings.APP_NAME, "services": services}


for r in (auth_routes.router, device_routes.router, reservation_routes.router,
          order_routes.router, ticket_routes.router, approval_routes.router,
          report_routes.router, agent_routes.router):
    app.include_router(r)

# 本地运行托管前端静态资源
_static_dir = Path(__file__).resolve().parent.parent / "static"
if _static_dir.exists():
    app.mount("/", StaticFiles(directory=str(_static_dir), html=True), name="static")
