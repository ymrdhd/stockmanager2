# 09G 最终网页端收尾开发报告

## 09G-A：投资账本只读对账预览

- 开发 BASE：`e976b364158b73ff19cf711c27488c6e44917b35`
- 分支：`09G-web-finalization`
- 目标：依据 `PRODUCT_SPEC.md` §2.3 的“实际成交 → 批次 → 持仓 → 资金 → 收益；展示差额和处理建议，但不自动改账”，增加同一 SQLite 只读快照内的内部账本对账摘要。
- 非目标：不与券商/旧数据库做外部对账；不写入或校正任何事实；不更改交易、策略或收益公式；不替代真实旧库迁移验收。

## 开工基线证据

- 外层交付包 SHA256：`df8d135c643b0151cf595c6a91fe53ee0d87dd8403528098af4a8a2653dd6edd`
- 09F 源码 ZIP SHA256：`a9a581cbabd87fbcf55a58ab87ddbbaa3275a3ec13d482cf9d818358ed015680`
- 09F Git Bundle SHA256：`485859ff08e630f2fb2b17dbeb3cf5f6f464503eaa1e9f78883484d4ecc1c6de`
- Bundle `verify`、`fsck --full`：PASS；HEAD `e976b364158b73ff19cf711c27488c6e44917b35`；153 个 Git 跟踪文件与源码 ZIP 153/153 逐字节一致；无缺失/额外文件；工作树干净。
- 已读取 `AGENTS.md`、`docs/STATUS.md`、`docs/ISSUES.md`、`docs/PRODUCT_SPEC.md`、`docs/INTERFACE_CONTRACT.md`、`docs/INDEX.md`、09F 报告/矩阵/候选口径/本地验收、`AGENT_WORKFLOW.md`、`DELIVERY_PLAN.md`。

## 实际实现

### 后端

新增 `PortfolioService.reconciliation()` 与 `GET /api/v1/portfolio/{account_id}/reconciliation`：

- 在一个 SQLite 只读事务快照中固定账户、估值时点、账本版本、行情版本、数据库 UUID 和 generation。
- 若请求携带版本约束，四项必须齐全；任何版本/身份变化返回冲突，拒绝拼接不同快照。
- 核对有效卖出成交数量与对应批次分配数量；返回卖出批次分配明细（最多100条，明确截断）。
- 对照剩余批次数量与持仓投影。
- 核对证券现金质量、持仓行情估值可用性、自然日收益与权益变动勾稽。
- 列出待核对成交、未解决公司行为及内部勾稽异常；结果分为 `pass`、`attention`、`unavailable`。
- 报告不提供自动修复，不写入任何账本事实，不声称与券商或旧数据库完成外部对账。

### 前端

投资页面新增「生成内部账本对账预览」：

- 必须先有当前账户的估值结果；请求绑定同一 `as_of`、ledger/quote revision、database UUID 和 generation。
- 返回报告身份与快照不一致时拒绝显示。
- 账户切换、估值刷新、行情或账本版本变化时清空旧报告，防止跨账户/跨版本残留。
- 展示内部成交计数、卖出批次分配、剩余批次成本、现金、行情、收益勾稽、异常及原因。
- 现有批次列表增加来源成交 ID / 期初记录 ID / 策略标识，便于人工追溯。
- 所有入口只读；权益现金差额仍须用户根据实际券商余额另行核对后显式提交。

## 实际测试结果

| 命令 | 实际结果 |
|---|---|
| `python -m pytest -q tests/unit/test_09g_reconciliation.py` | 4 PASS |
| 受影响回归：`test_09g_reconciliation.py`、`test_09f_features.py`、`test_pnl.py`、`test_finance.py`、`test_09c_ndx.py`、`test_strategy_service.py`、`test_security.py` | 94 PASS，12.17s |
| `cd frontend && npm test` | 47 PASS，0 FAIL |
| `python -m compileall -q backend tools migrations start.py` | PASS |
| `git -c core.whitespace=cr-at-eol diff --check` | PASS |
| `npm ci --fetch-retries=0 --fetch-timeout=15000` | FAIL：`registry.npmjs.org` DNS `EAI_AGAIN`，npm 退出时另报 `Exit handler never called` |
| `npm run typecheck` | BLOCKED，退出 127：`sh: vue-tsc: Permission denied`；npm ci 未成功，正式检查器不可用 |
| `npm run build` | BLOCKED，退出 127：同样在 `vue-tsc` 前置阶段失败；Vite 生产构建未执行 |
| 真实浏览器/Playwright | NOT RUN；未将 SFC setup 测试冒充浏览器验收 |
| Windows、真实旧库、真实行情、2027官方日历、Tailscale | NOT RUN / 外部资料待提供 |
| Python 全量回归 | NOT RUN；按工作流由独立审查执行 |

