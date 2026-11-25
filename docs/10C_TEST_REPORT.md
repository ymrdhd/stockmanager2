# 10C 测试报告（实际执行命令、环境与结果）

- 日期：2026-11-25
- BASE：`c646eb937b33345b50b1511f420cd6d00086ebf9`
- 分支：`10c-final-minimal-patch`
- 所有数据库均为临时合成库或全新合成演示库；**未连接、未读取、未修改任何真实数据库**。

## 1. 环境

| 项 | 值 |
|---|---|
| OS | Windows 11（中文路径含空格 `Stock Manager`） |
| Python（项目 venv） | 3.14.4（`uv 0.11.7`，`requires-python >=3.11`） |
| Python（系统） | 3.11.9 |
| Node（本轮安装脚本实测） | 22.22.2（官方 win-x64 便携包，仅解压到 `artifacts/tmp/node22`） |
| npm（随 Node 22.22.2） | 10.9.7 |
| Node（系统默认，用于负向验证） | 24.19.0 / npm 11.17.0 |
| 浏览器 | Chromium 141（Playwright 驱动的本机已安装 chromium-1194） |
| Git | 2.54.0.windows.1 |

## 2. Python

### 2.1 全量回归

命令：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

结果：**346 passed, 48 warnings in 102.33s**　日志：`logs/pytest-full-10c.log`

历史基线为 339 PASS；本轮新增 7 项（`tests/unit/test_10c_restricted_assets.py`）。

### 2.2 本轮新增专项

命令：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_10c_restricted_assets.py -q
```

结果：**7 passed**　覆盖：

1. 普通现金/银行存款 + 完全受限资产 + 证券账户现金与市值 → 受限只影响流动指标，总额与净资产不变，证券现金/市值不重复计算。
2. 未知分类 → 流动指标为 null、逐账户提示、总额仍计入。
3. 流动负债与非流动负债 → 仍按已批准现行流动负债口径；负债拒绝设置资产受限标记（服务层与输入层双重拒绝）。
4. 分类修改 → 审计事件 1 条、事件表 `initial` + `changed`、幂等重放、陈旧 `expected_ledger_revision` 拒绝、回溯生效时间拒绝、历史时点仍用旧证据。
5. 数据库重开 → 分类与金额保持。
6. `0011 → 0012` 真实 schema 升级 → 既有账户全部 `unknown`（0 条分类事件）、余额不变、外键/完整性检查通过、升级前备份仍为 schema 11。**验证「迁移不得无依据认定为不受限」**。
7. 备份 → 恢复 → 重开：受限分类、金额、流动指标全部保持。

### 2.3 受影响既有专项

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_finance.py tests/unit/test_09f_features.py tests/unit/test_database.py tests/unit/test_database_upgrade.py -q
```

结果：**53 passed**

