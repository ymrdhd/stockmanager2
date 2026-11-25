# 文档索引（任务路由，不是必读清单）

**日常只需读**：`AGENTS.md` + `docs/STATUS.md` + 本表命中的相关文档/代码范围。每轮最多先读 1–2 篇专题；仅当修改范围扩大时增加阅读。

| 正在做的任务 | 先读 | 再读/验证 | 预期代码位置 |
|---|---|---|---|
| 从零到交付/阶段任务/发布门槛 | `DELIVERY_PLAN.md` | `STATUS.md` + `AGENT_WORKFLOW.md` | `docs/`, 分阶段业务模块 |
| 初建工程/确定首版功能 | `PRODUCT_SPEC.md` §§1–4 | `ARCHITECTURE.md` §§1–2 | `backend/`, `frontend/` |
| 买卖、成交导入、批次扣减 | `DATA_CONTRACTS.md` §§1–4 | `ACCEPTANCE_TESTS.md` 交易组 | `backend/application/trading/` |
| 当日/历史收益、成本、权益 | `DATA_CONTRACTS.md` §§5–7 | `ACCEPTANCE_TESTS.md` 收益组 | `backend/domain/portfolio/`, `analytics/` |
| 记账、账户、证券资产联动 | `DATA_CONTRACTS.md` §§1、4、6–7 | `PRODUCT_SPEC.md` 财务节 | `backend/application/finance/` |
| V9/G2 信号和持仓决策 | `STRATEGY_CONTRACT.md` | `LEGACY_BASELINE.md`, 回归样例 | `backend/domain/strategy/` |
| 行情、Fed、刷新任务 | `ARCHITECTURE.md` 数据流节 | `ACCEPTANCE_TESTS.md` 数据质量组 | `backend/infrastructure/market/` |
| 页面/交互/API | `INTERFACE_CONTRACT.md` | `PRODUCT_SPEC.md` 对应页面 | `frontend/`, `backend/api/` |
| 双机、认证、启动模式 | `OPERATIONS_SECURITY.md` | `ARCHITECTURE.md` §§3–4 | `backend/main.py`, 部署脚本 |
| 备份、恢复、迁移旧库 | `OPERATIONS_SECURITY.md` §§3–5 | `DATA_CONTRACTS.md` §7 | `tools/`, `migrations/` |
| 架构修改、引入第三方库 | `DECISIONS.md` + `ARCHITECTURE.md` | `CODE_STANDARDS.md` | 相关模块 |
| 开发迭代收尾、生成审查包 | `AGENT_WORKFLOW.md` | `AGENTS.md` 第 4 节 | `tools/package_changes.py` |

约定：`docs/STATUS.md` 只记当前阶段，完成后覆盖旧状态；`docs/DECISIONS.md` 只记不可轻易更改的决策；不要每轮为小修改建立新 MD。

首次只从 STATUS 对应迭代开始。文档权威：产品=PRODUCT_SPEC，数据=DATA_CONTRACTS，策略=STRATEGY_CONTRACT，页面/API=INTERFACE_CONTRACT，架构=ARCHITECTURE，运维安全=OPERATIONS_SECURITY，代码=CODE_STANDARDS，排程=DELIVERY_PLAN，验收=ACCEPTANCE_TESTS，工作流=AGENT_WORKFLOW。冲突按 AGENTS 处理。

09B 交接：`09B_REPORT.md`（实际测试、限制）及 `09B_LOCAL_ACCEPTANCE.md`（Windows/生产构建、迁移审批、演示和双机验收），不得视为发布授权。

09C 交接：`09C_REPORT.md`（实际测试、限制）、`09C_FUNCTION_MATRIX.md`（规格/服务/界面/测试对应及覆盖差距）、`09C_LOCAL_ACCEPTANCE.md`（未来经明确批准的本地构建、日历更新及验收任务）。非发布授权。

09E 交接：`09E_REPORT.md`（三个 S1 的前后复现、实际测试、环境限制及回退），`ISSUES.md` 保留 09D 编号，非关闭裁决或发布授权。


09F 本轮入口：`09F_REPORT.md`、`09F_FUNCTION_MATRIX.md`、`09F_PRODUCT_CANDIDATES.md`、`09F_LOCAL_ACCEPTANCE.md`。

09G 本轮交接：`09G_REPORT.md` 记录内部账本只读对账预览、测试证据、未完成边界与回退方式；`09G_FUNCTION_MATRIX.md` 记录当前 V1.0 网页功能差距。


## 09H 交接入口
- [09H_REPORT.md](09H_REPORT.md)：真实开发、测试、基线恢复与限制。
- [09H_FUNCTION_MATRIX.md](09H_FUNCTION_MATRIX.md)：剩余工作分类。
- [09H_HANDOFF.md](09H_HANDOFF.md)：本地与后续网页验收顺序。
- [CHANGELOG.md](CHANGELOG.md)：本次变更记录。

09I 收尾交接：`09I_REPORT.md`（实际变更及测试）、`09I_FUNCTION_MATRIX.md`（当前剩余分类）、`09I_HANDOFF.md`（最终环境验收顺序与候选口径）。不构成正式发布批准。
09J 交接：`09J_REPORT.md`（接近止损证据边界测试）、`09J_HANDOFF.md`（剩余产品决策及本地验收范围）。
09J 最终交接请以 `09J_REPORT.md` 第7节及 `09J_HANDOFF.md` 末节为准，保留09J-final tag原身份，不以旧tag代替最终交付HEAD。

10 本地验收：`10_LOCAL_ACCEPTANCE_REPORT.md`（实际环境、构建、回归、浏览器与 Windows 运维结果，PASS/FAIL/BLOCKED/NOT RUN 分列）、`10_HANDOFF.md`（剩余授权、阻断与下一步）。当前状态以 `STATUS.md` 为准。
