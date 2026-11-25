# 10 本地验收报告（实际执行）

日期：2026-11-25（北京时间）。基线：`09J-web-handoff` HEAD `00c2d143f96b0d51e2dca7b5848f477c08071e73`；工作分支 `10-local-acceptance`。本报告只记录实际命令与结果，不代替独立审查或发布批准。

## 1. 环境

| 项 | 实际值 |
|---|---|
| OS | Windows 11（win32），路径含中文与空格（`C:\Stock Manager 测试\...`） |
| Python | 系统 3.13.14；`.venv` 由 uv 解析为 **3.14.4**（pyproject 要求 >=3.11） |
| uv | 0.11.7 |
| Node / npm | 22.22.2 / 10.9.7（脚本参考 10.9.2，补丁差异） |
| Git | 2.55.0.windows.3 |
| SQLite / 日历 | 3.50.4 / exchange_calendars 4.13.1 |
| 浏览器 | Playwright 1.56.0 + Chromium 141.0.7390.37（独立 venv，不改应用依赖） |

## 2. 基线核验（PASS）

- 外层 SHA256 7/7 匹配（含 source/incremental/documents/bundle/任务书）。
- `git clone --branch 09J-web-handoff` → HEAD `00c2d14...`，工作树干净。
- 源码 ZIP 170 文件与 Git 对象逐字节一致 170/170（Python zipfile + `git show`，无 CRLF 误判）。

## 3. 依赖、类型检查与生产构建（PASS）

| 命令 | 结果 |
|---|---|
| `uv sync --locked` | exit 0，42 包 |
| `npm ci`（frontend） | exit 0，52 包，0 vulnerabilities |
| `npm run typecheck`（`vue-tsc --noEmit`） | 首跑 **FAIL TS2339**（`WorkspacePage.vue` strategyRows 推断类型缺少 `status`/`ndx_aux`）；修复为 `computed<Fact[]>` 后 exit 0 |
| `npm run build`（含 typecheck） | exit 0，vite 7.3.8，23 modules |
| `npm test` | **63 PASS / 0 FAIL**，440 ms |

dist 实际产物与 SHA256：

```
d30034631bce60a31e0e2d404f1564b57c553d088b70e3c66b930be745b9151d  dist/index.html (416 B)
39400eba16ee7711c6e6f70f5e93c882258d772f01b2f84c0ba8127ec7448266  dist/assets/index-BOGUeAUA.css (2306 B)
5f2ae06a94736c2152b0948791bf9d1ac943e10920c89a1d0a892839a4aa7660  dist/assets/index-BhVnMgBz.js (191470 B)
```

`index.html` 引用的 JS 哈希与构建输出一致，无旧 dist 复用。

## 4. Python 回归（先 FAIL 后全部 PASS）

首轮 `pytest -q`：**42 FAIL / 296 PASS**。根因统一为 Windows 只读句柄 `os.fsync` → `OSError: [Errno 9] Bad file descriptor`，影响备份、恢复、接管、schema 升级与每日保留。修复 `durability.fsync_file` 后：

| 范围 | 结果 |
|---|---|
| `test_backup_restore.py` + `test_takeover.py` + `test_database.py` | 38 PASS |
| 其余 9 个受影响文件 | 96 PASS |
| `ruff check` 改动文件 | 无新增问题（余项为既有 I001/F811/UP035） |
| `compileall -q backend tools start.py`、`git diff --check` | PASS |
| 最终 `pytest -q` 全量 | **339 PASS / 0 FAIL**，181.43 s，exit 0 |

## 5. Windows 合成演示与真实浏览器（PASS）

- `windows.ps1 -Action Demo` → `C:\Stock Manager 测试\synthetic-01`，role=sandbox，generation 1，4 笔合成成交。
- `DemoStart -Port 9092`：`/api/v1/health` 200、`/` 200 且返回新 dist、未认证 `/system/status` 401。
- 双标签页旧意图：`verify_09f_browser.py` **PASS**（只新增 1 笔成交、1 条现金、1 个批次）。
- 扩展真实浏览器 **10 项 PASS**：登录/身份、五个页签、普通账单（手工余额不变）、关联退款（回到原支出）、内部转账（净资产不变）、超量卖出（不生效、批次不变、作废留审计）、持仓代码/名称过滤、策略合成数据不可执行、A→B→A 账户隔离、端口预览与沙盒备份 fail-closed 后重新读取恢复。
- 安全 **4 组 PASS**：未认证读 401；缺身份头 428；他库 UUID 409；非法世代 400、错世代 409；缺 CSRF 403；每次均确认无事实写入且请求未提交。真实浏览器子查询失败 → 禁写且交易入口禁用；丢响应保留原 request_id，重试后仅 1 笔有效成交。
- 期初与分红 **2 项 PASS**：期初持仓只入批次并投影到持仓；现金分红只增现金、股数不变、事件可审计。
- 截图与日志：`artifacts/tmp/e2e-*.png`、`artifacts_*.log`（本机，未入包）。