测试组有重叠，不把 94 与 47 相加成一个总成绩。`npm ci` 失败后已清理不完整 `node_modules`，未修改锁文件，未删除 `vue-tsc` 或降低构建要求。Python 工具启动时 stderr 另有 artifact_tool spreadsheet warmup 远程异常，但 `pytest`/`compileall` 退出码均为 0；日志中保留原始输出。

## 问题与未完成范围

- 09D-010：已增加系统内部对账预览，但没有真实券商单据、真实旧库或旧批次外部证据；完整外部对账仍 PARTIAL。
- 09D-005：会话/断线禁写已有基础，完整登录过期、所有页面恢复及真实浏览器重连仍待验证。
- 09D-007：证券名称数据源合成测试通过，实网及历史名称迁移未验证。
- 09D-008：退款业务合成测试通过；旧财务库退款类型/映射缺少真实样本，不能猜测迁移。
- 09D-009：基础收益率已有候选计算与测试，正式口径仍待产品确认。
- 09D-011：流动净资产未知资产/负债分类未完整，不能用普通净资产替代。
- 09D-012：接近止损默认 200bps 仍是候选；需产品确认阈值及报价/主子仓验收。
- 09D-013：网页一键备份已有接口及入口；完整 UI、服务端环境和操作验收仍待构建/浏览器/Windows。
- 09D-016/017：逐金额质量解释、退款审计和完整策略审计可读性仍有缺口。
- 09D-018：生产依赖安装、Vue 类型检查、Vite、FastAPI dist 和真实浏览器未通过。
- 09D-019：可信日历仍需取得并核验 2027 官方公告；不得扩大现有覆盖期限。

## 风险与回退

- 本轮只新增 GET 只读 API 和前端查询展示，不改变账本写入、数据库 schema、交易、策略或收益公式。
- 所有测试均使用临时合成数据库；没有操作用户真实数据库、没有正式接管、没有部署。
- 回退到 BASE：在本轮独立工作树中恢复 `e976b364158b73ff19cf711c27488c6e44917b35`。回退会移除本轮代码与文档，不应在用户原仓库直接执行破坏性命令。
- 09G 审查包生成后不再追加未审查代码修改。

## 工作量重新评估

09F 交付包证实源码 ZIP 与 Git HEAD 一致，但它仍将若干产品口径、浏览器构建、真实数据/硬件验收列为未完成。因此用户提出的 MVP 85%–90% 可以作为规划估算，不是测试可证明的完成率。根据 09F 源码与问题矩阵重新估算：网页端源码/自动化收尾约占项目总量 3%–5%；本地实机、真实数据、双机与部署验收约占 7%–10%；MVP 总剩余约 10%–15%。这是基于当前已知事项的工作量估算，若真实旧库/实网发现新缺陷可能增加。09G-A 只补齐一个网页端内部对账入口，不足以据此上调总体完成百分比。

## Git 检查点

- 09G BASE：`e976b364158b73ff19cf711c27488c6e44917b35`
- 09G 最终 HEAD：`de00f678ac5a6717c7c6e6b12d3362e00315b1c5`
- 分支：`09G-web-finalization`
- `09G-base` 分支保留准确起始基线。
- 最终提交 SHA 由本轮 Git Bundle/交付清单固定记录；最终工作树干净（提交后实际 `git status --short` 无输出）。
- 09F 历史及全部原始提交保留；未重建历史。

## 结论

`CHANGES_REQUIRED` — 09G-A 源码实现及专项测试完成，等待独立审查。

本轮未宣布 09D-010 CLOSED，不批准源码冻结或 V1.0 发布。
