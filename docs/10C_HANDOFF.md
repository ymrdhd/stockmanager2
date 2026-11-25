# 10C 交接（最终最小补丁 → 定向复审）

## 1. 本轮结论一句话

在 10B 已通过审查的冻结检查点 `c646eb9` 之上，只做三项最小补丁：受限资产明确标记（方案 B）、Windows 安装脚本 npm 版本范围、冷却 regime 文档措辞。**未扩大 V1.0 功能范围，未操作真实数据库，未接管、部署或发布。**

## 2. 身份与基线

| 项 | 值 |
|---|---|
| BASE（冻结检查点） | `c646eb937b33345b50b1511f420cd6d00086ebf9` |
| 分支 | `10c-final-minimal-patch` |
| 起始工作树 | `git status --short` 为空 |
| 复用环境 | 现有 `.venv`（Python 3.14.4 / uv 0.11.7）、`frontend/node_modules`（52 包）、`frontend/dist`、本机 Chromium 141 |
| 临时 Node 22 | `artifacts/tmp/node22`（仅用于实测安装脚本，未进 Git、未改系统 PATH） |

## 3. 修复内容

### 3.1 10B-001 受限资产识别（方案 B）

- **schema（Alembic `0012_liquidity_status.py`，SCHEMA_VERSION 11→12）**
  - `accounts.liquidity_status TEXT NOT NULL DEFAULT 'unknown' CHECK(IN ('liquid','restricted','unknown'))`
  - `account_liquidity_events(id, account_id, previous_status, liquidity_status, effective_at, reason, kind)`
  - 索引 `account_liquidity_history(account_id, effective_at)`
  - 迁移不给既有账户写任何分类证据 → 全部保持 `unknown`，**不无依据认定为不受限**。
- **应用层（`backend/application/finance.py`）**
  - `liquidity_status_at()` 按截止时点取当时证据；无证据返回 `unknown`。
  - `_overview`：流动资产只汇总 `liquid` 资产账户；`restricted` 计入 `restricted_assets_fen` 且仍在 `total_assets_fen`/`net_worth_fen`；`unknown` → `liquid_assets_fen`/`liquid_net_worth_fen` 为 `null` 并逐账户 `liquidity_classification_unknown:<id>`。
  - 负债仍按已批准的现行流动负债口径，不接受资产受限标记。
  - `set_account_liquidity()`：`expected_ledger_revision` CAS、`append-only` 生效时间（不能早于账户生效日或最近一次分类）、写 `audit_events(event='liquidity_changed')`、幂等重放返回原结果。
  - `restricted_assets_fen` 同时进入 `net_worth_trend` 每行与 `amount_evidence`。
- **API**：`POST /api/v1/accounts/{id}/liquidity`（`status`、`occurred_at`、`note`、`expected_ledger_revision`）。
- **前端**：账户表新增「流动性分类」列；财务页新增「流动性分类」表单；总览/趋势/历史显示受限资产与口径说明；reason 文案改为「流动性分类待确认（受限/不受限），暂不计入流动资产」。
- **部分受限资金**：V1 采用拆分账户记录（两个账户余额合计等于实际总额），表单 `confirm` 与页面说明均写明，不做复杂冻结资金系统。

### 3.2 10B-002 安装脚本版本守卫

- npm：`-ne '10.9.2'` → 主版本 10 且次版本 ≥9（`10.9.x`）。
- Node：保持 `>=22.13.0 <23`（原正则已如此，仅修正提示文案为实测 22.22.2）。
- Python：新增 `Assert-Python`，要求 `.venv` 解释器 ≥3.11（`uv` 按 `requires-python >=3.11` 与 `uv.lock` 选择）；`Install` 在 `uv sync --locked` 后检查。
- 未删除任何检查，未升级 `uv.lock` / `package-lock.json`。

### 3.3 10B-003 文档措辞

