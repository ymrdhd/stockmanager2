# 当前状态（每轮覆盖，不累计日志）

- 10-local-acceptance：以 `09J-web-handoff` HEAD `00c2d143f96b0d51e2dca7b5848f477c08071e73` 为 BASE，分支 `10-local-acceptance`；本地 V1.0 构建、真实浏览器、Windows 运维与实网行情已实际执行。**尚未正式接管、部署或发布。**
- 本轮修复 3 个实际缺陷：Windows 只读句柄 `fsync` 失败导致备份/恢复/接管/迁移 42 项测试全红（S1）；收盘后刷新不写证券名称导致名称缺失（S2）；验收脚本 `exact=True` 标签定位在 Chromium 141 失效（工具）。
- 实际通过：`npm ci` 52 包；`vue-tsc --noEmit` exit 0；`vite build` exit 0（dist/index.html 416B、CSS 2.31kB、JS 191.47kB）；`npm test` 63 PASS；`pytest -q` **339 PASS**（含新增名称测试）；真实 Chromium 演示 10 项 + 安全 4 组 + 期初/分红 2 项 PASS。
- Windows：合成演示（中文含空格路径）启动/停止/端口 9092 正常，FastAPI 托管新 dist；合成正式库备份→写者锁→停机恢复 generation 1→2→旧世代拒绝→重启登录 PASS；正式模式网页一键备份 PASS。
- 实网：腾讯源 510300/159915/600519 当日与前一交易日未复权收盘、来源/时点 PASS；真实名称已写入（沪深300ETF华泰柏瑞/创业板ETF易方达/贵州茅台）。日历 XSHG 4.13.1 指纹 PASS，可信上限仍 2026-12-31。
- 阻断/未跑：2027 官方日历（上交所仅有 2026 安排）BLOCKED；Tailscale 双机（本机未安装、无第二设备）BLOCKED；真实旧库试迁移（无授权安全副本）BLOCKED；四项产品候选未确认；独立审查关闭未完成。npm 10.9.7 与脚本锁定 10.9.2 有补丁差异，手工路径绕开 `windows.ps1 -Action Build`。
- 下一步：用户确认四项口径与真实旧库/部署授权 → 2027 官方日历取得后更新锁文件 → 独立审查 → 正式接管与发布。详见 `10_LOCAL_ACCEPTANCE_REPORT.md`、`10_HANDOFF.md`。
