# Stock Manager 2.0

当前阶段：`06-ui`（部分完成，待独立审查）。已实现后端交易、持仓收益、行情、财务及五页 Vue 业务源码（Alembic 0007）；依赖锁、完整 Vue 类型检查、构建及浏览器验收仍因环境阻塞。V9/G2 正式策略、真实行情、数量公司行为及旧库迁移未完成。

## 环境及依赖安装

- Python 3.11+（SQLite ≥ 3.37）、uv（测试环境 uv 0.10.0）。
- Node.js 22.13+（22.x）、npm 10+、Git。
- 除特别说明外，以下命令从仓库根目录执行，优先兼容 Windows PowerShell。

**当前锁文件尚未生成**：需要在可访问 PyPI 和 npm registry 的开发环境先完成解析。这两步未通过前不要将工程视为可复现安装，也不要手工拼装锁文件。

```powershell
uv lock
uv sync --locked
cd frontend
npm install --package-lock-only
npm ci
cd ..
```

生成并审查 `uv.lock`、`frontend/package-lock.json` 后，正常安装只需 `uv sync --locked` 和在 `frontend/` 中运行 `npm ci`。依赖锁文件仍需在正式发布前补齐。

## 启动

后端：先用 `python start.py --help` 查看入口。正式路径与 UUID/generation 固定保存在忽略的 `data/official.json`；初始化与显式接入后不得由普通参数重选。相对路径不允许。以下仅为 PowerShell 运维示例，不要对现有库执行初始化：

```powershell
$db = Join-Path (Get-Location) 'data/stock_manager.sqlite'
# 仅首次新库，迁移至 0007 并建立固定绑定
python start.py init-database --database "$db" --confirm
# 默认读取绑定；可选 --database/--uuid/--generation 仅用于核对，不可覆盖绑定
python start.py check-database
python start.py server --port 8000
```

已有 0001/0002/0003/0004/0005/0006 正式库且尚未绑定时，须显式使用 `adopt-database --database <绝对路径> --uuid <已核验UUID> --generation <已核验generation> --confirm`；不会接入 backup 或 sandbox。随后停止写服务，执行 `python start.py upgrade-database --destination <新的绝对备份目录> --confirm`，先备份再事务升级；不自动升级或降级。已是最新版本时不重复升级。若绑定缺失/损坏，启动、检查及维护安全拒绝；不得手工猜测或覆盖绑定。绑定及锁文件应与正式数据库一起受保护，初始化若在绑定发布时失败，保留新库供人工核验后显式接入，不删除已有数据。禁止手工移动/替换活动数据库、绑定或 `.writer.lock` 文件。

默认路径锚定项目根目录，默认服务端口 9091；开发前端代理使用 8000，因此示例指定端口。`server` 持有唯一写锁，退出或崩溃由 OS 释放。`local --database <绝对快照或沙盒路径> --uuid <该库UUID> --generation <该库generation>` 默认真实只读；显式 `init-database --role sandbox --database <独立绝对路径> --confirm` 后可加 `--writable-sandbox`。沙盒不会更改正式绑定。`client` 不开启本地数据库，远程接入未实现。

备份：`python start.py backup --destination <新的绝对备份目录>`（无需停止正式服务）。备份 UUID/generation/schema 与事实保留，副本 role=backup，不能直接作为正式写库或可写沙盒；拒绝覆盖。完整恢复及业务对账尚未实现。

交易 API（仅已验证数据库模式，详细请求字段见本机 `/docs`）：`POST/GET /api/v1/accounts`、`/instruments`、`/trades`、`/cash`；`GET /trades/{id}`、`POST /trades/{id}/corrections`、`POST /trades/{id}/confirm`、`GET /lots`、`/allocations`、`/pending`（均在 `/api/v1`）。写入使用唯一 request_id；价格和 ATR 为十进制字符串，数量为整股字符串（兼容整数输入），费用为整数分或明确 null。查询列表默认 100、最多 500 条，`offset` 与可选 `ledger_revision` 用于分页时核验账本版本。未分配、费用不明或非正常交易日期/时段的真实成交标为 pending，不影响有效持仓和现金；补齐事实后确认并按时间重放，复杂历史冲突安全拒绝。`POST /api/v1/trades/{id}/void` 可审计作废。现金期初、真实出入金与余额调整分开记录；无可靠期初或存在历史缺口时标记未核实。

收益 API：`GET /api/v1/portfolio/{account_id}?as_of=<带时区ISO时间>&previous_trading_day=<实际前一交易日>` 返回该时点持仓/批次、证券权益、日收益、已实现/未实现/累计收益和质量；历史日收益通过对应日 as_of 查询。`POST /api/v1/quotes`、`GET /api/v1/quotes/{instrument_id}?as_of=...`、`POST /api/v1/opening-positions`、`POST /api/v1/company-events` 为明确输入及查询入口，详见本机 `/docs`。仅确认现金分红入账；未支持的数量公司行为录入缺口，不产生虚构买卖或收益。实际前一交易日由后端日历确定；`previous_trading_day` 是可选兼容输入，不匹配时拒绝。休市自然日收益与最近完成交易日收益分开。`quote_revision` 可固定报价版本（账本仍使用当前有效事实）；报价查询的 `available_at` 可额外限制历史时点可获得的数据，旧报价获取时间未知时不纳入严格回放。`POST /api/v1/quotes/resolutions` 对已审阅的收盘冲突显式确认正确报价（selected_quote_id、expected_quote_revision、request_id、reason）；旧证据保留，历史严格可获得性不提前使用更正。

