# 09H 交接

## 恢复
优先使用 `Stock_Manager_09H_recovery.bundle`；与 `09H-source.zip` 逐字节比对。BASE `0e7a018ea0d44c87671679b36ad7da4f6bdc313c`，实际最终 HEAD 见交付 README。保留 `09H-base` 与 `09G-audit-recovered`，不得用旧报告 SHA 覆盖实际提交。

## 网页优先剩余
- 按09H_FUNCTION_MATRIX核对系统端口/重启提示、退款/其他领域审计与历史数据质量，逐项完成明确规格；未经批准不编造流动性分类、退款期、收益率或阈值。
- 独立审查本轮差异，执行新增缺陷复现、受影响回归、完整Python回归。
- 有正常依赖和浏览器条件时 `cd frontend; npm ci; npm run typecheck; npm run build; npm test`。禁止跳过 vue-tsc 或使用旧 dist。使用合成演示库运行 FastAPI，真实测试会话失效、断线、数据库身份变化、双标签防重、账户切换及审计/账务证据。
- 本轮 Vue 编译尚未通过；不得标记为冻结或发布。

## 本地 Agent 集中验收（尚未启动）
1. 正常锁定依赖构建，Windows 安装、启动/停止、端口和重启；保留正式库与沙盒隔离。
2. 真实行情三只标的、可信官方2027日历（无可信证据不延长）；先演示再实测，禁止伪造通过。
3. 真实旧库只读探测、隔离预演、三类逐项独立对账。差异未解决不允许正式接管。
4. 备份、恢复、文件锁/断电故障、身份和 generation、双电脑 Tailscale 私网及浏览器认证；任何正式接管和部署必须另行明确批准。
5. 保留全部真实日志、版本和失败证据，不能将环境不足归为PASS。

参考 09F_LOCAL_ACCEPTANCE、09C_LOCAL_ACCEPTANCE 及 AGENTS；以本轮更新状态为准。
