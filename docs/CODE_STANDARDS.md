# 代码规范 — Python / Vue / SQLite / Agent

## 1. 组织与大小

- **一个模块一个职责**。顶层只放启动配置与总入口；业务文件按 `backend/{api,application,domain,infrastructure}/<domain>/`，前端按 `frontend/src/{pages,components,stores,api,composables}/`。
- 不新增 Tkinter、PySide、业务 Mixin 或跨层万能 `self`；使用依赖注入和明确 service/repository 接口。
- 单 Python 文件建议不超过 400 行；超过 600 行需要拆分理由和重构计划。单函数建议 < 60 行；例外在代码评审中说明。**可读性优先，非机械行数约束**。
- 不建空的“未来可能用到”模块。新文件必须被当前需求入口、测试或启动流程引用；删除未使用导入、路由、旧示例。
- 目录内可以用短 `README.md` 说明非显而易见的模块契约；重复主文档的不允许创建。

## 2. Python 规范

- Python 3.11+；核心用类型标注，公共函数说明输入、输出、异常、单位、时区。禁止业务层广泛使用 `Any`、隐式全局可变缓存。
- `ruff` 统一 lint/format；`mypy` 或 `pyright` 检查关键领域层；测试使用 `pytest`、`hypothesis`。Python 依赖选择一个锁定机制（建议 uv.lock，P0 确认）；前端 npm lockfile，新增依赖必须说明用途、许可证和运行成本。
- `Decimal`/整数分处理资金，量化/四舍五入只在明确的业务边界发生；展示时格式化，不把展示字符串写回数据层。
- 不要 `except Exception: pass` 掩盖账务/行情错误。可恢复错误转化为带代码的领域异常；预期故障日志不重复刷屏。
- SQLAlchemy 2.x 统一业务事务；仅基础设施可用 sqlite3 Backup API。API 层不得调用 `sqlite3.connect()`，domain 不得导入 `fastapi`、`sqlalchemy`、`tkinter`、Vue 相关库。
- 后台网络抓取必须有超时、限频、重试上限、来源标签，不得在持有 SQLite 写事务时远程请求。

## 3. 业务层边界

- `TradeService` 是成交唯一写服务；`FinanceService` 是账本/账户唯一写服务；两者通过明确应用用例共用事务，不各自“写一半再同步”。
- `PortfolioService` 和 `PnLService` 读取事实并生成可重建视图，不直接修改成交和入金。
- `StrategyService` 接受显式的行情快照、规则版本、批次状态，输出可审计的候选/正式/否决及证据；不承担下单。
- 禁止 UI 更新方法直接写 `portfolio`, `trade_history`, `capital_history` 风格二次权威状态；不可通过线程中修改全局对象实现业务事务。
- 函数命名使用动词+名词（如 `record_execution`, `recalculate_pnl`），不能使用含混的 `sync_all`, `handle_data`, `fix_everything`。

## 4. SQL / 迁移

- 所有表结构变更提供有序 migration；在临时数据库验证升级/失败回滚，并在正式迁移前生成备份。
- 启用 `foreign_keys`, 使用参数化 SQL、外键、唯一约束、CHECK；不拼接用户字符串生成 SQL。
- 写操作从应用用例传入**同一个事务上下文**，各 repository 不自行 `commit()`；失败整体 rollback。
- 不用 `INSERT OR REPLACE` 覆盖事实表；交易修正必须有冲正/审计。不能因为“个人自用”省略幂等。
- 查询新增 JOIN/排序/筛选时评估索引；大量历史数据按游标或分页读取。
- 运行时文件只位于配置确定的 `data/` 绝对路径，不根据 `cwd` 猜测数据库文件。

## 5. HTTP / WebSocket

- 路由前缀 `/api/v1`，使用 Pydantic 请求/响应模型；正确使用 400/401/403/404/409/422/503。
- 写 API 同源、认证、CSRF 验证与幂等键；仅允许私有可信 Host/Origin，不用 `allow_origins=['*']` 偷懒。
- 同一业务读接口返回：数据版本、行情时点、数据质量状态；持仓/收益跨页面不得各自重新算出冲突值。
- WS 只用于消息提醒；断线后 HTTP 重新获取完整状态，不通过 WS 推送构造第二套财务事实。

## 6. Vue / TypeScript

- Vue 3 Composition API + `<script setup lang="ts">`，页面负责组合，计算规则不放在浏览器。
- 页面从 `frontend/src/api/` 统一获取类型化的 DTO；Pinia 只保存 UI/会话/筛选状态和短期服务端结果，不把资金总额当作权威可变状态。
- 页面大组件分解成 `PositionTable`, `LotDetail`, `DataQualityBadge` 等；无巨大 `App.vue` 集中全部业务。
- 金额、正负、时区、账户、交易日口径统一走格式化工具；UI 不能把 `null` 显示为零。
- 所有写按钮要显示提交中/成功/失败、冲突与重试；重复点击仍由后端保证幂等。
- 关键流程有组件测试与 Playwright E2E；断网/身份过期/服务端异常应有可理解的状态。

## 7. 测试质量门槛

- Domain 纯函数：单元测试 + 属性测试（成交顺序、买卖约束、收益重算）。
- Application+SQLite：临时真实 SQLite 集成测试（事务回滚、重复请求、资金/批次守恒）。
- API：授权、权限、Origin/CSRF、缺失行情/409 冲突等契约测试。
- Frontend：页面交互与核心路径端到端测试。
- 修 Bug 必须先写可复现的回归测试；不要通过修改已有测试预期来掩盖错误。
- 每轮执行**相关最小测试集**；在 P1/P2/P4 发布门槛前执行全部核心用例。

## 8. 依赖、文档与安全

- 依赖优先：Python 标准库/已有依赖 > 小而成熟、许可证明确的库 > 自研基础设施。
- 不引入与现有 Python+Vue 技术栈冲突的大型整套财务系统。许可证和升级策略记录在 PR 或 ADR。
- 每个业务接口只有**一处权威文档**：产品行为 `PRODUCT_SPEC`、数据口径 `DATA_CONTRACTS`、系统边界 `ARCHITECTURE`、编码规则本文件；不要把规范全文复制进 `AGENTS.md`。
- 日志、异常、测试快照与代码审查包必须脱敏；禁止提交 `.env`、数据库、token、备份。
- 不允许因重构顺手复活已下线功能；用户未明确要求的历史 UI/研究脚本不迁移。

## 9. 提交与审查

- 一次变更聚焦一个可验证业务用例；推荐提交信息 `feat(trading): ...`、`fix(pnl): ...`、`test(finance): ...`、`docs(agent): ...`。
- 每轮必须增量打包；详情见 `AGENT_WORKFLOW.md`。提交说明至少包含：做了什么、影响哪些数据表、通过何种测试、尚未解决什么。
