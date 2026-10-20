"""Agent 工作流（规则引擎实现）。

节点与架构文档保持一致：
parse_intent → load_context → check_permission → plan_action → risk_gate
  ├─ 只读：execute_action（查询）→ verify_result → 生成回答
  ├─ 普通写：幂等执行 → 回执校验 → 审计
  └─ 高风险：request_approval（创建审批，不直接执行）
最后 write_memory → reflect。

生产环境可将本模块替换为 LangGraph 实现（节点名不变），
模型只能调用已登记的白名单工具，参数经 Pydantic 校验，
模型输出绝不直接作为 SQL 或命令执行。
"""

import re
import uuid
from datetime import datetime, timezone

from .. import db
from ..config import settings

NODES = [
    "parse_intent", "load_context", "check_permission", "plan_action",
    "risk_gate", "request_approval", "execute_action", "verify_result",
    "write_memory", "reflect",
]

HIGH_RISK_INTENTS = {"refund", "shutdown", "price_change", "batch_reassign"}

INTENT_KEYWORDS = [
    ("refund", ["退款"]),
    ("shutdown", ["停机", "停运", "关停"]),
    ("price_change", ["调价", "改价", "调整价格"]),
    ("batch_reassign", ["改派", "批量改派", "重新分派"]),
    ("fault_analysis", ["故障", "归因", "异常", "为什么", "原因"]),
    ("repair_suggestion", ["维修", "修复", "怎么修", "维修方案", "处理方案"]),
    ("order_analysis", ["订单分析", "收入", "营收", "订单量"]),
    ("metrics_query", ["可用率", "利用率", "故障率", "指标", "报表", "对比", "运营情况", "修复时长"]),
    ("reservation_query", ["预约"]),
    ("device_query", ["设备", "充电桩", "枪", "离线", "在线", "状态"]),
]

INTENT_NAMES = {
    "refund": "退款申请", "shutdown": "停机申请", "price_change": "调价申请",
    "batch_reassign": "批量改派申请", "fault_analysis": "故障归因",
    "repair_suggestion": "维修方案", "order_analysis": "订单分析",
    "metrics_query": "运营指标查询", "reservation_query": "预约查询",
    "device_query": "设备查询", "help": "使用帮助",
}

# 工具白名单：每个工具声明名称、风险级别、允许角色
TOOLS = {
    "query_dashboard": {"risk": "read", "roles": {"admin", "operator", "finance", "maintenance"}},
    "query_devices": {"risk": "read", "roles": {"admin", "operator", "maintenance", "resident", "finance"}},
    "query_orders": {"risk": "read", "roles": {"admin", "operator", "finance", "resident"}},
    "query_tickets": {"risk": "read", "roles": {"admin", "operator", "maintenance"}},
    "analyze_fault": {"risk": "read", "roles": {"admin", "operator", "maintenance"}},
    "create_ticket": {"risk": "write", "roles": {"admin", "operator", "resident"}},
    "create_approval": {"risk": "high", "roles": {"admin", "operator", "finance", "resident"}},
}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_intent(message: str) -> tuple[str, dict]:
    """意图识别：关键词规则 + 实体抽取。"""
    params: dict = {}
    m = re.search(r"(CP-[A-Z0-9]+)", message, re.IGNORECASE)
    if m:
        params["pile_name"] = m.group(1).upper()
    m = re.search(r"(O-\d+)", message, re.IGNORECASE)
    if m:
        params["order_id"] = m.group(1).upper()
    m = re.search(r"(T-\d+)", message, re.IGNORECASE)
    if m:
        params["ticket_id"] = m.group(1).upper()
    for cname in ("阳光花园", "滨江苑", "翠湖湾"):
        if cname in message:
            params["community_name"] = cname

    for intent, words in INTENT_KEYWORDS:
        if any(w in message for w in words):
            return intent, params
    return "help", params


def _community_id_by_name(name: str | None) -> str | None:
    for c in db.store.communities:
        if c["name"] == name:
            return c["id"]
    return None


def _visible_communities(user: dict) -> set[str]:
    if user["role"] in {"admin", "finance"}:
        return {c["id"] for c in db.store.communities}
    return set(user.get("communities") or [])