财务 API（`/api/v1`）：复用 `POST/GET /accounts`，新账户（含证券）传 `kind`、经核实的 `effective_at`、`included`，`PATCH /accounts/{id}` 使用 `expected_ledger_revision` 更名或调整状态；关闭账户仍计入已有余额。`POST/GET /finance/balances` 使用 `after_fen`、`expected_balance_id`（首次显式 null）、实际时间与原因。`/finance/bills`、`/finance/categories`、`/finance/transfers` 提供创建、查询，账单和转账各有 `/{id}/corrections`、`/{id}/void`。转账手工端必须提交 `from_included`/`to_included` 及对应 `*_balance_id`，证券端留空；已包含不调整，未包含原子更新，普通更正拒绝复杂历史；人工核对 `POST /finance/transfers/{id}/reconciliation` 可提交最新手工余额 ID、实际余额与核实时间、核实原因及 `confirmed=true`，作废或替换正确金额（相同两端与原日期）。`POST /accounts/{id}/scope` 与 `/effective-date` 分别记录统计范围和可信开户时间。独立 `/cash` 不接受新内部 `transfer_id`。`GET /finance/overview?as_of=...` 返回净资产、证券权益及财务月；`GET /finance/month?period=YYYY-MM-23` 与 `/finance/year/{year}` 查询分期明细，支持 as_of。`POST /cash/{id}/corrections`、`/cash/{id}/void`、`POST /company-events/{id}/corrections`、`/company-events/{id}/void` 为非成交现金及分红更正入口；`GET /company-events` 查询历史。金额整数分，负债用非负金额且从净资产扣减。所有写操作使用唯一 request_id，具体字段见 `/docs`。

行情 API：`GET /api/v1/market/status`、`GET /api/v1/market/prices/{instrument_id}`、`POST /api/v1/market/refresh?instrument_id=<可选ID>`。正式服务默认 20 秒盘中检查；`--market-refresh-seconds 10-300` 可配置，`--no-market-refresh` 可关闭后台网络刷新。休市不做持续网络轮询，收盘后补取已完成日线；只读本地模式禁止刷新写入。当前适配器仅支持可识别代码范围内沪深股票/ETF 的实际未复权价格，来源为东方财富公开接口（真实接口在开发环境尚未验证）；其他市场/代理/指数数据不得写入证券估值表。日历使用 exchange-calendars 4.13.1 的 XSHG 预计算日历（沪深共用内地交易日，覆盖至 2026-12-31）；`/market/status` 提供日历覆盖及60天到期提示，正式发布前核对交易所公告并更新可信日历。超出覆盖或缺失必要行情返回不可用。`GET /api/v1/strategy/evidence/{account_id}` 仅展示实际批次及冻结证据，明确策略引擎不可用，不返回正式建议。

仅健康检查的开发模式（不连接数据库、无任何业务服务）：

```powershell
python -m uvicorn backend.main:create_app --factory --host 127.0.0.1 --port 8000 --workers 1
```

健康检查：`http://127.0.0.1:8000/api/v1/health`，不表示业务已就绪。

另一个终端启动前端（从根目录）：

```powershell
cd frontend
npm run dev
```

打开 `http://127.0.0.1:5173`。Vite 仅在本机代理 `/api/v1` 到本机后端；不配置公网、远程代理或正式部署。导航为总览、投资、交易、财务、系统。模式及写权限由 `/api/v1/system/status` 返回；策略尚不可用，维护命令在系统页说明。后端停止后应明确提示连接失败，不切换数据库。当前 Vue/Vite 依赖安装、完整页面构建与真实浏览器操作尚未验收。

## 测试及构建

从根目录执行：

```powershell
uv run --locked python -m pytest -q
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy backend
cd frontend
npm test
npm run typecheck
npm run build
```

前端 `npm test` 用 Node.js 内置测试运行器验证健康检查、整数金额、表单载荷、防重复及网络未知结果重试。真实 HTTP 检查：在前端目录、后端运行时执行 `npm run test:live -- connected`；停止后端后执行 `npm run test:live -- unavailable`。这两条命令不替代 Vue 页面浏览器验收。

## 增量审查

以开工时记录的完整 BASE 为准（见 `docs/STATUS.md`），收尾先复核变更，再生成增量包。PowerShell 示例：

```powershell
cd .. # 如果当前位于 frontend/
$base = "298d9d5fd21be3bd6efb7f159fcee4dafce90c61"
python tools/package_changes.py --base "$base" --summary "实际变更摘要" --tests "实际测试命令及结果" --risks "风险与阻塞事项" --rollback "安全回退方案"
```

将命令输出的实际 ZIP 路径传给 `python tools/verify_review.py` 校验。ZIP 位于 `artifacts/reviews/`，不会进入 Git。收到审查意见后仍用原 BASE 打包全部本轮变化；仅在独立审查 APPROVED 或 APPROVED_WITH_ISSUES 后推进。
