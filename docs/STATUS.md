# 当前状态（每轮覆盖，不累计日志）

- 09G-A `ledger-reconciliation-preview`，BASE `e976b364158b73ff19cf711c27488c6e44917b35`，HEAD `de00f678ac5a6717c7c6e6b12d3362e00315b1c5`，分支 `09G-web-finalization`；内部只读对账预览源码完成，等待独立审查。
- 09F 基线：Bundle/HEAD/源码 ZIP 一致，153/153 逐字节 PASS；09E 与 09F 历史保留。
- 新增 `GET /api/v1/portfolio/{account_id}/reconciliation`，同一只读 SQLite 快照核对卖出分配、剩余批次/持仓、现金、行情、收益勾稽、pending 和未解决公司行为；投资页显示批次来源/分配与异常，不自动改账，不代表外部券商/旧库对账。
- Python 受影响专项 94 PASS；前端 Node/Workspace setup 47 PASS；compileall、Git diff check PASS。
- npm ci FAIL（registry DNS EAI_AGAIN）；vue-tsc/typecheck 与 Vite build BLOCKED；真实浏览器 NOT RUN；Python 全量回归由独立审查执行。
- 09D-005/007/008/009/011/012/013/016/017 仍有源码/口径/验证缺口；09D-018 生产构建/浏览器阻塞；09D-019 2027官方日历阻塞。详见 `09G_FUNCTION_MATRIX.md`、`09G_REPORT.md`、`ISSUES.md`。
- 仅临时合成库；未操作真实数据库、未正式接管、未部署、未批准源码冻结或 V1.0 发布。
