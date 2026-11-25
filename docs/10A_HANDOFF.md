# 10A 交接（四项口径正式批准 + 最终交付）

## 基线与授权

- BASE：`26c981551cec7c3b8188b89664e5a5cbedde068f`（`10-local-acceptance`）。
- 本轮分支：`10a-final-delivery`。
- 批准来源：产品负责人 2026-11-25 正式批准四项业务口径，权威文本见 `DECISIONS.md` ADR-017～ADR-020。
- **批准的是产品口径，不是部署授权。** 未操作真实数据库、未正式接管、未部署、未发布。

## 本轮改了什么

代码（1 处差异，最小修复）：

1. `backend/application/portfolio.py:1313`：当日投资收益率由 `.6f` 改为 `.2f`。
2. `backend/application/finance.py:701`：财务总览证券合计收益率同口径改为 `.2f`。
3. `tests/unit/test_09f_features.py:44-45`：断言由 `'6.666667'` 改为 `'6.67'`，并新增一条以同一格式化表达式的交叉断言。

其余三项口径（退款冲减原支出期、流动净资产、接近止损 2% 预警）经定向核对与实现一致，未改业务代码。

文档：

- `DECISIONS.md`：新增 ADR-017～ADR-020（含影响模块与回退方式）。
- `PRODUCT_SPEC.md`：2.1 当日收益率、2.2 接近止损预警、2.4 退款统计与流动净资产。
- `INTERFACE_CONTRACT.md`：新增「四项已批准业务口径的接口约束」节。
- `09F_PRODUCT_CANDIDATES.md`：候选 → 已正式批准，并逐条标注落地状态。
- `ISSUES.md`：09D-009/011/012/008 状态更新；新增 10A 定向核对表与 10A-001～10A-003 观察。
- `STATUS.md`、`CHANGELOG.md`、`INDEX.md`：状态与入口更新。
- 新增 `10A_FINAL_REVIEW.md`（审查材料）、本文件（交接）。

未改：数据库 schema、核心 V9/G2 公式、ATR、动态 3/10 日冷却、保护价与批次逻辑、产品阈值语义、依赖锁。

## 已验证（实际）

- `pytest tests/unit/test_09f_features.py tests/unit/test_finance.py tests/unit/test_pnl.py -q` → **44 passed, 5 warnings**（17.31s）。
- 未重跑全量 Python 回归（按开发/审查分工由独立审查者执行）；前端源码零改动，未重复 typecheck/build/单测。

## 仍未完成（需用户/外部输入，不因口径批准而消除）

1. **2027 交易日历**：上交所官方页当前仅发布 2026 年休市安排；未猜测、未改锁文件。
2. **真实旧库只读试迁移与逐项对账**：需要用户授权并只使用 SQLite Backup API 一致性副本。
3. **Tailscale 私有 HTTPS 与双电脑**：本机未安装、无第二设备与公司许可；禁止 Funnel/公网。
4. **独立审查关闭**：10-001、10-002、10-003 三项 FIXED 待关闭裁决；完整回归与 ISSUES 裁决由独立审查者给出。
5. **正式接管/部署/发布**：未获授权，未执行。

## 交付包构成（最终完整 ZIP）

最终 ZIP 内包含下列内容，全部位于同一层目录，另附 `SHA256SUMS_10A.txt` 与本 README 的副本：

| 文件 | 内容 |
|---|---|
| `10A-source.zip` | 完整源码（与 Git 对象逐字节一致，含本轮提交） |
| `10A-incremental-review.zip` | 相对 BASE 的本轮增量 + `manifest.json` + `REVIEW.md`（已 `verify_review` 校验） |
| `Stock_Manager_10A_recovery.bundle` | 全历史 Git bundle（所有分支） |
| `10A-documents-and-logs.zip` | 最新 MD 文档、实际测试日志、环境清单 |
| `SHA256SUMS_10A.txt` | 上述文件 SHA256 |
| `README_10A_delivery.md` | 交付说明与复原顺序 |

## 复原顺序

```powershell
# 1 校验完整性
Get-Content .\SHA256SUMS_10A.txt | ForEach-Object {
  $p = $_ -split '\s+', 2
  if ((Get-FileHash -LiteralPath $p[1] -Algorithm SHA256).Hash.ToLowerInvariant() -ne $p[0]) { throw "mismatch: $($p[1])" }
}
# 2 从 bundle 重新克隆并切换到本轮分支
git clone --branch 10a-final-delivery .\Stock_Manager_10A_recovery.bundle Stock_Manager_V1
# 3 与 source.zip 对账（可选，逐文件 SHA256）
```

随后按 `LOCAL_AGENT_START.md` 与 `docs/10A_HANDOFF.md` 继续本地环境验收。本包不是运行包，也不构成正式发布或真实数据库接管授权。

## 建议下一步

1. 独立审查：完整 Python 回归、10-001/10-002/10-003 关闭裁决、ADR-018 精度修复是否接受。
2. 产品决定 10A-001（受限资产是否需要独立 schema 标记）与 10A-003（`liquidity_basis` 文案）。
3. 授权后：真实旧库一致性副本 dry-run → 独立对账 → 试点迁移。
4. 2027 官方公告发布后：更新 `exchange-calendars` 与 `uv.lock`，跑日历审查与边界测试。
5. 安装 Tailscale 并按 `OPERATIONS_SECURITY.md` 验收 Host/Origin/CSRF/会话。
6. 独立审查通过且用户明确授权后，再申请正式接管与 V1.0 发布。
