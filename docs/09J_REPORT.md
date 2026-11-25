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
