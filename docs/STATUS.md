# 当前状态（每轮覆盖，不累计日志）

- 10A-口径批准与最终交付：BASE `26c981551cec7c3b8188b89664e5a5cbedde068f`（`10-local-acceptance`），分支 `10a-final-delivery`。四项业务口径已由产品负责人于 2026-11-25 正式批准并记录为 ADR-017～ADR-020。**尚未正式接管、部署或发布**；产品口径批准不等于部署授权。
- 定向核对结论：退款（ADR-017）、流动净资产（ADR-019）、接近止损 2% 预警（ADR-020）实现与批准文本一致，未改业务代码；当日投资收益率（ADR-018）存在一处显示精度差异（`.6f` → `.2f`），已最小修复。
- 实际通过（本轮受影响专项）：`pytest tests/unit/test_09f_features.py tests/unit/test_finance.py tests/unit/test_pnl.py -q` → **44 PASS**。未重跑全量回归（按开发/审查分工由独立审查执行），未重复构建前端（前端源码未变更）。
- 历史基线实测（10-local-acceptance 轮，未重跑）：`npm ci` 52 包；`vue-tsc --noEmit` exit 0；`vite build` exit 0；`npm test` 63 PASS；`pytest -q` 339 PASS；真实 Chromium 17 项 PASS；Windows 合成演示与正式库备份/恢复/写者锁 PASS；实网 510300/159915/600519 行情与名称 PASS。
- 仍未完成（不因口径批准而消除）：2027 官方日历 BLOCKED（上交所仅发布 2026 安排）；Tailscale 私有 HTTPS 双机 BLOCKED（本机未装、无第二设备）；真实旧库只读试迁移与逐项对账 BLOCKED（无授权一致性副本）；独立审查关闭未完成；正式接管与发布未获授权。
- 新观察（未改实现，见 `ISSUES.md` 10A-001～10A-003）：schema 缺少受限资产标记字段；前端已能原样展示两位小数；`liquidity_basis` 文案未逐字写入「排除受限资产」。
- 下一步：提交 10A 增量审查包 → 独立审查执行完整 Python 回归与关闭裁决 → 取得 2027 官方日历后更新锁文件 → 用户明确授权后才做正式接管与发布。详见 `10A_HANDOFF.md`、`10A_FINAL_REVIEW.md`。
