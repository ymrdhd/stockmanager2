# 09C 本地 Agent 最终构建与验收任务书（未批准正式部署）

以 09C 完整源码 ZIP 或最新恢复 bundle 为起点，不叠加历史增量。使用 SHA256SUMS 核验附件，git clone Stock_Manager_09C_recovery.bundle 后核对交付报告的 HEAD，git status --short 应为空；原始策略 ZIP 独立保存。使用无个人数据的独立目录（含中文与空格，例如 `C:\Stock Manager 演示`），先完成演示验收。以下命令在 PowerShell 中从项目根目录运行；路径为示例，按本机替换。09C 未实际运行 Windows、浏览器或双机。本文件只是移交候选任务，不启动本地 Agent；源码覆盖差距由独立审查处理后，必须获得产品侧明确授权才执行。不要将本手册视为正式迁移授权。

## 1. 安装、构建与服务

安装 Python 3.11+（本轮 Linux 3.13.5）、uv、Node >=22.13.0 <23（基准 22.16.0）、npm 10.9.2。保持 `uv.lock`、`frontend/package-lock.json` 不变，不以 npm install/删除锁文件替代。用本机允许的网络访问依赖仓库；不要绕过企业限制。

```powershell
.\tools\windows.ps1 -Action Install
# 上述执行 uv sync --locked；在 frontend 执行 npm ci、npm run typecheck、npm run build
.\tools\windows.ps1 -Action Demo -Destination 'C:\Stock Manager 演示\synthetic'
.\tools\windows.ps1 -Action DemoStart -Destination 'C:\Stock Manager 演示\synthetic' -Port 9092
```

首次 DemoStart 会交互设置独立演示密码；打开 `http://127.0.0.1:9092/`。停止演示用 Ctrl+C。演示数据是合成成交与价格，不证明真实行情；策略行情标记未核实，不能输出可执行建议。不得替换正式库。演示目录必须是新目录。不要使用 `--allow-missing-web-build` 来通过浏览器验收。确认首页及静态资源由 FastAPI 的 9092 端口返回，不启动 Vite。记录 install/typecheck/build 各退出码、dist 文件哈希及浏览器截图。09 的缓存错误为 `ENOTCACHED ... vue-tsc-3.3.14.tgz ... only-if-cached`，随后 `vue-tsc: not found`；09B/09C 不重试同一受限环境，不删除类型检查。

## 2. 浏览器与并发验收（演示库）

按 `ACCEPTANCE_TESTS.md` 的核心业务用例记录实际操作、预期/结果、HTTP 状态及账本前后计数/版本。登录、资产与当日/累计收益、证券与账户、买卖及更正/作废、期初更正、分红及已支持公司行为、财务余额/账单/转账、V9/G2 当前/历史及数据不可用降级、日历有效期、重启、备份/恢复等。所有修改仅在演示库进行。

同源双标签：登录两页，对同一笔提交确认两页复用持久化请求 ID，不生成第二笔实际成交；让响应在服务端已提交后丢失（用本地测试代理或浏览器调试器模拟），刷新、关闭重开、网络恢复、会话过期后重新登录；核对同一 ID 重试、唯一有效成交/批次/资金事实及 ledger revision。不同数据库身份或 generation 必须拒绝旧 pending 自动提交。不要只看 UI 提示；核对后端计数。Web Locks 只在同源范围，跨设备后端幂等同一 request_id，不把两笔不同 ID 的真实成交误删。真实双标签本轮未运行。

## 3. 隔离迁移和审批

只用已关闭旧服务的脱机副本。SQLite 有活动 WAL/SHM/journal 时先用旧系统的 SQLite Backup API 制作一致快照，不复制主文件、不删 sidecar。保持原文件只读、路径及 SHA256 不变。真实个人数据留在本机，不上传。旧 09 sandbox 协议不兼容 09B，重新迁到新路径，不覆盖已有库。

```powershell
$Py = '.\.venv\Scripts\python.exe'
& $Py tools/migrate_legacy.py dry-run --stock-db 'C:\offline\stock.db' --finance-db 'C:\offline\finance.db' --target 'C:\offline\sandbox-09B.sqlite' --report-file 'C:\offline\dry-run.json'
& $Py tools/migrate_legacy.py migrate --stock-db 'C:\offline\stock.db' --finance-db 'C:\offline\finance.db' --target 'C:\offline\sandbox-09B.sqlite' --report-file 'C:\offline\migration.json' --confirm
```

证券单库可省略 `--finance-db`；纯财务样本需合法的空 stock schema（测试用例中有合成示例），09C 仍未建立通用旧库格式。审查迁移报告实际有效/未迁/待确认记录、持仓、账本现金、账户日期来源、费用/成本、资产负债、财务月及映射。不可解释关键差异和 unknown 不能修改报告清除。修正源事实应留存证据，重新使用新 sandbox；不能把目标投影反抄为旧库证据。

