-- =====================================================================
-- 邻里充电运营平台 数据库初始化脚本
-- 双库架构（与《02_系统架构文档》一致）：
--   1) MySQL      —— 身份中心：租户、用户、角色、数据范围、刷新令牌
--   2) PostgreSQL —— 业务库：设备、事件、预约、订单、工单、审批、审计、Agent
-- 演示模式（DEMO_MODE=true）不依赖数据库；本脚本用于生产环境初始化。
-- 所有表均带租户隔离字段或社区范围字段，写操作需应用侧在事务内完成幂等判断。
-- =====================================================================

-- =====================================================================
-- 第一部分：MySQL 身份中心（数据库：charging_identity）
-- =====================================================================

CREATE DATABASE IF NOT EXISTS charging_identity
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE charging_identity;

-- 租户（多租户隔离的根）
CREATE TABLE IF NOT EXISTS tenants (
  id          VARCHAR(32)  PRIMARY KEY COMMENT '租户ID',
  name        VARCHAR(128) NOT NULL COMMENT '租户名称',
  status      VARCHAR(16)  NOT NULL DEFAULT 'active' COMMENT 'active/disabled',
  created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
) COMMENT='租户';

-- 用户（业务服务只保存 PBKDF2 哈希，不存明文密码）
CREATE TABLE IF NOT EXISTS users (
  id             VARCHAR(32)  PRIMARY KEY COMMENT '用户ID',
  tenant_id      VARCHAR(32)  NOT NULL COMMENT '租户ID',
  username       VARCHAR(64)  NOT NULL COMMENT '登录名',
  password_hash  VARCHAR(255) NOT NULL COMMENT 'pbkdf2$salt$dk',
  name           VARCHAR(64)  NOT NULL COMMENT '姓名',
  role           VARCHAR(16)  NOT NULL COMMENT 'admin/operator/maintenance/finance/resident',
  status         VARCHAR(16)  NOT NULL DEFAULT 'active' COMMENT 'active/disabled',
  created_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_tenant_username (tenant_id, username),
  KEY idx_role (role),
  CONSTRAINT fk_users_tenant FOREIGN KEY (tenant_id) REFERENCES tenants (id)
) COMMENT='用户与角色';

-- 用户-社区数据范围（RBAC 之外的行级数据授权）
CREATE TABLE IF NOT EXISTS user_community_scopes (
  user_id      VARCHAR(32) NOT NULL,
  community_id VARCHAR(32) NOT NULL COMMENT '授权社区ID',
  PRIMARY KEY (user_id, community_id),
  CONSTRAINT fk_scope_user FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
) COMMENT='用户社区数据范围';

-- 刷新令牌（支持轮换与撤销；access 令牌短 TTL 不落库）
CREATE TABLE IF NOT EXISTS refresh_tokens (
  jti        VARCHAR(64) PRIMARY KEY COMMENT 'JWT ID',
  user_id    VARCHAR(32) NOT NULL,
  expires_at DATETIME    NOT NULL,
  revoked    TINYINT(1)  NOT NULL DEFAULT 0,
  created_at DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_user (user_id),
  CONSTRAINT fk_rt_user FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
) COMMENT='刷新令牌';

-- 登录失败限流记录（连续失败锁定）
CREATE TABLE IF NOT EXISTS login_failures (
  username     VARCHAR(64) PRIMARY KEY,
  fail_count   INT      NOT NULL DEFAULT 0,
  locked_until DATETIME NULL COMMENT '锁定截止时间',
  updated_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) COMMENT='登录失败限流';

-- =====================================================================
-- 第二部分：PostgreSQL 业务库（数据库：charging_business）
-- =====================================================================
-- 以下语句在 PostgreSQL 中执行：
--   CREATE DATABASE charging_business WITH ENCODING 'UTF8';

-- 社区
CREATE TABLE IF NOT EXISTS communities (
  id         VARCHAR(32)  PRIMARY KEY,
  tenant_id  VARCHAR(32)  NOT NULL,
  name       VARCHAR(128) NOT NULL,
  address    VARCHAR(255) NOT NULL DEFAULT '',
  status     VARCHAR(16)  NOT NULL DEFAULT 'active'
);
COMMENT ON TABLE communities IS '社区（数据范围的基本单位）';

