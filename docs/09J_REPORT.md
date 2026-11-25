# 09J-near-stop-evidence｜开发与测试报告

## 1. 基线与目标

- BASE：`d2348fbc5b78cff641902ca95f5cfd9c6d07b3c4`（09I 最终 HEAD）。
- 分支：`09J-near-stop-evidence`。
- 目标：扩展 09H 已有主/子仓与过期报价专项，覆盖 near-stop 预警的拒绝条件和边界。
- 范围：仅新增测试；不修改业务源码、策略公式、保护价、真实成交、数据库 schema 或产品阈值。

## 2. 新增测试覆盖

新增 `tests/unit/test_09j_near_stop_edges.py`，使用临时正式角色 SQLite 数据库、真实 `StrategyService` 与 `PortfolioService.quote` 写入合成报价，不联网、不访问用户数据库。覆盖：

1. 已核实真实主仓/子仓分别计算，返回 display-only 提示及报价来源/时间/阈值。
2. 真实过期盘中报价不产生 near-stop 提示。
3. 同一交易日冲突收盘价不产生提示。
4. 缺少报价不产生提示。
5. 交易日历不可用时不产生提示。
6. 未核实信号、策略偏离、公司行为价格尺度失效任一出现时，保守抑制提示。
7. 账本证据版本不一致时抑制提示。
8. 非账本核实或已关闭模型持仓不产生提示。
9. 报价超出配置警示距离时无提示。
10. 主仓保护价达到与子仓尚在警示距离时分别返回 `protection_reached` / `near_stop`。

## 3. 实际测试

| 命令 | 结果 |
|---|---|
| `PYTHONPATH=. python -m pytest -q tests/unit/test_09j_near_stop_edges.py` | 12 PASS |
| `PYTHONPATH=. python -m pytest -q tests/unit/test_09j_near_stop_edges.py tests/unit/test_09h_stop_evidence.py tests/unit/test_09f_features.py` | 21 PASS，2.87s |
| `python -m compileall -q tests/unit/test_09j_near_stop_edges.py` | PASS |
| `git diff --check` | PASS |
| 完整 Python 回归 | NOT RUN，按工作流由独立审查执行 |
| 前端 npm test / Vue typecheck / Vite build | NOT RUN，本轮没有前端源码变化；09I 已记录 vue-tsc 缺失与 build 前置失败 |
| 真实浏览器、实网行情、Windows | NOT RUN |

## 4. 重要限制

- 200 bps 是当前服务显式配置下的候选展示阈值；测试证明实现边界，不代表产品负责人已批准该阈值。
- near-stop 仅为风险提示，不修改模型退出、真实持仓、策略信号或下单。
- 合成报价不代表腾讯/其他供应商实网质量；真实行情与浏览器仍待现场验收。

## 5. 回退与交接

- 本轮仅新增一个测试文件；回退只需在独立工作树恢复该文件到 BASE，不涉及数据库。
- 09D-012 不标记 CLOSED；产品确认 2% 阈值后，才可冻结显示口径。真实主/子仓与报价页面仍需浏览器验收。
- 下一阶段应先完成 09I 独立审查与可用依赖环境的 typecheck/build/browser；现场事项按 `09J_HANDOFF.md` 执行。

## 6. 结论

`CHANGES_REQUIRED` — 接近止损边界自动化证据已扩展，相关测试通过；产品阈值、真实行情、Vue 构建及浏览器尚未验收。等待独立审查，不批准源码冻结或 V1.0 发布。

## 7. 最终网页收尾与综合复测（后续提交）

- 交付统一以 09I HEAD `d2348fbc5b78cff641902ca95f5cfd9c6d07b3c4` 为增量 BASE，保留已存在的 09J 边界测试提交 `f48cb2e714c18f00311ec33e64d6df3bf89dbcbe`。09I ZIP 带顶层目录，初次未剥离目录的比对失败，修正后 167/167 与 Git 对象逐字节一致，09I 五项 SHA256 通过。独立克隆工作树干净，git fsck 通过。
- 新发现 09J-001：当前页面的非主查询（如持仓详情）收到网络超时，旧代码只统一处理 401，仍可能显示允许写入。实际 Workspace setup 新增复现 FAIL。修复统一 API 的连接故障判定（未知 HTTP 状态、401/403/408、5xx、无效成功响应），立即标记数据过期、禁写；401 另清除会话及业务数据。普通 409 等业务冲突不触发全局断线。旧连接代次的迟到失败不影响已重新核对的连接。
- PagedRecords 独立请求同样上报连接故障，所有七个列表接入主页面；旧列表代次/卸载的迟到失败不传播。保留持久 pending 和原 request_id，既有写入安全、策略公式及账本计算不变。没有变更数据库 schema、核心策略、依赖锁。
- 产品口径不擅自批准；已明确的功能按09I/09F矩阵保留。当前未发现新的必须先在本环境实现的大型功能，但不是逐条独立审查关闭或正式冻结证明。真实源结构相关迁移仍可能需要现场定向修改。

| 实际命令/范围 | 结果 |
|---|---|
| `PYTHONPATH=. python -m pytest -q`（09J 边界测试提交，前端故障修复前） | 338 PASS，74.47s，exit 0 |
| `PYTHONPATH=. python -m pytest -q tests/unit/test_09e_integrity.py tests/unit/test_security.py tests/unit/test_09i_closeout.py`（前端修复后） | 18 PASS，4.16s，exit 0，属于上述回归范围的复测，不相加 |
| `npm test` 修复前基线 | 60 PASS |
| 新增次查询网络超时、旧连接超时测试（修复前） | 1 FAIL、1 PASS；FAIL 为预期复现 |
| `npm test` 修复后最终 | 63 PASS，0 FAIL，包含实际 Workspace/PagedRecords setup 逻辑测试及既有测试；非真实 Vue 渲染 |
| 全局 tsc 限定检查：API、forms、ui 及全部普通 TS 测试 | 初次2个已有测试的断言流缩窄 TS2339；改为读取函数保留断言后 PASS；非 vue-tsc |
| `python tools/verify_calendar.py` | PASS，4.13.1；截至2026-12-31，未导入2027 |
| `python -m compileall -q backend tools start.py`、`git diff --check` | PASS |
| `npm run typecheck`、`npm run build` | FAIL，vue-tsc not found，exit 127；Vite 实际阶段 NOT RUN |
| `npm ci` | NOT RUN；当前无 node_modules，历史 DNS/缓存失败，不重复同环境安装 |
| 真实浏览器、实网、Windows、真实库、Tailscale | NOT RUN / BLOCKED（依赖实际构建、现场、资料） |

全量回归由本次开发助手在隔离工作树实际执行，不冒充另一独立审查者的关闭裁决。Python 业务源码未修改，前端改后再运行跨运行时真实 API/SQLite 等受影响专项。未重新取得原始策略参考ZIP，本轮原文件哈希未核验。未操作用户真实数据。最终状态：**网页端交接候选，CHANGES_REQUIRED（产品确认、独立关闭及生产/现场验收未完成）**；不再预设常规网页功能轮次，明确复现时才继续修复。
