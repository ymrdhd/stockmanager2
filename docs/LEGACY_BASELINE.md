# 旧项目可达功能基线（迁移参考，非新项目需求来源）

**核对来源**：用户上传 `stock-1_20261122_183448.zip`；以默认 `start.py → web_app.launch → web_app.server` 和可导航的 `web_app/static/index.html`/`app.js`、对应 API 及 `subroutine/finance/web/` 页面为依据。压缩包含 Vue 源码但无编译好的 Vue `dist`，原样启动会落到静态页。**这不是运行验收**：未提供真实业务数据库。

| 旧项目当前默认 Web 功能 | 新版处理 | 源码入口例证 |
|---|---|---|
| 股票总览、持仓/当日收益、买点距离、月度盈亏 | 保留并整理到“总览/投资” | `web_app/server.py` 的 `/api/stocks/overview`, `/api/stocks/proximity`, `/api/stocks/capital` |
| 持仓、批次、止损/加仓决策 | 保留；同一后端读统一成交/批次 | `web_app/static/app.js` 持仓详情与策略展示 |
| Web 手工成交、V9/G2 批次关联、历史查询 | 保留，重写统一写入服务 | `web_app/trades.py`、`/api/stocks/manual-trade`, `/api/stocks/strategy-lots` |
| V9/G2 策略观察池、收盘更新 | 保留现行规则和严格质量闸门 | `subroutine/index_strategy/`, `/api/stocks/strategy`, `/api/stocks/strategy-refresh` |
| Fed 利率、FOMC 概率、CPI/实际利率 | 保留“投资·市场研究” | `web_app/fed_observer.py`, `/api/fed/observer` |
| Finance 净资产、余额、收支、历史财务月 | 保留“财务”及历史报表 | `subroutine/finance/web_dashboard.py`, `web_editor.py`, `period_spending.py` |
| 系统运行状态、网页设置 | 保留精简的“设置” | `/api/health`, `/api/settings/web` |
| 可进入的 `/finance/`、`/finance/history/` | 纳入财务页面迁移清单 | `web_app/server.py` 对应路由 |

### 显式排除

- 五资产配置：**用户已确认下线**；尽管旧仓库历史脚本、Mixin 和文档有残留，也不算新需求。
- 历史 Tkinter/Qt Mixin、无实际当前 Web 入口的批量研究脚本与旧参数系统：**不默认迁移**。如用户明确要求恢复，另立需求与验收。
- 自动券商下单、双主离线交易合并、内置 AI 投资顾问：当前不是功能，不应臆造。

### 不得丢失的业务语义

1. `subroutine/finance/period_spending.py` 财务月 23 日至次月 22 日；财务月不等于自然月。
2. 当前手工银行余额与手工收支账单是**分开维护的**；历史迁移时不可把旧账单直接重新加减余额。
3. Web 股票写入旧代码受 `STOCK_WEB_WRITES` 控制、SQLite 限制且通过 `request_id` 防重；正式新版统一 TradeService。
4. 当前 `web_app/capital.py` 有当日收益增量结算逻辑；新版改为可重算收益而不重复写回本金事实。
5. V9/G2 包含主仓/加仓子仓及有效数据闸门；只能使用可核实的旧测试和现行计算输出确认兼容性，不能从旧文档复制未经验证阈值。

未提供旧正式数据库时，不能宣称历史成交、资产统计或策略迁移已通过实盘数据核对。