def _fmt_pile(p: dict, guns: list[dict]) -> str:
    c = next((c for c in db.store.communities if c["id"] == p["community_id"]), {})
    gun_txt = "、".join(f"{g['connector_no']}号枪[{g['status']}]" for g in guns)
    return f"{p['name']}（{c.get('name','')}，{p['status']}，{p['power_kw']}kW）：{gun_txt}"


class AgentRun:
    """一次 Agent 运行，逐步产出节点事件（用于 SSE 流式输出）。"""

    def __init__(self, user: dict, message: str, session_id: str | None):
        self.user = user
        self.message = message
        self.session_id = session_id or uuid.uuid4().hex
        self.trace_id = uuid.uuid4().hex
        self.events: list[dict] = []
        self.tool_calls: list[dict] = []
        self.data_sources: list[str] = []
        self.nodes_done: list[dict] = []
        self.intent = "help"
        self.params: dict = {}
        self.risk = "read"
        self.status = "success"
        self.answer = ""
        self.approval_id: str | None = None
        self.denied_reason: str | None = None

    # ---------- 节点 ----------

    def _emit(self, node: str, summary: str, **extra) -> dict:
        event = {"node": node, "summary": summary, "ts": _now(), **extra}
        self.nodes_done.append(event)
        return event

    def run(self):
        """生成器：依次产出节点事件，最后产出 final 结果。"""
        yield self._emit("parse_intent", "解析用户意图")
        self.intent, self.params = parse_intent(self.message)
        yield self._emit("parse_intent", f"意图：{INTENT_NAMES[self.intent]}",
                         intent=self.intent, params=self.params, done=True)

        yield self._emit("load_context", "加载会话上下文与数据范围")
        self._load_context()
        yield self._emit("load_context",
                         f"数据范围：{len(self.scope)} 个社区，会话 {self.session_id[:8]}",
                         done=True)

        yield self._emit("check_permission", "校验角色与数据范围")
        allowed, reason = self._check_permission()
        if not allowed:
            self.status = "denied"
            self.denied_reason = reason
            self.answer = f"权限不足：{reason}"
            db.store.audit(self.user["username"], self.user["role"], "agent.denied",
                           self.intent, reason, self.trace_id, result="denied")
            yield self._emit("check_permission", f"拒绝：{reason}", done=True)
            yield self._emit("reflect", "已记录越权尝试", done=True)
            yield {"final": self.result()}
            return
        yield self._emit("check_permission", "校验通过", done=True)

        yield self._emit("plan_action", "规划工具调用")
        plan = self._plan()
        yield self._emit("plan_action", f"计划工具：{'、'.join(plan)}", done=True)

        self.risk = "high" if self.intent in HIGH_RISK_INTENTS else (
            "write" if "create_ticket" in plan else "read")
        yield self._emit("risk_gate", "风险分级")
        yield self._emit("risk_gate",
                         f"风险级别：{'高风险（需审批）' if self.risk == 'high' else ('普通写' if self.risk == 'write' else '只读')}",
                         done=True)

        if self.risk == "high":
            yield self._emit("request_approval", "高风险动作只创建审批申请，不直接执行")
            self._request_approval()
            yield self._emit("request_approval",
                             f"已创建审批 {self.approval_id}，等待授权人员决策", done=True)
        else:
            yield self._emit("execute_action", "执行工具调用")
            self._execute(plan)
            yield self._emit("execute_action", f"完成 {len(self.tool_calls)} 次工具调用", done=True)
            yield self._emit("verify_result", "校验结果完整性")
            ok = self._verify()
            yield self._emit("verify_result", "结果校验通过" if ok else "结果校验失败", done=True)
            if not ok:
                self.status = "failed"

        yield self._emit("write_memory", "写入会话记忆（脱敏）")
        self._write_memory()
        yield self._emit("write_memory", "短期记忆已更新", done=True)

        yield self._emit("reflect", "复盘本次运行")
        db.store.agent_runs.append({
            "trace_id": self.trace_id, "user": self.user["username"],
            "intent": self.intent, "risk_level": self.risk, "status": self.status,
            "tools": [t["tool"] for t in self.tool_calls], "ts": _now(),
        })
        yield self._emit("reflect", f"运行记录已保存（trace_id={self.trace_id[:12]}…）", done=True)

        yield {"final": self.result()}

    # ---------- 各节点实现 ----------

    def _load_context(self) -> None:
        self.scope = _visible_communities(self.user)
        hist = db.store.memory_short.setdefault(self.session_id, [])
        hist.append({"role": "user", "content": self.message[:500], "ts": _now()})
        # TTL 清理：仅保留最近 20 条
        db.store.memory_short[self.session_id] = hist[-20:]

    def _check_permission(self) -> tuple[bool, str]:
        role = self.user["role"]
        if self.intent in {"shutdown", "price_change", "batch_reassign"} and role in {"resident", "maintenance"}:
            return False, f"角色 {role} 不允许发起 {INTENT_NAMES[self.intent]}"
        if self.intent == "refund" and role == "maintenance":
            return False, "维修人员不允许发起退款"
        if self.intent == "order_analysis" and role == "resident":
            return False, "居民用户仅能查询本人订单"
        return True, ""

    def _plan(self) -> list[str]:
        if self.intent in HIGH_RISK_INTENTS:
            return ["create_approval"]
        return {
            "fault_analysis": ["query_devices", "analyze_fault"],
            "repair_suggestion": ["query_devices", "analyze_fault"],
            "order_analysis": ["query_orders", "query_dashboard"],
            "metrics_query": ["query_dashboard"],
            "reservation_query": ["query_orders"],
            "device_query": ["query_devices"],
            "help": [],
        }.get(self.intent, ["query_dashboard"])

    def _request_approval(self) -> None:
        p = self.params
        community_id = _community_id_by_name(p.get("community_name"))
        target = p.get("order_id") or p.get("pile_name") or p.get("ticket_id") or "待补充"
        amount = 0.0
        if self.intent == "refund" and p.get("order_id"):
            order = next((o for o in db.store.orders if o["id"] == p["order_id"]), None)
            if order:
                amount = order["amount"]
                community_id = order["community_id"]
                if self.user["role"] == "resident" and order["user_id"] != self.user["sub"]:
                    self.status = "denied"
                    self.answer = "只能为本人订单申请退款。"
                    return
        reason_map = {
            "refund": "Agent 会话中用户申请退款",
            "shutdown": "Agent 会话中申请设备停机",
            "price_change": "Agent 会话中申请计费调价",
            "batch_reassign": "Agent 会话中申请工单批量改派",
        }
        risk_map = {
            "refund": f"涉及金额 {amount} 元，需财务复核",
            "shutdown": "停机将影响用户充电，需评估影响范围",
            "price_change": "调价影响计费，需财务与运营双确认",
            "batch_reassign": "批量改派影响维修时效，需运营负责人确认",
        }
        approval = {
            "id": db.store.next_id("A"), "type": self.intent, "target": target,
            "community_id": community_id, "amount": amount,
            "reason": f"{reason_map[self.intent]}。原始诉求：{self.message[:120]}",
            "applicant": self.user["username"], "risk_assessment": risk_map[self.intent],
            "status": "pending", "approver": None, "decided_at": None,
            "executed_at": None, "receipt": None, "created_at": _now(),
        }
        with db.store.lock:
            db.store.approvals.append(approval)
        self.approval_id = approval["id"]
        self.status = "approval_created"
        self.tool_calls.append({"tool": "create_approval", "risk": "high",
                                "status": "success", "summary": f"创建审批 {approval['id']}"})
        self.data_sources.append("approvals")
        db.store.audit(self.user["username"], self.user["role"], "agent.approval_created",
                       approval["id"], f"type={self.intent} target={target}", self.trace_id)
        self.answer = (
            f"该操作属于高风险动作，我已创建审批申请 {approval['id']}"
            f"（类型：{INTENT_NAMES[self.intent]}，对象：{target}"
            + (f"，金额：{amount} 元" if amount else "")
            + "）。该操作不会直接执行，需财务/审批人员决策后才会生效。"
        )

    def _execute(self, plan: list[str]) -> None:
        p = self.params
        for tool in plan:
            spec = TOOLS[tool]
            if self.user["role"] not in spec["roles"]:
                self.tool_calls.append({"tool": tool, "risk": spec["risk"],
                                        "status": "denied", "summary": "角色无权限"})
                continue
            handler = getattr(self, f"_tool_{tool}")
            summary = handler()
            self.tool_calls.append({"tool": tool, "risk": spec["risk"],
                                    "status": "success", "summary": summary})

    def _tool_query_dashboard(self) -> str:
        community_id = _community_id_by_name(self.params.get("community_name"))
        if community_id and community_id not in self.scope:
            return "超出授权社区范围，已跳过"
        m = db.compute_metrics(community_id)
        self.metrics = m
        self.data_sources.append("dashboard_metrics")
        return "已汇总运营指标"

    def _tool_query_devices(self) -> str:
        piles = [p for p in db.store.piles if p["community_id"] in self.scope]
        if self.params.get("pile_name"):
            piles = [p for p in piles if p["name"] == self.params["pile_name"]]
        if self.params.get("community_name"):
            cid = _community_id_by_name(self.params["community_name"])
            piles = [p for p in piles if p["community_id"] == cid]
        self.piles = piles
        self.data_sources.append("piles")
        self.data_sources.append("device_events")
        return f"命中 {len(piles)} 台充电桩"

    def _tool_query_orders(self) -> str:
        if self.user["role"] == "resident":
            orders = [o for o in db.store.orders if o["user_id"] == self.user["sub"]]
        else:
            orders = [o for o in db.store.orders if o["community_id"] in self.scope]
        if self.params.get("order_id"):
            orders = [o for o in orders if o["id"] == self.params["order_id"]]
        self.orders = orders
        self.data_sources.append("orders")
        return f"命中 {len(orders)} 条订单"

    def _tool_analyze_fault(self) -> str:
        self.fault_notes = []
        for p in getattr(self, "piles", []):
            events = [e for e in db.store.device_events
                      if e["pile_id"] == p["id"] and e["severity"] in {"warn", "critical"}]
            tickets = [t for t in db.store.tickets if t["pile_id"] == p["id"]]
            if events or p["status"] in {"fault", "offline"}:
                self.fault_notes.append((p, events, tickets))
        self.data_sources.append("tickets")
        return f"归因分析 {len(getattr(self, 'fault_notes', []))} 台设备"

    def _verify(self) -> bool:
        if self.intent == "device_query" and not hasattr(self, "piles"):
            return False
        return True

    def _write_memory(self) -> None:
        hist = db.store.memory_short.setdefault(self.session_id, [])
        hist.append({"role": "assistant", "content": self.answer[:500], "ts": _now()})
        # 长期记忆只保存脱敏后的稳定事实
        if self.intent in HIGH_RISK_INTENTS and self.approval_id:
            db.store.memory_long.append({
                "user_id": self.user["sub"], "trace_id": self.trace_id,
                "fact": f"用户关注{INTENT_NAMES[self.intent]}（审批 {self.approval_id}）",
                "ts": _now(),
            })

    # ---------- 回答生成 ----------

    def result(self) -> dict:
        if self.status == "success" and not self.answer:
            self.answer = self._compose_answer()
        return {
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "intent": self.intent,
            "intent_name": INTENT_NAMES[self.intent],
            "risk_level": self.risk,
            "status": self.status,
            "answer": self.answer,
            "data_sources": sorted(set(self.data_sources)),
            "tool_calls": self.tool_calls,
            "approval_id": self.approval_id,
            "nodes": self.nodes_done,
            "model": settings.LLM_MODEL,
        }

    def _compose_answer(self) -> str:
        i = self.intent
        if i == "metrics_query":
            m = getattr(self, "metrics", db.compute_metrics(None))
            lines = [
                f"设备可用率 {pct(m['device_availability'])}，利用率 {pct(m['utilization'])}，"
                f"故障率 {pct(m['fault_rate'])}",
                f"平均修复时长 {m['mttr_hours'] or '—'} 小时，预约成功率 {pct(m['reservation_success_rate'])}",
                f"订单收入 {m['revenue']} 元，退款金额 {m['refund_amount']} 元，未关闭工单 {m['open_tickets']} 个",
            ]
            if m["per_community"]:
                lines.append("社区对比：" + "；".join(
                    f"{c['community_name']} 可用率 {pct(c['availability'])}/收入 {c['revenue']} 元/待办工单 {c['open_tickets']}"
                    for c in m["per_community"]))
            return "。".join(lines[:3]) + "\n" + lines[3] if len(lines) > 3 else "。".join(lines)
        if i == "device_query":
            piles = getattr(self, "piles", [])
            if not piles:
                return "未找到授权范围内的设备。"
            lines = [_fmt_pile(p, [g for g in db.store.guns if g["pile_id"] == p["id"]])
                     for p in piles[:8]]
            return f"共 {len(piles)} 台设备：\n" + "\n".join(lines)
        if i in {"fault_analysis", "repair_suggestion"}:
            notes = getattr(self, "fault_notes", [])
            if not notes:
                return "授权范围内未发现异常设备。"
            out = []
            for p, events, tickets in notes[:5]:
                ev_txt = "、".join(f"{e['type']}({e['severity']})" for e in events[-3:]) or "无告警事件"
                tk = tickets[-1] if tickets else None
                tk_txt = f"；最新工单 {tk['id']}[{tk['status']}]" if tk else "；暂无工单"
                advice = _repair_advice(p, events)
                out.append(f"{p['name']}：状态 {p['status']}，近期事件：{ev_txt}{tk_txt}。{advice}")
            header = "故障归因与维修建议：\n" if i == "fault_analysis" else "维修方案：\n"
            return header + "\n".join(out)
        if i == "order_analysis":
            orders = getattr(self, "orders", [])
            revenue = round(sum(o["amount"] for o in orders if o["status"] == "completed"), 2)
            abnormal = [o for o in orders if o["status"] == "abnormal"]
            by_status: dict[str, int] = {}
            for o in orders:
                by_status[o["status"]] = by_status.get(o["status"], 0) + 1
            status_txt = "、".join(f"{k} {v} 单" for k, v in sorted(by_status.items()))
            extra = f"；异常订单：{'、'.join(o['id'] for o in abnormal)}" if abnormal else ""
            return f"共 {len(orders)} 条订单（{status_txt}），已完成收入 {revenue} 元{extra}。"
        if i == "reservation_query":
            if self.user["role"] == "resident":
                res = [r for r in db.store.reservations if r["user_id"] == self.user["sub"]]
            else:
                res = [r for r in db.store.reservations if r["community_id"] in self.scope]
            active = [r for r in res if r["status"] == "active"]
            return f"共 {len(res)} 条预约，其中生效中 {len(active)} 条。" + (
                "生效预约：" + "、".join(f"{r['id']}({r['start_time'][:16]}~{r['end_time'][11:16]})"
                                         for r in active[:5]) if active else "")
        return ("我是邻里充电运营 Agent，可以帮你：查询运营指标、设备状态、订单与预约；"
                "分析故障归因、给出维修方案；发起退款/停机/调价等审批申请（高风险动作只会创建审批，不会直接执行）。")


def pct(v) -> str:
    return "—" if v is None else f"{round(v * 100, 1)}%"


def _repair_advice(pile: dict, events: list[dict]) -> str:
    types = {e["type"] for e in events}
    if "temperature" in types:
        return "建议：断电冷却后检查温控传感器与散热风道，更换异常传感器，复测满载温升。"
    if "offline" in types:
        return "建议：检查网络模块与供电空开，远程重启通信单元，仍离线则现场排查。"
    if "charge_interrupted" in types:
        return "建议：检查枪线接触与绝缘检测记录，回放中断前后电流曲线。"
    if pile["status"] == "fault":
        return "建议：读取故障码后按维修手册处置，关闭工单前完成心跳与测试订单验收。"
    return "建议：持续观察，必要时创建工单。"
