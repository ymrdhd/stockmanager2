# 系统架构与部署规格

## 1. 模块化单体：一个业务后端

```text
公司电脑 / 本地模式
Vue 3 SPA (browser)
    │ HTTPS(Tailscale Serve) / HTTP(loopback)
FastAPI: auth, validation, REST, WS
    ├── application/trading/   → 统一提交真实成交、修正、批次
    ├── application/portfolio/ → 持仓、估值、风险、收益投影
    ├── application/strategy/  → V9/G2 正式裁决和证据
    ├── application/finance/   → 账本、余额、转账、净资产
    ├── application/market/    → 行情、Fed 与数据质量
    └── infrastructure/       → SQLite、调度、备份、网络
             │
     仅 1 个正式 SQLite 业务库（WAL）
     行情临时缓存/日志独立，且永不当成成交事实
```

依赖方向：`API → Application → Domain`；Infrastructure 实现内层定义的接口，由组合根注入（不是 Domain 导入 Infrastructure）；Domain 不依赖 FastAPI、UI、ORM 模型；服务由构造函数显式注入，不使用跨 Mixin 的万能 `self`。

## 2. 进程、并发、生命周期

- 公司只允许**一个正式 FastAPI 服务实例**拥有数据库写入与行情调度权；使用一个 ASGI worker，后台任务不重复启动。
- SQLite WAL + `busy_timeout` + 短事务；连接池策略以 SQLite 线程安全和实际压测为准。写路径必须受数据库约束和事务保护；不能靠“人不会同时点”替代幂等。
- 策略、行情和统计在服务端计算；前端不自行记账或保存一份业务总额。
- 长耗时行情抓取先出事务、再原子提交有效输入；失败回退到带时效标识的最近快照，不能生成虚假正式信号。
- 背景调度与 Web 路由调用同一 application service；失败入 `job_runs`/日志并在设置显示，不静默吞异常。

## 3. 启动模式及数据库身份

| 模式 | 所在设备 | 访问数据 | 写权限 |
|---|---|---|---|
| `server` | 公司电脑 | 绝对路径的唯一正式 SQLite | 可写，实例独占 |
| `client` | 家里/公司浏览器 | HTTPS 到 `server`；**不启动本地后端** | 通过服务端写 |
| `local` | 任一电脑 | 不同目录的快照/沙盒库 | 默认只读；沙盒需显式确认 |

- `system_metadata` 中包含 `database_uuid`、`instance_role`、`generation`、`schema_version`、`created_at`。启动核验配置的预期 UUID/role，文件缺失或不符直接拒绝作为正式服务启动。
- 禁止客户端访问失败后开启本机可写正式账本。公司正式库已不可用但需“接管”时，必须实际停止或隔离旧写入、选定备份、人工确认并提升 generation；不能仅靠 generation 锁定失联旧主机。详见 OPERATIONS_SECURITY §4。
- 配置不允许任意相对路径让不同工作目录创建同名新库；新建正式库是专门的 `init-database --confirm` 运维命令。

## 4. 两台电脑跨网段访问

```text
公司 Windows 常驻机:
  Stock Manager (127.0.0.1:9091)
  Tailscale Serve → tailnet 内的 HTTPS 域名
家里 Windows:
  Tailscale 客户端 + 浏览器 → 同一个 HTTPS 域名
```

- 不开放 SQLite/共享磁盘/SMB 或公网 9091；不需要动态 DNS、路由器映射。
- Tailscale 是私有连接层而**不是应用登录**。应用仍须有会话认证、写操作 CSRF/Origin 防护，受信 Host/Proxy 配置及 WebSocket 鉴权。Cookie `HttpOnly`, `Secure`（HTTPS），`SameSite=Lax/Strict` 按交互测试；绝不把永久 token 放在查询串/URL fragment。
- `TrustedHost` 只许可回环与配置的确切 tailnet 域名；仅配置可信代理路径，不能仅凭 loopback 或转发头认证身份；不全局允许 `*`。
- 公司设备、Tailscale 安装和私人财务数据存储须符合公司 IT 许可；若不允许，保持本地模式，不规避单位安全管控。
- 正式服务停止或断网时家里不能写，缓存只能只读并展示采样时刻。

## 5. API 契约（首版）

以下为**新系统建议接口**，不是原项目已有路由：

| 方法 / 路径 | 作用 | 注意事项 |
|---|---|---|
| `GET /api/v1/system/status` | DB 身份、schema、job/备份状态 | 敏感字段脱敏 |
| `GET /api/v1/dashboard` | 汇总、风险、待办 | 带 `as_of` 与计算质量 |
| `GET /api/v1/portfolio/positions` | 持仓/批次 | 明确账户、行情时点 |
| `GET /api/v1/strategy/signals` | V9/G2 信号 | 区分候选与正式 |
| `GET /api/v1/market/fed` | 利率观测 | 带来源/时间 |
| `GET /api/v1/trades` | 成交历史 | 分页、只读 |
| `POST /api/v1/trades` | 录入已实际成交 | 必须幂等键，事务 |
| `POST /api/v1/trades/{id}/corrections` | 冲正/更正 | 全审计，不原地覆写 |
| `GET /api/v1/finance/overview` | 净资产/现金流 | 资产不重复 |
| `GET /api/v1/finance/transactions` | 账本 | 分页/筛选 |
| `POST /api/v1/finance/transfers` | 账户间转账 | 一事务双边分录 |
| `POST /api/v1/admin/backup` | 触发一致性备份 | 仅授权会话，任务状态可查 |

返回错误采用统一 `code/message/details/request_id`；计算类响应必含 `as_of`, `data_version`, `quality`, `source`。API 用 OpenAPI 自动检查与测试，不允许前端绕开 API 直接连 DB。

## 6. 数据访问与缓存

- 实际成交、资金流水、账户与审计：写在主业务库；关键表禁用级联删除事实记录。
- 日 K/行情可缓存到 `cache/` 或独立行情缓存表，带来源、交易时区、收盘确认、缺失/代理标识；能重新抓取的缓存不是权威财务账本。
- 持仓、日度 P&L、净资产等聚合表（如需要）是**投影**，可重建；写入时记录依赖 `ledger_revision` 与 `quote_revision`。过期即停止作为当前值展示。
- 图表分页加载，避免将整个历史账本一次发给浏览器。

## 7. 备份、恢复、升级

- 先使用 SQLite 官方 Backup API 创建一致性的**单库快照**，校验 `PRAGMA integrity_check`、业务对账与 schema 版本，再通过 Restic（可选）保存加密版本；不直接复制活动中的 `.db` 文件。
- 正式备份采用每日+升级前，保留至少 7 个日版本和 4 个周版本，至少一份隔离在家里或其他允许的独立存储；加密凭据不得随包提交。
- 恢复只进入临时路径 → 自动检验 → 手工停写与切换；失败不覆盖现用正式库。
- 迁移表结构使用Alembic 有序迁移脚本和 `alembic_version` 记录（单一 schema 版本权威）；修改前备份、失败回滚或安全阻断启动。不可自动删除旧库。
- 含财务数据的增量审查 ZIP 与业务备份 ZIP 是两种东西：**代码审查包不含任何正式数据**。

## 8. 运行日志与故障处理

- 单独结构化日志、任务日志、数据质量标志；不记录密码、token、账户全号或完整交易敏感值。
- 关键动作附 `request_id`, `execution_id`, `database_uuid`, `ledger_revision`；对账失败阻断受影响写入/统计，不在后台自动修正来源账。
- 最少监控：服务存活、上次有效行情、上次策略检查、备份与恢复演练日期、对账异常数。
