# 10 本地验收交接

## 基线与产物

- BASE：`00c2d143f96b0d51e2dca7b5848f477c08071e73`（`09J-web-handoff`）。
- 本轮分支：`10-local-acceptance`；最终 HEAD 以交付 `SHA256SUMS.txt` 与 Git bundle 引用为准。
- 实际测试与证据：`10_LOCAL_ACCEPTANCE_REPORT.md`；问题处置：`ISSUES.md` 末节；变更：`CHANGELOG.md`。

## 本轮改了什么

1. `backend/infrastructure/durability.py`：新增 `fsync_file`，修复 Windows 只读句柄 `fsync` 导致的备份/恢复/接管/迁移全链路失败（S1）；`backup.py`、`restore.py`、`takeover.py` 改用该函数。
2. `backend/infrastructure/market/tencent.py`：日线刷新附加真实证券名称（进程内缓存，失败不影响价格），修复收盘后名称长期为空。
3. `frontend/src/pages/WorkspacePage.vue`：`strategyRows` 显式 `Fact[]`，修复首次真正执行的 `vue-tsc` TS2339。
4. `tools/verify_09f_browser.py`：标签定位不再用 `exact=True`（Chromium 141 可访问名含选项文本），断言未削弱。
5. 新增 `tests/unit/test_market_provider.py::test_closes_attaches_provider_name_...`；文档更新。

未改：数据库 schema、核心 V9/G2 公式、ATR、动态 3/10 日冷却、产品阈值、依赖锁。

## 已验证（实际）

- 锁定安装、完整 Vue typecheck、Vite 生产构建、FastAPI 托管新 dist、Windows 合成演示启停。
- `pytest -q` 339 PASS；`npm test` 63 PASS。
- 真实浏览器 17 项（双标签 1 + 扩展 10 + 安全 4 + 期初/分红 2）。
- 合成正式库：备份、恢复校验、每日备份、单写者、停机恢复、旧世代拒绝、重启登录、网页一键备份。
- 实网：三只标的当日/前日未复权收盘、来源与名称；日历指纹。

## 仍未完成（需用户/外部输入）

1. **四项产品口径**：退款原支出期、基础日收益率分母/精度、流动净资产分类、2% 仅展示阈值。确认后才写入 DECISIONS 与契约。
2. **2027 交易日历**：上交所官方页当前只有 2026；取得正式公告后按 `09F_LOCAL_ACCEPTANCE.md` 日历节更新锁文件与指纹。
3. **真实旧库试迁移**：需要用户授权并只使用 SQLite Backup API 一致性副本；未知 REFUND/REIMBURSEMENT 或名称映射必须阻断。
4. **Tailscale/双机**：本机未安装、无第二设备与公司许可；按要求使用 loopback + 私有 HTTPS，禁止 Funnel/公网。
5. **独立审查**：本轮 FIXED 项仍未由独立审查者关闭；不得以本报告替代。
6. **正式接管/部署/发布**：未获授权，未执行。

## 建议下一步

1. 用户确认四项口径 → 记录 DECISIONS，必要时定向改实现与测试。
2. 授权后：真实旧库一致性副本 dry-run → 独立对账 → 试点迁移。
3. 2027 官方公告发布后：更新 `exchange-calendars` 与 `uv.lock`，跑日历审查与边界测试。
4. 安装 Tailscale 并在两台设备上按 `OPERATIONS_SECURITY.md` 验收 Host/Origin/CSRF/会话。
5. 独立审查通过后，再申请正式接管与 V1.0 发布。
