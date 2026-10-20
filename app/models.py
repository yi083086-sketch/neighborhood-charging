"""Pydantic 请求/响应模型（API 参数校验，防止非法输入进入业务层）。"""

from typing import Any, Literal

from pydantic import BaseModel, Field

# ---------- 认证 ----------


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class RefreshRequest(BaseModel):
    refresh_token: str


# ---------- 设备事件 ----------

EVENT_TYPES = {
    "heartbeat", "power", "current", "temperature",
    "offline", "charge_interrupted", "fault", "overload",
}


class DeviceEventIn(BaseModel):
    event_no: str = Field(min_length=1, max_length=64, description="事件编号，用于幂等去重")
    pile_id: str
    type: str
    severity: Literal["info", "warn", "critical"] = "info"
    payload: dict[str, Any] = Field(default_factory=dict)


# ---------- 预约 ----------


class ReservationIn(BaseModel):
    gun_id: str
    start_time: str = Field(description="ISO8601，如 2026-10-21T10:00")
    end_time: str
    note: str = ""


# ---------- 订单 ----------

ORDER_STATUS = {"pending_payment", "reserved", "charging", "completed", "cancelled", "abnormal"}


class OrderIn(BaseModel):
    gun_id: str
    reservation_id: str | None = None
    test_order: bool = Field(default=False, description="工单验收测试订单")
    amount: float = Field(default=0.0, ge=0)


# ---------- 工单 ----------


class TicketCreateIn(BaseModel):
    pile_id: str
    title: str = Field(min_length=1, max_length=128)
    priority: Literal["P1", "P2", "P3"] = "P3"
    description: str = ""
    source: Literal["manual", "complaint", "event"] = "manual"


class TicketDispatchIn(BaseModel):
    assignee: str = Field(min_length=1, max_length=64)
    note: str = ""


class TicketProcessIn(BaseModel):
    repair_note: str = Field(min_length=1, max_length=512)


# ---------- 审批 ----------

APPROVAL_TYPES = {"refund", "shutdown", "price_change", "batch_reassign"}


class ApprovalIn(BaseModel):
    type: Literal["refund", "shutdown", "price_change", "batch_reassign"]
    target: str = Field(min_length=1, max_length=128, description="影响对象，如社区/充电桩/订单编号")
    community_id: str | None = None
    amount: float = Field(default=0.0, ge=0)
    reason: str = Field(min_length=1, max_length=512, description="业务理由")


class ApprovalDecisionIn(BaseModel):
    decision: Literal["approved", "rejected"]
    comment: str = ""


# ---------- Agent ----------


class AgentChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    session_id: str | None = None
    stream: bool = False
