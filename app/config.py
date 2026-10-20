"""环境配置。

生产环境密钥由部署平台注入，禁止提交 .env。
DEMO_MODE=true 时使用内存数据源，便于本地与 Netlify 无数据库部署。
"""

import os
import secrets


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    def __init__(self) -> None:
        self.APP_NAME = "邻里充电运营平台"
        self.DEMO_MODE = _as_bool(os.getenv("DEMO_MODE"), True)
        # 身份中心（MySQL）与业务库（PostgreSQL）连接串
        self.MYSQL_URL = os.getenv("MYSQL_URL", "")
        self.POSTGRES_URL = os.getenv("POSTGRES_URL", "")
        self.REDIS_URL = os.getenv("REDIS_URL", "")
        # 令牌
        self.JWT_SECRET = os.getenv("JWT_SECRET") or secrets.token_hex(32)
        self.JWT_ALG = "HS256"
        self.ACCESS_TOKEN_TTL = int(os.getenv("ACCESS_TOKEN_TTL", "3600"))
        self.REFRESH_TOKEN_TTL = int(os.getenv("REFRESH_TOKEN_TTL", "86400"))
        # 登录失败限流
        self.LOGIN_MAX_FAILS = int(os.getenv("LOGIN_MAX_FAILS", "5"))
        self.LOGIN_LOCK_SECONDS = int(os.getenv("LOGIN_LOCK_SECONDS", "300"))
        # LLM（Agent 在 DEMO_MODE 下使用规则引擎，无需真实密钥）
        self.LLM_MODEL = os.getenv("LLM_MODEL", "rule-engine")
        self.LLM_API_KEY = os.getenv("LLM_API_KEY", "")
        # 工单关闭验收：心跳新鲜度窗口（分钟）
        self.HEARTBEAT_FRESH_MINUTES = int(os.getenv("HEARTBEAT_FRESH_MINUTES", "60"))


settings = Settings()
