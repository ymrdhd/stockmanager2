

# 09H
- 基线 `0e7a018ea0d44c87671679b36ad7da4f6bdc313c`；恢复遗漏的09G审计代码/测试，保留内部对账分支成果及原历史。
- 会话及身份恢复安全、财务金额来源与审计说明。
- 新增合成主/子仓近止损证据及过期/版本/核实保护测试。
- 98 Python专项、55前端测试通过；生产类型检查仍失败，未发布。

# 09I
- 09H 基线完整恢复；财务只读审计接口及页面、退款直接关联与历史来源说明、查询竞争隔离。
- 端口输入及启动命令预览、参数范围验证；保留手工停启与安全边界。
- 37 Python专项、60前端自动化通过；正式Vue类型检查仍失败，未生成运行包。

# 09J
- 在 09I 最新基线上补充 12 项接近止损证据边界测试，覆盖主/子仓、真实报价新鲜度、冲突/缺失行情、日历失败、未核实/偏离/尺度失效证据及版本变化。
- 与 09H 主/子仓测试、09F 距离函数回归合计本轮定向 21 PASS。
- 不修改策略、账本或产品阈值；2% 仍待产品确认。完整 Vue 构建/浏览器及本地验收未完成。
- 详见 `09J_REPORT.md`、`09J_HANDOFF.md`。
- 最终交接补充：复现并修复次查询网络故障仍可写入，统一禁写与分页上报、旧连接代次保护；新增3项前端测试。338 Python全量（修复前）/18受影响专项（修复后）、63前端自动化及限定TS通过。正式Vue仍缺依赖，未发布。

# 10-local-acceptance
- BASE `00c2d143f96b0d51e2dca7b5848f477c08071e73`（09J-web-handoff）；分支 `10-local-acceptance`。
- 修复 Windows 只读句柄 `fsync` 失败（新增 `durability.fsync_file`），恢复备份/恢复/接管/迁移在 Windows 的可用性；42 项原失败转 PASS。
- 修复收盘后证券名称不写入：腾讯日线刷新附加真实名称，新增 1 项测试；实网核对 510300/159915/600519。
- 修复 `WorkspacePage.vue` 策略行推断类型导致的 `vue-tsc` TS2339；首次完成正式 Vue typecheck 与 Vite 生产构建。
- 修复浏览器验收脚本标签定位（Chromium 141 可访问名含选项文本）。
- 实际执行 Windows 合成演示、真实浏览器 16 项、合成正式库启停/单写者/备份/恢复/旧世代拒绝、实网行情与日历指纹；文档更新见 `10_LOCAL_ACCEPTANCE_REPORT.md`、`10_HANDOFF.md`。
- 未改数据库 schema、核心策略公式、ATR/冷却规则、产品阈值与依赖锁；未接管或覆盖真实库，未部署发布。

# 10C-final-minimal-patch
- BASE `c646eb937b33345b50b1511f420cd6d00086ebf9`（10B 独立审查已通过的冻结检查点）；分支 `10c-final-minimal-patch`。
- 修复 10B-001：Alembic `0012` 新增 `accounts.liquidity_status`（`liquid`/`restricted`/`unknown`，默认 `unknown`）与 `account_liquidity_events`；受限资产仍计入总资产/净资产但不计入流动资产/流动净资产；未知分类不计算完整指标并提示待确认；旧库升级不伪造分类；分类变更写审计且历史查询按生效时点取证据；负债沿用现行流动负债口径。新增 `POST /api/v1/accounts/{id}/liquidity`、账户页「流动性分类」入口与说明。同步修改 `liquidity_basis` 文案，修复 10A-003。
- 修复 10B-002：`tools/windows.ps1` 的 npm 精确 `10.9.2` 改为 `10.9.x`（主版本 10、次版本 ≥9），Node 保持 `>=22.13 <23`，新增 `.venv` Python ≥3.11 检查；未删除检查、未升级依赖锁。Node 22.22.2 / npm 10.9.7 下 `-Action Install` 实测通过，npm 11.17.0 与 Node 24 仍被拒绝。
- 修复 10B-003：`docs/reference/strategy_rule_map.md` 冷却 regime 来源改为「当次评估日 G2 regime」，与实现一致；未改任何策略公式、3/10 日阈值或近止损规则。
- 实测：Python 全量 346 PASS（新增 7 项受限资产/迁移/备份恢复专项）；前端 63 PASS、`vue-tsc --noEmit` exit 0、`vite build` exit 0；真实 Chromium 受限资产专项 9/9 PASS。全部使用合成库。
- 同轮追加用户需求：根目录双击入口 `打开网页.cmd`（调用 `tools/open_preview.py`），只运行 `data/preview-app/` 下的合成演示库、仅本机监听、端口冲突自动顺延、首次交互设密码、出错停住窗口；不触碰正式库。实测演示库 3 账户/4 成交、Chromium 登录与页面通过。
- 文档：`STATUS.md`、`ISSUES.md`（10A-001/003 标记已修 + 新增 10C 处置表）、`DECISIONS.md`（ADR-021）、`PRODUCT_SPEC.md`、`INTERFACE_CONTRACT.md`、`DATA_CONTRACTS.md`、`OPERATIONS_SECURITY.md`、`README.md`、`INDEX.md`、新增 `10C_HANDOFF.md` 与 `10C_TEST_REPORT.md`。
- 未操作真实数据库、未正式接管、未部署发布；2027 日历、真实旧库试迁移与 Tailscale 双机仍 BLOCKED/未授权。

# 10A-final-delivery
- BASE `26c981551cec7c3b8188b89664e5a5cbedde068f`（`10-local-acceptance`）；分支 `10a-final-delivery`。
- 产品负责人 2026-11-25 正式批准四项业务口径，记入 `DECISIONS.md` ADR-017（退款冲减原支出期、到账单独记录）、ADR-018（当日投资收益率分母为上一有效交易日收盘证券权益、两位小数、分母缺失或非正不计算）、ADR-019（流动净资产=流动资产−流动负债，含现金/银行卡/支付账户/可变现证券，排除受限资产，未知分类提示待确认）、ADR-020（接近保护价 2% 内风险预警，不改变 V9/G2 规则，NDX T5 独立）。
- 定向核对：退款、流动净资产、接近止损三项实现与批准文本一致，未改业务代码；当日收益率存在显示精度差异，`backend/application/portfolio.py:1313` 与 `backend/application/finance.py:701` 由 `.6f` 收敛为 `.2f`，同步更新 `tests/unit/test_09f_features.py:44-45` 断言为 `6.67`。分子分母、不可计算判定与原因码均未变。
- 受影响专项：`pytest tests/unit/test_09f_features.py tests/unit/test_finance.py tests/unit/test_pnl.py -q` → 44 PASS。未重跑全量回归（由独立审查执行），前端源码未改故未重复构建。
- 文档：`PRODUCT_SPEC.md`（2.1/2.2/2.4 写入四项批准口径）、`DECISIONS.md`（ADR-017～ADR-020）、`INTERFACE_CONTRACT.md`（新增「四项已批准业务口径的接口约束」节）、`STATUS.md`、`ISSUES.md`（09D-009/011/012 状态更新 + 新增 10A 定向核对表与 10A-001～003 观察）、`09F_PRODUCT_CANDIDATES.md`（候选 → 已批准并标注落地状态）、`INDEX.md`、新增 `10A_HANDOFF.md` 与 `10A_FINAL_REVIEW.md`。
- 未改 schema、策略公式、ATR/冷却规则、保护价/批次逻辑、依赖锁；未操作真实数据库、未正式接管、未发布。

