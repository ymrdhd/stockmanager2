# Agent 分轮实施工作流（低 token + 可审查）

## 1. 每轮只交付一个可测目标

唯一迭代顺序见 `DELIVERY_PLAN.md`（00–10）；此处只定义每轮操作，不维护第二张路线表。

同一轮不应同时做无关的大范围 UI 美化和交易模型修改。每轮修改量控制在审查者能看完的范围内；如超过约 15–20 个文件，应拆成更小切片。

## 2. 固定循环

```text
读取 AGENTS.md + STATUS.md
  ↓
docs/INDEX.md 选择需要的专题章节
  ↓
记录 BASE commit + 本轮验收目标
  ↓
定位 → 实现 → 最小测试 → 相关回归
  ↓
更新 STATUS 与受影响契约
  ↓
增量打包 → REVIEW.md + manifest + SHA256
  ↓
用户/独立审查者 APPROVED 或 APPROVED_WITH_ISSUES → 开始下一轮
```

### Agent 最小任务提示词（可直接复制）

```text
你正在维护 Stock Manager 2.0。先读 AGENTS.md、docs/STATUS.md；
通过 docs/INDEX.md 只选择本任务需要的文档章节和代码文件，禁止每轮全仓扫描。
本轮目标：【填写唯一明确目标】；本轮基线 commit：【填写 SHA】。
请先确认业务不变量，做最小完整变更并测试。
完成后更新 docs/STATUS.md，必要时更新唯一契约文档；
必须运行 tools/package_changes.py --base <基线SHA> --summary "..." --tests "..."，
给出审查 ZIP 路径、变更文件、删除文件、测试结果、风险和未完成事项。
不得创建新根目录临时文件，不得打包数据/密钥/缓存/备份。
```

### 当 Agent 需要旧源码参照时

- 优先 `LEGACY_BASELINE.md` 查看准确现行入口；再搜索**指定旧模块**的符号和相关测试，只提取能复现行为的约束。
- 不要将旧 README、历史脚本、已下线 Mixin 列表作为功能需求；用户确认“五资产已下线”。
- 若旧源码与用户现行行为冲突，必须报告，而不是自行“恢复旧功能”。

审批规则：只有产品负责人或独立审查者给出的 APPROVED 或 APPROVED_WITH_ISSUES 允许推进；后者把未完成事项记入 ISSUES，真实测试结果不变，正式发布核心安全/数据验收不豁免。开发 Agent 不决定严重程度或最终优先级。

## 3. 审查包的定义

审查包**只用于代码审查，不用于传输正式数据库或部署**。每轮输出 ZIP 如：

```text
artifacts/reviews/20261123T120102Z_ba12cde_abcdefgh.zip
├── manifest.json     # 起始 commit、生成时间、SHA256、新增/修改/删除/更名路径
├── REVIEW.md         # 本轮目标、测试结果、风险、恢复方式
└── files/
    ├── backend/application/trading/service.py
    ├── tests/integration/test_record_buy.py
    └── docs/STATUS.md
```

**必须对删除和重命名单列清单**；仅打包本轮更改后的文件内容，不通过命名猜测“最新版本”。

### 常用命令

```bash
# 开始工作前（记录一遍即可，不必每一步重复）
BASE=$(git rev-parse HEAD)

# 未提交的本轮修改，或本轮中间已 commit 的修改均以 BASE 为准
python tools/package_changes.py --base "$BASE" \
  --summary "增加交易去重约束和集成测试" \
  --tests "python -m pytest -q tests/integration/test_trading.py: PASS"

# 首次规范包/没有 Git 仓库时可用完整交付模式；后续不能反复 --all
python tools/package_changes.py --all --summary "项目骨架首次交付" --tests "工具测试: PASS"
```

- 包应写入忽略的 `artifacts/reviews/`，**不得放在根目录**或误提交 ZIP。
- 脚本忽略运行期和敏感文件；如果待审文件超过单文件/总大小上限，需要拆分任务，不能绕过过滤。
- 审查者反馈后只修改相关文件，重新用**同一个 BASE**生成更新后的完整本轮增量包，并在 `REVIEW.md` 标明修订；避免“第二个包不包含第一次改动”导致审查不全。

## 4. 上下文节流与压缩

- 首轮可读 2–3 篇相关专题；后续相同任务优先使用最新 `STATUS.md` + `git diff --stat` + 目标函数，不回读未修改长文档。
- 搜索用 `rg -n 'symbol' backend/<domain>/`；不要默认 `rg -n . .`、不要全文打印数据库/日志和旧归档。
- 合并独立检查命令，使用 `--quiet`/指定测试路径，错误时仅看失败片段。
- 记录已裁决的长期问题至 `DECISIONS.md`，任务完成的细节交给 Git；不要无限扩充 `STATUS.md`。
- 只有业务规格变动时才改产品/数据契约；实现细节不必重写整份规格书。

## 5. 每轮审查简报（终端输出模板）

```text
本轮：<目标与结果>
基准：<git SHA>
变更：<新增 X / 修改 Y / 删除 Z；主要文件>
测试：<命令、PASS / FAIL / NOT RUN>
风险：<数据迁移/兼容/安全；无则写“未发现新风险”>
审查包：artifacts/reviews/<准确文件名>.zip

```

## 5. 上下文预算与接续

- 常驻规则目标：AGENTS 不超过 100 行，STATUS 不超过 50 行；INDEX 只做路由。先读这三者必要部分；已在上下文且版本未变不重复读取。
- 每轮先选 1–2 个专题，通常只读约 200–400 行目标代码/测试作为起点；依赖、数据、安全需要时按符号扩展，不以行数上限牺牲正确性。避免大批工具输出；使用 `rg -n`、`git diff --stat`、限定行范围。
- 旧源码只在策略/迁移/兼容验证轮按 LEGACY_BASELINE/STRATEGY_CONTRACT 定位；不在每轮解压和扫描整个旧项目。保存已核验的规则映射与样例，后续按 ID 使用。
- STATUS 只记录当前任务、BASE、验收 ID、阻塞、审查包/批准状态与下一步；历史细节由 Git 和审查 ZIP 保存。子 Agent 只获得任务相关契约与文件，不复制全文。
- 先确认干净工作树；无 Git 先建立规范基线提交。不得把其他人的未提交改动混入本轮。修正保持原 BASE。打包后校验 `python tools/verify_review.py <ZIP>` 并人工复核内容/排除项；增量包不是独立完整项目，审查者需拥有相同基线及此前批准增量。
- 审查包的 --risks/--rollback 应填写实际内容；无风险也应说明验证范围。工具仅做路径过滤与完整性检查，不能证明无凭据或真实财务内容。
