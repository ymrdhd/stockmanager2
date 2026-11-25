# 09B-release-blocker-closure 开发报告

结论：CHANGES_REQUIRED；RC1 运行包未生成，未批准正式部署。实际 BASE `0fc2309a875c28cb0bb4b46b3d324029f0cdd084` 是 09 完整源码检查点：09 12 个增量文件校验与完整源码一致，非增量源码与 08 BASE `7271eac6801b22a980d06de9e2a7778615b4c226` 一致。原始策略 SHA256 `1edc58feabbe2e32cda9bfb2bd34f205f4cd3c4b0b3e008e5910002b6af6cc06`。只在临时合成库测试，没有访问用户正式库。

## 变更与边界

- 接管 CLI：审批 token 绑定报告、目标文件、旧正式库哈希/身份/代；验证持久化迁移报告、源哈希、schema、完整性、外键和版本；应用写锁及 SQLite 停机探测、验证备份、维护 marker、切换绑定与 generation、失败关闭及显式回滚。`--isolated` 必须实际停止所有写端；不可证明外部不遵守锁的进程或 Windows 硬断电安全。未使用用户库。
- 迁移：09B 协议、未知现金及费用与日期来源、普通财务转账已包含余额证据、独立同截止声明逐项对账、待确认/差异保守阻断。纯财务合成样本完成对账至接管；证券与混合代表夹具重建及阻断通过，但未取得证券/混合无差异端到端验收。未知/未验证事实不得用目标数字反填。详见本地手册。
- 策略：写事务内复核实际账本版本、现金、核心槽位、其他活跃容量计划、已消费信号；信号/计划原子提交。已有计划是应用层容量占用，不是实际资金划拨；建议不等同计划，真实成交仍按唯一 TradeService 幂等记录。NDX 接入原始辅助规则、可信实际仓位与现金缓冲；缺证据降级，辅助输出仍不可执行，完整加/减/退出黄金周期待验收。历史配置/研究回放按用户授权延期，证据不足不可执行。未改 ATR/G2 核心及动态 3/10 日冷却。
- 备份：沿用一致性 Backup API、恢复验证；每日自动快照、最近7天及4个较旧周代表，手动/恢复备份不删。Windows 调度、强制退出和真实故障恢复待验证。
- 日历 UI 展示可信起止/到期提示；覆盖至 2026-12-31，不虚构 2027。未重新联网测试。
- Windows 操作脚本、独立 demo 与验收手册；合成 demo 生成实际成功，未经过生产页面验收。独立完整源码、不包含 dist/依赖/数据库/凭据。

## 实际执行（Linux Python 3.13.5、Node v22.16.0、npm 10.9.2；不是完整锁定 Windows 环境）

| 命令（根目录） | 结果/日志 |
|---|---|
| `python -m pytest -q tests/unit/test_takeover.py` | 6 PASS；09B-takeover.log |
| `python -m pytest -q tests/unit/test_migration.py tests/unit/test_takeover.py` | 20 PASS；09B-migration-takeover.log |
| `python -m pytest -q tests/unit/test_09b_closure.py tests/unit/test_strategy_service.py tests/unit/test_rc_decision_time.py tests/unit/test_backup_restore.py tests/unit/test_migration.py tests/unit/test_takeover.py` | 69 PASS；09B-final-tests.log；在最终原子信号/计划调整前 |
| `python -m pytest -q tests/unit/test_strategy_service.py tests/unit/test_rc_decision_time.py tests/unit/test_09b_closure.py::test_transaction_capacity_race tests/unit/test_09b_closure.py::test_ndx_cash_buffer_reference tests/unit/test_09b_closure.py::test_real_trade_concurrent_request_id tests/unit/test_security.py tests/unit/test_database.py tests/unit/test_market_resolution.py::test_iss019_coverage_and_expiry` | 最终调整后 63 PASS；09B-atomic-safety.log |
| `python tools/create_demo.py --target /tmp/sm09b-demo-1795583044` | PASS；09B-demo-final.log，3 账户、4 成交、3 未平批次、1 账单、1 转账、4 余额事实、3 报价、880 策略 bars，integrity_check ok、FK 无异常、sandbox gen1 |
| `npm ci` / typecheck / production build | 09B NOT RUN（按授权不再重复受限依赖安装）；09 历史 `npm ci --offline --no-audit --no-fund` FAIL ENOTCACHED vue-tsc-3.3.14；09 历史 `npm run build` FAIL vue-tsc not found，Vite 未运行 |
| `python tools/verify_market_live.py` | 09B NOT RUN（09 历史 6 FAIL，transport/cooldown）；本地验收 |
| 真实浏览器、Windows、Tailscale、真实旧库、全量独立回归 | NOT RUN |

测试组重叠，不能加总；最初扩展失败包括 NDX 结果序列化与 demo 余额前置 ID/指数别名，修复后上述集中专项及最终 demo 通过。保留失败日志供审查。不声称所有 S0/S1 完成。保留历史 ISSUE 编号，见 ISSUES 09B 映射。回退代码使用 09B BASE；数据库须使用经校验的停机备份/明确恢复，不能用 Git 恢复数据库。