独立来源证据 JSON 格式：顶层 `run_id`、`target_identity`（uuid/generation/schema_version）、`stock_sha256`、`finance_sha256`（无为 null）、`source_reference`、带时区 `valuation_at`、`confirmed_account_mapping_and_dates: true`、`expected`。`expected.accounts` 按名称排序，每项 `name,kind,included,effective_at,balance_fen`，证券另含 `cash_fen,market_value_fen,equity_fen`；`positions` 按账户、代码排序，每项 `account,code,quantity,remaining_cost_fen,remaining_fees_fen`；`totals` 为 `securities_cash_fen,securities_market_value_fen,securities_equity_fen,total_assets_fen,total_liabilities_fen,net_worth_fen`；`counts` 为有效 `trade_executions,finance_transactions,finance_transfers`；另 `trade_fees_fen` 和 `months`（按 period，每项 `period,income_fen,expense_fen,fees_fen,net_fen`，财务月 23 日起）。金额整数分，时间使用带时区 ISO，账户日期精确对应 UTC 投影。证据截止不能早于已迁事实；必须独立核对，未知拒绝。`tests/unit/test_09b_closure.py` 含纯财务声明构造示例，`tests/unit/test_09c_migration.py` 含真实迁移生成的证券/混合报告及独立声明、启动和回滚合成示例；不应把该示例值用在真实账户。

```powershell
& $Py tools/migrate_legacy.py reconcile --target 'C:\offline\sandbox-09B.sqlite' --evidence 'C:\offline\independent-statement.json' --report-file 'C:\offline\accepted.json' --confirm
```

以下仅供独立合成验收项目目录使用，真实正式接管仍需用户另行批准。先明确初始化独立测试正式库，停止所有正式及目标服务、其他写进程和远程访问；确保没有活跃 sidecar。以下命令第一次只打印审批摘要；审查 source/target/report/formal generation，第二次将审核通过的**本次完整 token**原样替换，不使用演示 token。

```powershell
& $Py start.py init-database --database 'C:\isolated-acceptance\official.sqlite' --role formal --confirm
& $Py start.py takeover --target 'C:\offline\sandbox-09B.sqlite' --report 'C:\offline\accepted.json'
# 只有明确批准此次报告和所有写端实际停止后才执行：
& $Py start.py takeover --target 'C:\offline\sandbox-09B.sqlite' --report 'C:\offline\accepted.json' --approval '<review token>' --isolated
& $Py start.py check-database
```

`--isolated` 是操作者实际停止所有写端的确认，不是远程自动隔离。应用锁和 SQLite 探测无法保证不遵守锁的任意外部程序未来不打开文件；必须物理/权限隔离旧写端。保留旧库预接管备份，原 sandbox 仍为 sandbox，不作为第二正式库。任何异常不要删除维护 marker、不手工改 generation 或绑定；保持停机，保留日志/预备份/marker。若 `*.takeover-pending` 存在，显式隔离后运行 `start.py takeover-rollback --confirm --isolated`，检查旧事实及新 generation。成功接管后如需恢复旧库，走正常备份恢复而非删除 marker。硬断电与 Windows 文件系统中断实测仍待本地验证。

## 4. 备份和恢复

独立合成正式库通过接管后，可验证下列操作。正常手动备份可在服务运行时用 SQLite Backup API；接管、升级、恢复必须停机。目标备份目录须新建且不在同步活动数据库的目录内。

```powershell
.\tools\windows.ps1 -Action Backup -Destination 'C:\isolated-acceptance\manual-001'
.\tools\windows.ps1 -Action DailyBackup
.\tools\windows.ps1 -Action ScheduleBackup
& $Py start.py restore-check --source 'C:\isolated-acceptance\manual-001'
# 服务及所有写端已停、验收操作者明确批准后：
& $Py start.py restore --source 'C:\isolated-acceptance\manual-001' --confirm
& $Py start.py check-database
```

自动快照按 UTC 日期去重，保留最近7天及4个较旧周代表；只清理经校验的本库、本 generation 的自动备份目录，不清理手动及恢复前备份。Windows 调度每天本地02:00、错过后尽早执行，当前用户任务；须核查任务计划程序运行结果，关机或无登录环境不保证执行。恢复核对 hash、schema、身份、完整性、外键和同一估值点的数量、现金/净资产；验证旧 generation 拒绝。高级异地加密延期；不要同步活动 SQLite。任何备份都含业务数据，限制访问，不放源码包。

## 5. 日历与真实行情

```powershell
.\tools\windows.ps1 -Action VerifyMarket
```

实际验证 510300、159915、600519 的历史未复权日线与最新报价各一次，保存脚本退出码/日志；网络失败不改为模拟、不无限重试。09 已六项失败，09B/09C 未重复探测；本地须另行复核价格、交易日、时间、来源和真实未复权口径。交易日历保留 XSHG 可信上限 2026-12-31，页面显示起止日期和到期提醒。更新前从 SSE/SZSE 官方休市公告核对，选择有可信新覆盖的 exchange-calendars 版本，在隔离开发副本更新 pyproject/uv.lock，使用以下审查程序并运行日历边界测试；不靠周一至周五猜测。到期时不生成正式策略建议；已有财务事实查询不应被日历挡住，超范围真实成交可保留待核对事实，不伪造有效交易日。

