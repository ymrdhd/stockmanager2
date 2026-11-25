# 当前状态（每轮覆盖，不累计日志）

- 10C-final-minimal-patch：BASE `c646eb937b33345b50b1511f420cd6d00086ebf9`（10B 已通过的冻结检查点），分支 `10c-final-minimal-patch`。本分支只做 10B-001、10B-002 与文档措辞三项最小补丁，未扩大 V1.0 功能范围。**尚未正式接管、部署或发布**。
- 10B-001（受限资产识别，方案 B）已修：Alembic `0012` 新增 `accounts.liquidity_status`（`liquid`/`restricted`/`unknown`，默认 `unknown`）与 `account_liquidity_events`（分类分时点证据）；新接口 `POST /api/v1/accounts/{id}/liquidity`；账户页新增「流动性分类」入口与审计。受限资产仍计入总资产与净资产，不计入流动资产与流动净资产；未知分类不计算完整指标并提示待确认；旧库升级保持 `unknown`，不自动认定为不受限；负债仍按已批准流动负债口径。历史查询只用该时点已记录的分类证据，不用今天的分类倒推。
- 10B-002（安装守卫）已修：`tools/windows.ps1` 的 npm 精确 `10.9.2` 改为 `10.9.x`（主版本 10、次版本 ≥9），Node 仍要求 22.13–22.x；新增 `.venv` Python ≥3.11 检查。实测 Node 22.22.2 / npm 10.9.7 下 `-Action Install` 完整通过（`uv sync --locked`、`npm ci` 52 包、`vue-tsc`、`vite build`）；npm 11.17.0 与 Node 24 仍被清晰拒绝。未升级依赖锁文件。
- 10B-003（冷却 regime 措辞）已修：`docs/reference/strategy_rule_map.md` 改为「按当次评估日的 G2 regime 计算，不按买入信号日，也不冻结卖出日状态」，与 `_cooldown_state` 入参一致；策略公式、3/10 日阈值与已记录完成日均未改。近止损仍按 ADR-020 的有效报价与 2% 风险提示，不新增退出条件。
- 实测（本轮全部合成库）：`pytest -q` → **346 PASS**（含新增 7 项受限资产/迁移/备份恢复专项）；前端 `npm test` **63 PASS**、`vue-tsc --noEmit` exit 0、`vite build` exit 0（23 modules）；真实 Chromium 受限资产页面专项 **9/9 PASS**；Windows 标准安装脚本在 Node 22.22.2 / npm 10.9.7 实际通过，越界版本仍被拒绝。日志见 `10C_TEST_REPORT.md`。
- 用户补充需求（同轮追加）：根目录新增双击入口 `打开网页.cmd` + `tools/open_preview.py`，只在 `data/preview-app/` 创建并运行合成演示库、仅监听 127.0.0.1；端口被占用时自动改用下一个空闲端口；首次运行交互设置密码；出错时窗口停住显示原因。实测：合成库 3 账户/4 成交、端口冲突自动改用 9092、Chromium 登录与总览/财务页通过、未触碰 9091 上已有实例与正式库。
- 仍未完成（不因本轮补丁而消除）：2027 官方日历 BLOCKED（上交所仅发布 2026 安排）；Tailscale 私有 HTTPS 双机 BLOCKED；真实旧库只读试迁移与逐项对账 BLOCKED（无授权一致性副本）；正式接管与发布未获授权。`ISS-003` 全仓 Ruff/mypy 既有遗留仍 OPEN（与运行功能无关）。
- 下一步：提交 10C 增量审查包 → 独立审查只复审本轮变更（受限资产标记、schema 迁移、流动净资产、安装脚本）→ 通过后更新最终冻结 HEAD → 仅推进剩余真实环境验收。详见 `10C_HANDOFF.md`。
