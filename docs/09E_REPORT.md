# 09E-web-S1-integrity-fixes 开发报告（待独立审查）

BASE `9d9ad14ca629d1f563a370077d9426035efbf36b`；分支 `09E-web-S1-fixes`；09C BASE `9b88513c360a70473ef1b340415fb1a81b979ff3`。09C ZIP 140/140 与原提交字节一致。保留原策略与所有历史提交。未操作真实库、未部署、未启动本地 Agent。

## 三项修复及证据

- 09D-001：修复前新增 pending 竞争测试 FAIL（两个不同 ID）。`client.ts` 的同源全局写槽使用持久化 terminal fence，表单明确打开时捕获意图，锁内校验，先写终态再删 pending；等待者不能创建新 ID。未知结果保留原 ID，恢复操作绑定所核对的 ID。明确打开新表单后允许相同内容；不以内容去重。不同账户仍共享单一写槽，旧表单保守拒绝并提示重新核对；并非跨浏览器/跨 origin 的业务去重，也不保证存储被人工清除后的身份。`pending.test.ts` 两独立对象、共享存储与锁，延迟成功、拒绝、丢响应、重开、显式独立意图及账户隔离通过。`intent.integration.ts` + `test_09e_integrity.py` 通过真实 FastAPI/SQLite（Node 模拟 HTTP 传输，不是浏览器），第一意图实际 1 成交/1 现金流水/1 批次，第二意图相同内容合法入账；另一账户丢响应后原 ID 重试，无重复事实。全局 fence 机制需真实浏览器验收。
- 09D-002：修复前新增 `test_strategy_service.py` 偏离成交证据测试 FAIL（返回正式证明）。`trading/service.py` 拒绝偏离成交的合规证明，保留真实成交、信号引用、偏离标记/原因；旧请求回执的错误派生证明在读取时清理，不改历史事实与审计。`portfolio.py` 与 `strategy/service.py` 暴露偏离原因，已核实模型动作闸门不放宽。合成测试覆盖正常成交、更正为偏离、实际持仓与现金、关闭重开、旧回执、恢复正常证明、更正/作废；未改变纯策略公式、冻结ATR或动态冷却规则，T5 独立证据分类 09D-004 仍未解决。
- 09D-003：原 Workspace SFC setup 两独立测试 FAIL（立即清空/导航）。`accountScope.ts` 账户+代次；`WorkspacePage.vue` 同步清空账户级视图、废弃旧请求、验证响应账户/代次，失败清空并显示错误；`PagedRecords.vue` 绑定查询及账户、重建分页；策略池 API 明确 account_id。覆盖 A→B、迟到A、A→B→A、导航、B失败、收益/策略/成交详情；真实脚本在 VM 中以同步 ref/watch 和可控 API 替身运行，未执行 Vue DOM 或浏览器。原版本通过 `SM2_WORKSPACE_SOURCE` 指向从 BASE 提取的原始 SFC 运行同一测试。未处理 09D-005/006 等其他事项。

## 实际执行

| 命令/证据 | 结果 |
|---|---|
| 修复前 pending/strategy/workspace 新测试 | FAIL，见 before-client/strategy/account.log |
| `PYTHONPATH=. python -m pytest -q tests/unit/test_09e_integrity.py` | 2 PASS，2.17s；首次夹具字段错误已修，保留初次失败日志 |
| `PYTHONPATH=. python -m pytest -q` | 302 PASS，83.02s，exit 0 |
| `python -m compileall backend tools` | PASS，exit 0 |
| `cd frontend; npm test` | 43 PASS，exit 0 |
| `npm ci --fetch-retries=0 --fetch-timeout=15000` | FAIL：npm 退出异常，debug 摘录 EAI_AGAIN registry.npmjs.org |
| `npm run typecheck` | FAIL：vue-tsc not found，exit 127 |
| `npm run build` | FAIL：前置 vue-tsc，Vite 步骤 NOT RUN |
| 全局 tsc 5.8.3，严格 noEmit，仅 API、AccountScope 和 TS 测试（非 Vue SFC） | PASS；首次发现测试断言流缩窄导致 TS2339，改用读取函数后通过；保留原日志 |
| 实际浏览器双页面及生产 dist | BLOCKED：无本轮 dist；09D 已实测管理员策略阻止 localhost，09E 未重复该探测 |

限定 tsc 命令（当前容器特定路径）：`tsc --strict --noEmit --target ES2022 --module ESNext --moduleResolution Bundler --lib ES2022,DOM,DOM.Iterable --types node --typeRoots /opt/nvm/versions/node/v22.16.0/lib/node_modules/ts-node/node_modules/@types --allowImportingTsExtensions --skipLibCheck src/api/client.ts src/api/accountScope.ts tests/*.test.ts tests/intent.integration.ts`。不替代锁定 vue-tsc。测试组有重叠，不相加。

## 剩余关卡及回退

三个问题已有代码及自动化通过证据，尚未获独立关闭裁决。没有新增已确认 S0/S1，但不能证明不存在。09D-004–019 保留；特别是 T5 分类、会话恢复、收益版本、基础退款/搜索、产品口径及运维入口，均未在本轮展开。待可用网页环境依次 npm ci、typecheck、build、实际 FastAPI dist、真实双页面/账户切换验收，独立审查后方可关闭。本轮不提供运行包。整体 CHANGES_REQUIRED：生产类型检查未通过，浏览器未验收。回退在隔离源码克隆中切换至原 09C HEAD，不回写任何业务数据库，不回滚真实成交；保留 pending 请求并先核对账本，不清除持久化记录冒充新意图。无 schema 迁移。禁止正式发布。