## 6. Windows 正式角色运维（PASS）

合成正式库 `C:\Stock Manager 测试\formal-01`（role=formal，schema 11）：

| 步骤 | 结果 |
|---|---|
| `check-database` | PASS，UUID/role/generation 一致 |
| `backup --destination backups-01` | PASS；同名目标再次备份被拒绝 |
| `restore-check` | PASS：sha256、file_size、identity、manifest、bound_uuid、role、schema、integrity、FK、业务计数、账本/行情版本全部 ok |
| `backup-daily` 同日两次 | PASS，幂等保留 1 份 |
| 第二写者（同库另端口） | 拒绝：`writer lock unavailable`；首实例健康 200 不受影响 |
| 停机 `restore --confirm` | PASS，generation 1 → 2，绑定同步更新 |
| 旧 generation 打开 | 拒绝；generation 2 可打开 |
| 重启服务 + 登录 `/system/status` | 200；`mode=server`、`writable=true`、`generation=2`、`role=formal` |
| 正式模式网页「一键备份」 | PASS，返回已校验备份 |

未注册系统计划任务（避免在用户机器留下持久配置）；`backup-daily` 命令本身已实测，任务计划注册标 NOT RUN。

## 7. 实网行情、名称与日历

| 项 | 实际结果 |
|---|---|
| `windows.ps1 -Action VerifyMarket` | **PASS**，腾讯源，全部 valid、`unadjusted_security` |
| 510300 | 收盘 3.970（2026-11-25）；日线 4.033（11-24）、3.970（11-25） |
| 159915 | 2.199；2.292、2.199 |
| 600519 | 1474.84；1460.00、1474.84 |
| 证券名称 | 修复后写入：沪深300ETF华泰柏瑞、创业板ETF易方达、贵州茅台 |
| `tools/verify_calendar.py` | PASS，XSHG 4.13.1，1990-12-03 至 2026-12-31，指纹 `3a0560be...c5c2f` |
| 2027 官方资料 | **BLOCKED**：上交所「休市安排」页仅有 2026 年公告与安排，无 2027；不猜测、不升级锁文件 |

## 8. 未执行 / 阻断

| 项 | 状态与原因 |
|---|---|
| 真实旧库试迁移与逐项对账 | **BLOCKED**：无用户授权的真实库一致性副本；未触碰任何真实库 |
| Tailscale 私有 HTTPS、公司/家庭双机 | **BLOCKED**：本机未安装 Tailscale，无第二设备与公司许可 |
| 2027 交易日历 | **BLOCKED**：官方资料未发布/未取得 |
| 四项产品候选口径 | 待产品负责人确认，未擅自批准 |
| 独立审查关闭 | 未完成；本报告不代替 |
| 正式接管、部署、发布 | 未授权，未执行 |
| Windows 计划任务注册 | NOT RUN（避免持久系统变更） |
| 拆分/合并等全部公司行为、202 条分页、重连身份重新核对 | 由自动化和既有专项覆盖，本轮未逐项在浏览器重跑（NOT RUN） |

## 9. 偏差与风险

1. npm 10.9.7 与 `windows.ps1` 锁定的 10.9.2 有补丁差异，`-Action Install/Build` 会被守卫拒绝；本轮按任务书手工安装构建，未改守卫。
2. `.venv` 实际 Python 3.14.4（要求 >=3.11）；全部测试通过，但正式部署建议固定到 3.11/3.12 以贴近参考环境。
3. 超量卖出且费用留空时，待核对原因先报 `unknown_fees`；记录不生效、不扣批次，确认/补费用后会按 `insufficient_lots` 拒绝。
4. 沙盒模式一键备份 403 后页面进入禁写，需显式重新读取；安全侧 fail-closed，交互可再评估。
5. 本轮只跑代表性 UI 路径；完整 UI01–09 逐项验收仍需独立审查与现场确认。
