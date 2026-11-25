# 09H 网页安全及账务证据收尾｜开发报告

## 基线
实际使用 09G Bundle 与完整源码 ZIP，BASE `0e7a018ea0d44c87671679b36ad7da4f6bdc313c`，156 个跟踪文件逐字节一致、原工作树干净、fsck PASS。ZIP 有顶层目录，首次路径比较未剥离前缀，纠正后精确比对通过。旧 STATUS 中 HEAD 与实际不同；以真实 Git 为准。另从现有 09G 审计分支恢复 `d6246a460d68a625657347f9a59b5ce5631aad0b` 的代码/测试，保留分支引用，不改写历史；本轮不是替换 09G 对账功能。

## 实际变更
1. 09D-005：同源会话和页面代次防护，401清除会话/业务数据并解除加载，断线清除投影；新数据库身份需明确确认，异步返回需核验，退出立即清除私有数据，保留待核实写入及幂等边界。
2. 恢复09G审计代码：真实 `at/request_id/actor/details_summary` 字段及原因中文显示，原始JSON仍可查。
3. 财务总览：同快照 `amount_evidence`、账户手工余额快照或证券现金基线、持仓报价来源/时间/版本/质量。金额仍使用原投影，不改变收益、现金或净资产公式。未知不填零。
4. 新增真实合成主/子仓证据、行情过期、错误账本版本、未核实信号验证，2%仍为未批准的展示候选；不改 V9/G2/NDX 核心计算。

## 执行结果
| 实际命令/范围 | 结果 |
|---|---|
| 修复前 Workspace setup 独立对照 | 6 PASS / 3 FAIL，原缺陷复现 |
| 修复后三个原缺陷测试及原账户测试 | 9 PASS |
| 财务/审计/退款等专项 | 32 PASS |
| 新增主子仓风险证据 | 1 PASS |
| 最终 Python 受影响集中专项（13个测试文件） | 98 PASS，14.58s |
| 最终 npm test | 55 PASS，0 FAIL |
| tsc --noEmit --strict（client/session/ui） | PASS，限定范围 |
| npm run typecheck | FAIL，vue-tsc Permission denied，127 |
| npm run build | FAIL，前置 typecheck；Vite NOT RUN |
| npm ci | NOT RUN，保留09G历史DNS失败，不反复安装 |
| 真实浏览器、独立全量 Python 回归 | NOT RUN |
| Windows、真实行情、真实旧库、Tailscale、正式部署 | NOT RUN |

各组有重叠，不累加。Python 启动存在非项目 spreadsheet warmup 日志噪声，但 pytest 实际退出0；日志保留。正式 Vue SFC 编译和真实浏览器不能用 setup 测试代替。

## 风险及回退
新身份确认仅限同一页面生命周期，完整刷新仍需服务器认证/写入身份及原 pending 保护；不宣称跨设备防重。金额证据为既有总览的可追溯解释，不是外部银行/券商证明。原策略参考 ZIP 当前未重新取得并校验，未更改纯计算模块。审计分支恢复文件与当前对账页面合并仍需独立审查。若失败，使用本轮 BASE 的隔离 Git 工作树回退应用代码，不覆盖或回退真实数据库。本轮无 schema 变更。详见矩阵与交接。未正式发布，CHANGES_REQUIRED。