### 2.4 数据库升级/备份/恢复合并专项

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_10c_restricted_assets.py tests/unit/test_database_upgrade.py tests/unit/test_backup_restore.py tests/unit/test_finance.py -q
```

结果：**50 passed**　日志：`logs/pytest-db-specialty-10c.log`

## 3. 前端

| 命令 | 结果 | 日志 |
|---|---|---|
| `npm test` | **63 pass / 0 fail**（317.6 ms） | `logs/npm-test-10c.log` |
| `npm run typecheck`（`vue-tsc --noEmit`） | **exit 0** | `logs/typecheck-10c.log` |
| `npm run build`（typecheck + `vite build`） | **exit 0**，23 modules，`dist/index.html` 0.42 kB、CSS 2.31 kB、JS 194.16 kB | `logs/build-10c.log` |

前端单测中唯一改动：`tests/client.test.ts` 的 reason 文案断言改为新文案「流动性分类待确认（受限/不受限），暂不计入流动资产」。

## 4. 真实浏览器（受限资产页面专项）

合成演示库（`tools/create_demo.py` 新建，端口 9098，loopback，`--no-market-refresh`），Chromium 141。

命令（密码经环境变量注入，不入日志）：

```powershell
$env:DEMO_REVIEW_PASSWORD = '<一次性演示口令>'
artifacts\tmp\pwvenv\Scripts\python.exe artifacts\tmp\10c\verify_browser_10c.py
```

结果：**9/9 PASS，0 failure**　日志：`logs/browser-10c.log`，截图：`logs/browser-10c-restricted.png`

| # | 断言 | 结果 |
|---|---|---|
| 1 | 登录 | PASS |
| 2 | 财务账户表显示三态「流动性分类」列 | PASS |
| 3 | 演示初始全为不受限，流动资产 = 总资产 | PASS（total=102,153,500 分） |
| 4 | 通过真实 UI 提交「受限」分类 | PASS |
| 5 | 受限金额离开流动指标、仍留在总额 | PASS（restricted=1,990,000；liquid=100,163,500） |
| 6 | 刷新后页面显示受限分类文案 | PASS |
| 7 | 变更前的历史时点仍用旧分类证据 | PASS |
| 8 | 审计事件 `liquidity_changed` 记录并在页面可见 | PASS |
| 9 | 新账户默认未知 → 提示「待确认」且流动指标被扣留 | PASS |

## 5. Windows 标准安装脚本

### 5.1 正向：Node 22.22.2 / npm 10.9.7

```powershell
$env:PATH = '<repo>\artifacts\tmp\node22;' + $env:PATH
powershell -NoProfile -ExecutionPolicy Bypass -File tools\windows.ps1 -Action Install
```

结果：**exit 0**　日志：`logs/windows-install-10c.log`

实际步骤与输出摘要：

- `uv sync --locked`：Resolved 42 packages / Checked 40 packages，exit 0
- `npm ci`：added 52 packages, audited 53，0 vulnerabilities
- `vue-tsc --noEmit`：无错误
- `vite build`：exit 0，`dist/index.html` 生成

**即：不再需要手工绕过 npm 守卫。**

### 5.2 负向：npm 11.17.0

用 PATH 前置的 `npm.cmd` 模拟 npm 11（不改系统安装），`-Action Build`：

结果：**exit 1**，`Use npm 10.9.x shipped with Node.js 22.13+ (verified 10.9.7); found 11.17.0`　日志：`logs/windows-npm11-reject-10c.log`

### 5.3 负向：Node 24.19.0

系统默认 Node 24，`-Action Build`：

结果：**exit 1**，`Use Node.js >=22.13.0 <23 (verified 22.22.2)`　日志：`logs/windows-node24-reject-10c.log`

### 5.4 Python 守卫

`Assert-Python` 读取 `.venv` 解释器版本并要求 ≥3.11；当前 venv 为 3.14.4，`-Action Install` 通过。系统 Python 3.11.9 亦满足 `requires-python >=3.11`。

## 6. 静态检查（仅本轮新增/修改文件）

- `ruff check migrations/versions/0012_liquidity_status.py`：**通过**
- `ruff check tests/unit/test_10c_restricted_assets.py`：仅与仓库既有测试相同的风格项（`I001` 导入排序；`F811` fixture 形参与导入同名——全仓测试通用写法），无逻辑类告警
- `mypy backend/application/finance.py backend/api/finance.py`：无本轮新增类型错误（输出中 49 项均为仓库既有 `migration.py`/`finance.py` 未注解等历史遗留，`ISS-003` 仍 OPEN）

## 7. 未运行 / 阻塞（如实记录）

| 项目 | 状态 | 原因 |
|---|---|---|
| 2027 官方交易日历 | BLOCKED | 上交所仅发布 2026 年休市安排；不猜测、不改锁文件 |
| 真实旧库只读试迁移与逐项对账 | BLOCKED | 无用户授权的真实库一致性副本 |
| Tailscale 私有 HTTPS 双机 | BLOCKED | 本机未安装、无第二设备与公司许可 |
| 与修改无关的历史浏览器场景 | NOT RUN | 按任务书只做受影响专项，避免重复 |
| 正式接管 / 部署 / 发布 | NOT RUN | 未获授权 |