-- 充电区域（社区下的物理分区）
CREATE TABLE IF NOT EXISTS zones (
  id           VARCHAR(32) PRIMARY KEY,
  community_id VARCHAR(32) NOT NULL REFERENCES communities (id),
  name         VARCHAR(128) NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_zones_community ON zones (community_id);

-- 充电桩
CREATE TABLE IF NOT EXISTS piles (
  id           VARCHAR(32)   PRIMARY KEY,
  tenant_id    VARCHAR(32)   NOT NULL,
  community_id VARCHAR(32)   NOT NULL REFERENCES communities (id),
  zone_id      VARCHAR(32)   REFERENCES zones (id),
  name         VARCHAR(64)   NOT NULL,              -- 设备编号，如 CP-A01
  status       VARCHAR(16)   NOT NULL DEFAULT 'online',  -- online/fault/offline/maintenance
  power_kw     NUMERIC(6,2)  NOT NULL DEFAULT 7.0,
  price_kwh    NUMERIC(8,4)  NOT NULL DEFAULT 1.2,  -- 电费 元/kWh
  price_hour   NUMERIC(8,4)  NOT NULL DEFAULT 0.5,  -- 时段占用费 元/h
  created_at   TIMESTAMPTZ   NOT NULL DEFAULT now(),
  UNIQUE (community_id, name)
);
CREATE INDEX IF NOT EXISTS idx_piles_community ON piles (community_id);
CREATE INDEX IF NOT EXISTS idx_piles_status ON piles (status);

-- 枪口（一台桩多个枪口，预约/订单挂在枪口上）
CREATE TABLE IF NOT EXISTS guns (
  id           VARCHAR(32) PRIMARY KEY,
  pile_id      VARCHAR(32) NOT NULL REFERENCES piles (id) ON DELETE CASCADE,
  connector_no SMALLINT    NOT NULL,
  status       VARCHAR(16) NOT NULL DEFAULT 'available',  -- available/charging/fault/offline
  UNIQUE (pile_id, connector_no)
);
CREATE INDEX IF NOT EXISTS idx_guns_pile ON guns (pile_id);

-- 设备事件（事件编号全局唯一，幂等去重的依据）
CREATE TABLE IF NOT EXISTS device_events (
  id         BIGSERIAL    PRIMARY KEY,
  event_no   VARCHAR(64)  NOT NULL UNIQUE,          -- 设备侧事件编号，重复上报直接忽略
  pile_id    VARCHAR(32)  NOT NULL REFERENCES piles (id),
  type       VARCHAR(32)  NOT NULL,                 -- heartbeat/temperature/offline/fault/...
  severity   VARCHAR(16)  NOT NULL DEFAULT 'info',  -- info/warn/critical
  payload    JSONB        NOT NULL DEFAULT '{}',
  ts         TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_events_pile_ts ON device_events (pile_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_type ON device_events (type);

-- 预约（同一枪口时间区间互斥，由排除约束防止重叠）
CREATE TABLE IF NOT EXISTS reservations (
  id              VARCHAR(32)  PRIMARY KEY,         -- R-xxxx
  user_id         VARCHAR(32)  NOT NULL,
  gun_id          VARCHAR(32)  NOT NULL REFERENCES guns (id),
  pile_id         VARCHAR(32)  NOT NULL REFERENCES piles (id),
  community_id    VARCHAR(32)  NOT NULL REFERENCES communities (id),
  start_time      TIMESTAMPTZ  NOT NULL,
  end_time        TIMESTAMPTZ  NOT NULL,
  status          VARCHAR(16)  NOT NULL DEFAULT 'active',  -- active/completed/cancelled
  note            VARCHAR(255) NOT NULL DEFAULT '',
  created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
  idempotency_key VARCHAR(191) UNIQUE,              -- 租户:用户:类型:键
  CONSTRAINT chk_time_range CHECK (end_time > start_time)
);
-- 防冲突核心：同一枪口的生效预约时间区间不允许重叠
CREATE UNIQUE INDEX IF NOT EXISTS uq_reservation_slot ON reservations (gun_id, start_time)
  WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_reservations_user ON reservations (user_id);
CREATE INDEX IF NOT EXISTS idx_reservations_community ON reservations (community_id);

-- 订单（状态机：pending_payment/reserved/charging/completed/cancelled/abnormal）
CREATE TABLE IF NOT EXISTS orders (
  id              VARCHAR(32)  PRIMARY KEY,         -- O-xxxx
  user_id         VARCHAR(32)  NOT NULL,
  gun_id          VARCHAR(32)  NOT NULL REFERENCES guns (id),
  pile_id         VARCHAR(32)  NOT NULL REFERENCES piles (id),
  community_id    VARCHAR(32)  NOT NULL REFERENCES communities (id),
  reservation_id  VARCHAR(32)  REFERENCES reservations (id),
  status          VARCHAR(20)  NOT NULL DEFAULT 'pending_payment',
  amount          NUMERIC(10,2) NOT NULL DEFAULT 0,
  kwh             NUMERIC(10,3) NOT NULL DEFAULT 0,
  test_order      BOOLEAN      NOT NULL DEFAULT FALSE,  -- 工单验收测试订单
  created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
  idempotency_key VARCHAR(191) NOT NULL UNIQUE      -- 创建订单必须携带幂等键
);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders (user_id);
CREATE INDEX IF NOT EXISTS idx_orders_community_status ON orders (community_id, status);

-- 订单状态变更历史（留痕）
CREATE TABLE IF NOT EXISTS order_history (
  id        BIGSERIAL   PRIMARY KEY,
  order_id  VARCHAR(32) NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
  status    VARCHAR(20) NOT NULL,
  operator  VARCHAR(64) NOT NULL,
  ts        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_order_history_order ON order_history (order_id);

-- 工单（流程：pending_dispatch→dispatched→accepted→processing→pending_acceptance→closed）
CREATE TABLE IF NOT EXISTS tickets (
  id                   VARCHAR(32)  PRIMARY KEY,    -- T-xxxx
  priority             VARCHAR(4)   NOT NULL DEFAULT 'P3',  -- P1/P2/P3
  pile_id              VARCHAR(32)  NOT NULL REFERENCES piles (id),
  community_id         VARCHAR(32)  NOT NULL REFERENCES communities (id),
  title                VARCHAR(128) NOT NULL,
  description          TEXT         NOT NULL DEFAULT '',
  source               VARCHAR(16)  NOT NULL DEFAULT 'manual',  -- manual/complaint/event
  source_event_no      VARCHAR(64),                 -- 自动建单时关联的事件编号（去重依据）
  status               VARCHAR(20)  NOT NULL DEFAULT 'pending_dispatch',
  assignee             VARCHAR(64),                 -- 维修人员用户名
  dispatch_note        VARCHAR(255) NOT NULL DEFAULT '',
  repair_note          VARCHAR(512) NOT NULL DEFAULT '',
  creator              VARCHAR(32),
  created_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),
  updated_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),
  closed_at            TIMESTAMPTZ,
  heartbeat_checked_at TIMESTAMPTZ,                 -- 验收门禁①：心跳检查时间
  test_order_id        VARCHAR(32)                  -- 验收门禁②：测试订单
);
CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets (status);
CREATE INDEX IF NOT EXISTS idx_tickets_community ON tickets (community_id);
CREATE INDEX IF NOT EXISTS idx_tickets_assignee ON tickets (assignee);
-- 同一事件不重复建单
CREATE UNIQUE INDEX IF NOT EXISTS uq_ticket_source_event ON tickets (source_event_no)
  WHERE source_event_no IS NOT NULL;

-- 工单流转历史
CREATE TABLE IF NOT EXISTS ticket_history (
  id        BIGSERIAL   PRIMARY KEY,
  ticket_id VARCHAR(32) NOT NULL REFERENCES tickets (id) ON DELETE CASCADE,
  status    VARCHAR(20) NOT NULL,
  operator  VARCHAR(64) NOT NULL,
  ts        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ticket_history_ticket ON ticket_history (ticket_id);

-- 审批（高风险动作：退款/停机/调价/批量改派；仅 pending 可决策，仅 approved 可执行）
CREATE TABLE IF NOT EXISTS approvals (
  id               VARCHAR(32)   PRIMARY KEY,       -- A-xxxx
  type             VARCHAR(20)   NOT NULL,          -- refund/shutdown/price_change/batch_reassign
  target           VARCHAR(128)  NOT NULL,          -- 影响对象（订单号/桩编号等）
  community_id     VARCHAR(32)   REFERENCES communities (id),
  amount           NUMERIC(10,2) NOT NULL DEFAULT 0,
  reason           VARCHAR(512)  NOT NULL,
  applicant        VARCHAR(64)   NOT NULL,
  risk_assessment  VARCHAR(512)  NOT NULL DEFAULT '',
  status           VARCHAR(16)   NOT NULL DEFAULT 'pending',  -- pending/approved/rejected/executing/executed
  approver         VARCHAR(64),
  decision_comment VARCHAR(255)  NOT NULL DEFAULT '',
  decided_at       TIMESTAMPTZ,
  executed_at      TIMESTAMPTZ,
  receipt          VARCHAR(512),                    -- 执行回执
  created_at       TIMESTAMPTZ   NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals (status);
-- 相同对象的待审批申请禁止重复提交
CREATE UNIQUE INDEX IF NOT EXISTS uq_approval_pending ON approvals (type, target)
  WHERE status = 'pending';

-- 审计日志（所有写操作留痕，含 trace_id 便于全链路追踪）
CREATE TABLE IF NOT EXISTS audits (
  id        BIGSERIAL   PRIMARY KEY,
  ts        TIMESTAMPTZ NOT NULL DEFAULT now(),
  username  VARCHAR(64) NOT NULL,
  role      VARCHAR(16) NOT NULL,
  action    VARCHAR(64) NOT NULL,
  object    VARCHAR(128) NOT NULL DEFAULT '',
  detail    VARCHAR(512) NOT NULL DEFAULT '',
  trace_id  VARCHAR(64) NOT NULL DEFAULT '',
  result    VARCHAR(16) NOT NULL DEFAULT 'ok'       -- ok/fail/denied/conflict
);
CREATE INDEX IF NOT EXISTS idx_audits_ts ON audits (ts DESC);
CREATE INDEX IF NOT EXISTS idx_audits_trace ON audits (trace_id);

-- Agent 运行记录（复盘与越权分析）
CREATE TABLE IF NOT EXISTS agent_runs (
  trace_id   VARCHAR(64) PRIMARY KEY,
  username   VARCHAR(64) NOT NULL,
  intent     VARCHAR(32) NOT NULL,
  risk_level VARCHAR(8)  NOT NULL,                  -- read/write/high
  status     VARCHAR(20) NOT NULL,                  -- success/denied/failed/approval_created
  tools      JSONB       NOT NULL DEFAULT '[]',
  ts         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_user ON agent_runs (username, ts DESC);

-- Agent 短期会话记忆（TTL 清理，只存脱敏内容）
CREATE TABLE IF NOT EXISTS agent_memory_short (
  id         BIGSERIAL   PRIMARY KEY,
  session_id VARCHAR(64) NOT NULL,
  role       VARCHAR(16) NOT NULL,                  -- user/assistant
  content    VARCHAR(512) NOT NULL,
  ts         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_memory_short_session ON agent_memory_short (session_id, ts);

-- Agent 长期记忆（仅存稳定事实，脱敏后写入）
CREATE TABLE IF NOT EXISTS agent_memory_long (
  id       BIGSERIAL   PRIMARY KEY,
  user_id  VARCHAR(32) NOT NULL,
  trace_id VARCHAR(64) NOT NULL,
  fact     VARCHAR(512) NOT NULL,
  ts       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_memory_long_user ON agent_memory_long (user_id);