- `docs/reference/strategy_rule_map.md`：冷却 regime 来源改为「按当次评估日的 G2 regime 计算，不按买入信号日，也不冻结卖出日状态」，与 `_cooldown_state(..., regime.state)` 一致。
- 未改 `cooldown_required_days`、3/10 日阈值、`cooldown_until_date`、ATR、保护价、近止损。

## 4. 实测结果（全部合成库，真实输出见 `10C_TEST_REPORT.md`）

| 项目 | 结果 |
|---|---|
| `pytest -q` 全量 | **346 passed**（历史 339 + 新增 7） |
| `pytest tests/unit/test_10c_restricted_assets.py` | 7 passed |
| `tests/unit/test_finance.py` + `test_09f_features.py` + `test_database.py` + `test_database_upgrade.py` | 53 passed |
| 前端 `npm test` | 63 passed |
| `vue-tsc --noEmit` | exit 0 |
| `vite build` | exit 0（23 modules，dist 生成） |
| 真实 Chromium 受限资产专项 | **9/9 PASS**（分类展示、UI 提交、受限不计流动、总额不变、历史不倒推、审计、未知提示） |
| Windows `-Action Install`（Node 22.22.2 / npm 10.9.7） | exit 0（uv sync + npm ci 52 包 + typecheck + build） |
| 越界版本拒绝 | npm 11.17.0 → 明确拒绝；Node 24 → 明确拒绝 |
| 数据库升级/备份/恢复专项 | 7 项专项含 0011→0012 升级、重开、备份恢复 |

## 5. 未做 / 仍未完成

- 未操作真实数据库、未执行接管、未部署、未发布。
- 2027 官方日历 BLOCKED（上交所仅发布 2026 安排）；真实旧库只读试迁移 BLOCKED（无授权一致性副本）；Tailscale 双机 BLOCKED（无第二设备与许可）。
- 未重跑与修改无关的历史浏览器场景（按任务书只做受影响专项）。
- `ISS-003` 全仓 Ruff/mypy 既有遗留仍 OPEN（与本轮功能无关；本轮新增/修改文件已单独检查）。

## 6. 复原与复核

```powershell
git clone --branch 10c-final-minimal-patch .\Stock_Manager_10C_recovery.bundle review
cd review
git rev-parse HEAD                # 本轮 10C HEAD
uv sync --locked
uv run --locked python -m pytest -q
cd frontend; npm ci; npm test; npm run build; cd ..
```

安装脚本复核（需要 Node 22.13+ / npm 10.9.x）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\windows.ps1 -Action Install
```

## 7. 定向复审范围（只审本轮，不重审冻结代码）

1. `migrations/versions/0012_liquidity_status.py` 与 `SCHEMA_VERSION`/`REVISION`、`allow_previous` 映射。
2. `backend/application/finance.py` 的 `liquidity_status_at`、`_overview` 流动资产/受限资产/未知分类、`set_account_liquidity`。
3. `backend/application/trading/service.py` 的 `AccountInput.liquidity_status` 与创建时 `initial` 事件。
4. `backend/api/finance.py` 新路由。
5. `tools/windows.ps1` 版本守卫。
6. 前端 `forms.ts`（`accountForm`/`liquidityForm`）、`WorkspacePage.vue`（列/入口/说明）、`ui.ts`（reason 文案）。
7. 新增 `tests/unit/test_10c_restricted_assets.py` 与受影响测试调整。

## 8. 交付物

| 文件 | 内容 |
|---|---|
| `Stock_Manager_10C_final_delivery.zip` | 合并交付包：源码、增量审查、Git Recovery Bundle、MD、日志、SHA256 |
| `10C-source.zip` | 完整源码 |
| `10C-incremental-review.zip` | 相对 BASE 的增量 + manifest + REVIEW |
| `Stock_Manager_10C_recovery.bundle` | 全历史 Git bundle |
| `10C-documents-and-logs.zip` | MD 文档、实际日志、截图、清单 |
| `SHA256SUMS_10C.txt` | 上述文件哈希 |
