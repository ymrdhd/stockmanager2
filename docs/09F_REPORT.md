# 09F 开发及专项测试报告

BASE `ee012a80c5efdb5869bc1b72a559445b67f53225`，分支 `09F-final-web-feature-completion`。09E 7项清单与146个源码文件、19个原变更均保留。原始策略 ZIP SHA256 `1edc58feabbe2e32cda9bfb2bd34f205f4cd3c4b0b3e008e5910002b6af6cc06`。原策略纯计算与09E客户端写意图核心不变。全部操作隔离合成库；无真实库、部署或本地 Agent。

## 实际证据
- `PYTHONPATH=. python -m pytest -q tests/unit/test_backup_restore.py`：15 PASS，7.56s（真实子进程 os._exit 故障注入；最初集成2 FAIL/24 PASS，WAL/SHM边文件阻断回滚已修复）。
- `PYTHONPATH=. python -m pytest -q tests/unit/test_finance.py tests/unit/test_database_upgrade.py tests/unit/test_pnl.py tests/unit/test_09c_history.py tests/unit/test_market.py tests/unit/test_market_resolution.py tests/unit/test_backup_restore.py`：91 PASS、2 FAIL，既有并发测试的替换函数不接收新增形参；恢复内部调用契约后重跑。
- 第一轮扩大范围（新09F、finance、09c_ndx、09e_integrity、backup_restore、strategy_service、database_upgrade、09c_history、market、market_resolution、pnl）：136 PASS、1 FAIL；开发无认证中间件环境仍可调用备份，补充接口级认证及 identity 声明校验，使用实际认证应用重测。
- `PYTHONPATH=. python -m pytest -q tests/unit/test_09f_features.py tests/unit/test_finance.py tests/unit/test_09c_ndx.py tests/unit/test_09e_integrity.py tests/unit/test_backup_restore.py tests/unit/test_strategy_service.py tests/unit/test_database_upgrade.py tests/unit/test_09c_history.py tests/unit/test_market.py tests/unit/test_market_provider.py tests/unit/test_market_resolution.py tests/unit/test_pnl.py tests/unit/test_takeover.py tests/unit/test_09c_migration.py tests/unit/test_migration.py tests/unit/test_security.py`：196 PASS 48.84s。09E真实合成API/SQLite跨运行时双意图、偏离证据重启、更正作废回归均通过。
- 后续补强回滚身份检查及 v10 有账单更正链升级：`PYTHONPATH=. python -m pytest -q tests/unit/test_09f_features.py tests/unit/test_backup_restore.py tests/unit/test_takeover.py`：29 PASS 9.34s。
- `cd frontend && npm test`：43 PASS；增加实际SFC setup断线禁写、月摘要版本冲突测试后最终45 PASS（0 FAIL）。非 Vue 渲染、非浏览器。各测试组重叠，不相加。
- `python -m compileall -q backend tools migrations start.py`：PASS；Git diff 检查保留 Windows CRLF，使用 `git -c core.whitespace=cr-at-eol diff --check`。
- 本轮不重试相同依赖安装/浏览器环境。npm ci / vue-tsc / Vite / FastAPI dist / 实际浏览器：NOT RUN（09D/E 历史 DNS EAI_AGAIN，浏览器 ERR_BLOCKED_BY_ADMINISTRATOR）；未生成运行包。新增真实浏览器验证源码 `tools/verify_09f_browser.py` 仅完成语法检查，实际执行 NOT RUN。完整 Python 回归由独立审查执行，未在本轮运行。Windows/真实旧库/真实行情/2027官方日历/Tailscale均NOT RUN或资料BLOCKED。

## 缺陷及限制
09D-015 原普通恢复在两个文件发布期间无维护标记，升级 S1，复用接管 marker 与离线回滚；实际进程中断/再次中断测试PASS，Windows物理断电仍未验收。09D-001~003保持FIXED待审查。09D-004、006、009、014及015获得专项证据；005、007、008、010、011、012、013、016、017仍有下列边界：重连/会话/所有页面真实交互未验证；名称实网数据未验证；旧退款迁移映射仍阻断；旧批次核验/权益校正仅增加明确现金差额入口，未完成完整逐项预览；未分类受限资产/负债不计算完整流动净资产；接近止损候选及完整主子仓/过期报价专项待补；一键备份认证和去重通过，端口只显示监听信息、受控重启说明；账单独立审计及逐金额质量展示不完整。新增备份端点拒绝非 server，演示库不能调用正式备份。浏览器脚本尚未执行，不应宣称可用。

不建议无条件源码冻结。未发现最后专项仍可复现的 S0/S1，但不是独立审查结论。建议后续综合独立验收，明确上述源码缺口；产品口径冲突保留，未关闭历史问题。参见 `09F_FUNCTION_MATRIX.md`、`09F_PRODUCT_CANDIDATES.md`、`09F_LOCAL_ACCEPTANCE.md`、`ISSUES.md`。回退源码可用09E提交；若数据库已升级0011，不得使用旧程序直接打开；保留升级前备份，显式离线演练并核对事实，不能自动降级或覆盖更新后账本。