## 6. 公司/家庭双机（独立审核批准后再部署）

正式库唯一存于公司电脑，经 IT 许可；公司仅监听 127.0.0.1，家庭机仅用浏览器，禁止复制/同步活动数据库。先在合成正式库验证 `windows.ps1 -Action Check`、`Start`、`Stop`（优先控制台 Ctrl+C 正常退出，Stop 验证 PID 身份后强制停止）。正式认证使用 `start.py set-password --host 127.0.0.1:9091 --host <实际机器名.tailnet.ts.net>`；密码交互输入，不放命令行或日志，不放宽 Host/Origin，不使用 insecure-cookie 远程。按照 `OPERATIONS_SECURITY.md` 和本机 Tailscale 版本帮助核对私有 Serve 命令 `tailscale serve --bg --https=443 http://127.0.0.1:9091`；`tailscale serve status` 确认确切 HTTPS 地址，家用机访问该地址。验证认证、未授权设备拒绝、断网恢复、会话过期、双机只连接同一 generation；不用 Funnel 或公网映射。尚未实测，不能标 PASS。

提交本地验收记录时标记 PASS/FAIL/NOT RUN、实际命令、退出码、原因，日志与截图去除凭据和真实数据；保留 Git SHA 及锁文件哈希。由产品和独立审查决定后续，不自动发布。


## 7. 09C 新增验收步骤

### 日历更新（仅当有真实完整官方数据时）

先执行 `uv run --locked python tools/verify_calendar.py`，预期输出 XSHG 4.13.1 和 2026-12-31 的可信覆盖及 SHA256。未知版本/历史冲突应失败关闭。真实官方 2027 公告尚未核验时不要提交新日历。获得可信公告后保存原始公告、哈希、链接；独立逐日核对完整扩展会话表（包含所有会话，不是工作日猜测）。隔离分支安装经核实的新版依赖并锁定：按实际核准版本更新 pyproject.toml 和 uv.lock（不使用推测的版本）。准备 `official-calendar-extension.json`，字段 `start`（上一可信终点次日）、`end`（新版可信终点）、`official_sources`（真实交易所 HTTPS 公告链接数组）、`sessions`（独立核实的全部 ISO 交易日数组，升序唯一）。不要使用测试夹具的虚构日期/URL。

```powershell
uv run --locked python tools/verify_calendar.py --statement 'C:\offline\official-calendar-extension.json'
# 上一步成功且独立审查确认来源、范围、锁文件后才显式写入：
uv run --locked python tools/verify_calendar.py --statement 'C:\offline\official-calendar-extension.json' --approve-official
uv run --locked python tools/verify_calendar.py
uv run --locked python -m pytest -q tests/unit/test_09c_calendar.py tests/unit/test_market_resolution.py::test_iss019_coverage_and_expiry
```

来源 URL 字符串校验不能代替人的真实性核查。程序拒绝历史日程变更；若官方纠错，独立审查差异并走专门版本批准，不能改旧指纹骗过验证。应用重启后检查系统页可信区间/版本、最后有效日、到期前提醒、超范围无正式策略建议；财务和真实补录仍能按安全边界工作。更新失败恢复原锁文件和元数据，重装原锁定版本，重新验证并重启。合成测试不部署。

### NDX 与新增页面

在独立合成测试环境运行 `uv run --locked python -m pytest -q tests/unit/test_09c_ndx.py tests/unit/test_09c_migration.py tests/unit/test_09c_history.py tests/unit/test_takeover.py`，预期全部通过；记录实际数量和退出码，不使用真实账本。用已构建前端验证：观察池 T5 的 15%/21.5%/24.5% 目标和实际数量、不预留资金、当前证据按钮、缺证据无按钮；只有真实发生的成交可录入，T5 不填冻结 ATR，部分成交使用不同券商编号；减仓/退出使用真实批次，不触发自动交易。不能以未核实演示行情伪装可信行情。用演示成交验证日期/账户/标的筛选、审计详情、月度投资收益日级表、日期详情和年度财务月；跨账户或切换数据库不混用旧结果。日级收益表标明当前账本重算及版本，不是历史信息时点回放。另验证工作台待核对计数、所选账户风险与计划、持仓代码/名称搜索、账单组合筛选、1/3/6/12月净资产趋势与摘要CSV，切换账户或账本版本后不得保留旧策略证据。

本地验收之前必须由独立审查处理 09C_FUNCTION_MATRIX 的 PARTIAL 项及缺陷等级，并决定是否冻结；不能自行宣告 V1.0 功能全完成。实际构建后可生成运行候选包，但不经批准不部署、不操作用户正式库。所有失败保持原数据库及备份，源码从本轮 bundle 复原；不以旧 dist、移除 vue-tsc、伪造行情或取消校验过关。
