# 部署、备份、接管与发布操作约束

## 1. 运行模式和配置

公司电脑：一个服务实例持有正式写权限，FastAPI 监听 127.0.0.1:9091（可配置）；Vue 已构建，由同一服务提供。家里电脑：Tailscale 客户端 + 浏览器；不启动本地正式后端。公司也用同一 Web 服务。本地模式：显式选择只读快照或可写独立沙盒，界面标识，不能参与正式账本。无服务器采购需求。

新启动器计划支持 server/client/local；保持 `python start.py` 本地启动能力，默认行为必须明确、不误开正式库。真实参数在实现时由 --help 和测试固定，不能把规划参数声称为旧版现有功能。配置示例不含真实密钥；正式路径/UUID/generation/允许 Host/Origin/时区显式指定；服务启动前做文件身份、schema 兼容性及写实例锁校验。只读模式数据库连接真正只读，不启动后台写任务。锁跨进程有效，不能仅在内存；新实例退出/崩溃恢复要测试。

## 2. 私网安全

公司 IT 授权是前置条件；不规避安全管控。Tailscale Serve 私有 HTTPS，不用 Funnel、公网端口映射、SMB 共享活动 SQLite。按实际版本官方文档验证命令；只允许用户设备连接。应用仍须认证；密码哈希/安全会话、登录限频、过期撤销、CSRF、确切 Host/Origin、WS 同源及认证。不要仅凭可伪造代理头或请求来自 loopback 就认证；可信代理路径和身份必须验证。HTTPS cookie Secure/HttpOnly/SameSite；本地 HTTP 会话独立，禁止降级远程。禁永久 token 放 URL 或前端脚本。日志不记录凭据、真实财务明细。测试未认证/错误 Origin/Host/CSRF、伪造代理头、WS 越权、断网重试。

Windows 启动与开机恢复由 07/09 实现：任务计划或成熟服务管理工具，固定用户、工作目录/绝对配置、最小权限、受控重启；禁止每次开机无条件生成新库。配置不休眠需获允许。无法连接时明确离线，只能读带时间的旧缓存（若已提供），禁止本地正式写入。

## 3. 备份与恢复

用 SQLite Backup API 生成一致性快照；备份批次记录 schema、UUID/generation、ledger_revision、文件哈希、时间、校验状态。支持日备份/升级前备份，至少 7 日与 4 周历史版本，独立加密副本；Restic 可选，禁止备份活动数据库文件代替在线快照。密钥单独保管，测试恢复须用独立路径。备份验证 `integrity_check`、`foreign_key_check`、业务不变量和哈希；失败显示告警，不能把“有 ZIP”当成成功。

恢复：验证备份→隔离测试库→核对成交/批次/资金/账户与版本→停止正式写端→保留现库→人工批准切换→冒烟验证。数据库 schema 更新先备份，迁移失败拒绝启动；禁止自动降级 schema 或删除数据。若切换后已新增真实成交，不能简单用切换前备份覆盖；先冻结并导出、对账、保留新事实，明确补录或前向修复方案。

### 3.1 已实现的命令（08）

备份（一次一个目录，发布式写入；失败不会覆盖已有有效备份）：

- `python start.py backup --destination <备份目录> [--database <正式库>]`
- `python start.py backup-status --destination <备份目录>`

备份目录结构固定为 `<destination>/database.sqlite` + `manifest.json`；目录内出现任何其他文件（例如 `auth.json`、`official.json`）即拒绝，认证凭据不会混入普通备份。manifest 记录 `schema_version`、数据库 UUID、generation、`ledger_revision`、`quote_revision`、备份时间、文件大小、SHA256、`source_path_basename`、`integrity_check`/`foreign_key_check` 结果，以及 `contains_personal_financial_data: true` 与中文敏感数据提示——**备份含完整个人金融数据，必须存放在受保护的非公开目录，不入 Git、不入审查包**。

恢复（维护操作，两段式，不提供网页一键恢复）：

- `python start.py restore-check --source <备份目录> [--database <正式库>]`：只读校验文件哈希、数据库身份、角色、schema 兼容性、完整性、外键与关键业务表可读性，并打印各表行数；不写任何东西。
- `python start.py restore --source <备份目录> [--database <正式库>] [--report <json>] --confirm`：先 `restore-check`，再确认当前无活写者（写锁被占用即拒绝，且**不得通过删除 `*.writer.lock` 解除保护**），接着把当前正式库另存为 pre-restore 备份，把校验过的副本复制到临时文件并改 `instance_role='formal'`、generation=max(当前,备份)+1，用 `os.replace` 换入，最后按身份契约发布 `official.json` 绑定并自检。

恢复成功后 generation 递增，旧页面与旧写请求会因 `x-database-generation` 不匹配被 409 拒绝，必须重新加载页面。失败时保留原正式库与原绑定。数据库文件替换与 `official.json` 切换**不是跨文件原子事务**，报告中的 `atomic_note` 会显式声明这一点；`instance_role='backup'` 的旧备份不得直接以正式写角色启动。日常备份与恢复演练保持隔离路径，真实恢复须由用户确认后执行。

## 4. 旧项目数据库迁移（一次性，08）

旧项目有股票与财务两个 SQLite 业务库，迁移工具只读打开源库、默认写入隔离目标库，**不改动旧库、不自动接管**：

- `python -m tools.migrate_legacy probe --stock-db <旧股票库> [--finance-db <旧财务库>]`：只读探测版本、表与字段识别结果、行数摘要。
- `python -m tools.migrate_legacy dry-run --stock-db ... [--finance-db ...] --target <隔离库> [--report-file <json>]`：完整演练，不写目标库。
- `python -m tools.migrate_legacy migrate --stock-db ... [--finance-db ...] --target <隔离库> --confirm [--report-file <json>]`：迁移到隔离库并生成对账报告与异常清单（`--target-role` 只允许 `sandbox`，正式库永不作目标）；有阻断项时退出码 2。
- `python -m tools.migrate_legacy report --target <隔离库>` 或 `--report-file <json>`：重读已迁移库或既有报告。

幂等以源库身份（文件哈希）＋源记录唯一标识映射为准：同一源数据重复迁移不会重复增加成交、现金、账单或资产；源数据变化不会自动覆盖已迁移事实，需要重新演练或人工核对。所有跳过与异常都带原因和数量，不静默丢弃。取旧库一致性副本的做法：停掉旧程序并确认 WAL 已完成安全 checkpoint 后取得独立一致性副本，或用 `sqlite3 <旧库> ".backup <副本>"`；迁移工具拒绝存在非空 WAL 的副本；工具本身以只读方式打开，不会污染源库。**正式接管属于明确维护步骤**，真实旧库对账仍是 V1.0 发布关卡。

## 4. 应急接管与防分叉

UUID 和 generation 只能识别版本，**不能远程阻止失联旧机器写入**。没有第三方仲裁时，接管必须获得旧主机已关停或写权限被可靠隔离的正面证据；仅“连接不上”不足以接管。无法确认就保留只读和非正式草稿，不启用新正式库。接管操作记录旧/新主机、备份哈希、最后有效 revision、批准人、时间；新主机使用提升的 generation，旧主机恢复后先禁用自动启动，核对并重新部署。禁止双向自动合并；手工裁决任何接管后差异。

## 5. 发布与维护证据

发布包与审查包分离：发布包含锁定依赖、构建好的前端、启动/升级/回滚文档，不包含真实 DB、密钥或个人资料。发布候选先在测试库跑完整关卡；真实迁移只在用户确认后执行。保留版本 tag、构建哈希、测试和迁移核对结果。现场双机、IT 授权、真实数据迁移和恢复演练未执行时标记 NOT RUN/BLOCKED，不称正式上线。每次升级前重复备份与预演，定期恢复演练。默认本地服务可用性不承诺 24/7；断电/断网依赖主机恢复。

## 6. 07-remote 实际启动与远程访问步骤

以下命令已在本机实现并实测（Windows 11、Python 3.11、Node 24）；Tailscale 双机接入与公司 IT 授权尚未执行，标记 NOT RUN。

安装（仅公司电脑，一次）：`uv sync --locked` → `cd frontend && npm ci && npm run build && cd ..`。

初始化正式库并设置凭据（一次，凭据按需轮换）：`python start.py init-database --database <绝对路径>\data\official.sqlite --confirm`，再 `python start.py set-password --host 127.0.0.1:9091 --host <机器名>.<tailnet>.ts.net`（密码经 getpass 交互输入，只存 scrypt 哈希到 `data/auth.json`，文件权限 0600，不入包不入库）。**Host 必须写成浏览器实际发出的值**：经 Tailscale Serve 的 HTTPS 走 443，浏览器 Host 头不带端口，因此登记 `<机器名>.<tailnet>.ts.net`，不要写 `<主机名>:9091` 或 `<主机名>:443`；只有确实以非默认端口访问时才登记 `主机名:端口`。不使用通配符放行。

启动正式服务：`python start.py server`（等价 `--port 9091`，实际监听 127.0.0.1:9091，同时提供已构建的 Vue 与 `/api/v1`）。自检：`python start.py check-database`（只读诊断，可在服务运行时执行，不抢写锁）。停止：Ctrl+C 或结束进程；写锁由操作系统释放，**不要删除 `*.writer.lock` 作为常规故障处理**。缺少前端构建产物时启动会明确报错，只有显式 `--allow-missing-web-build` 才允许仅提供 API。

家用机访问：安装 Tailscale 并加入同一 tailnet，在**公司电脑**上执行 `tailscale serve --bg --https=443 http://127.0.0.1:9091`，把 loopback 服务以私网 HTTPS 暴露给 tailnet 内设备；家用机浏览器打开 `https://<机器名>.<tailnet>.ts.net/`（`tailscale serve status` 会打印确切地址，以它为准）。要求：不用 Funnel、不做公网端口映射、不共享 SQLite 文件。`allowed_hosts` 只登记上一步实际出现的主机名，`allowed_origins` 仅在确有跨源需求时追加同源 HTTPS 地址（形如 `https://<机器名>.<tailnet>.ts.net`，不带结尾斜杠）。服务端只信 Host/Origin 头本身并逐请求比对白名单，**不采信 `X-Forwarded-Host`、`X-Forwarded-Proto`、`X-Real-IP` 等可伪造的代理身份头**。HTTPS 下保持 `cookie_secure` 默认 true；本地 HTTP 调试才用 `--insecure-cookie`，且该会话不得用于远程。

Windows 开机启动（任务计划程序，不是无条件建库）：触发器「用户登录时」，操作 `python.exe`，参数 `<安装目录>\start.py server`，起始位置填安装目录，勾选「不管用户是否登录都要运行」之外的默认选项即可；同一数据库只允许一个写实例，第二个实例启动会被文件锁拒绝并退出。

本地沙盒模式（全新安装、无正式绑定时）：`python start.py init-database --database <绝对路径>\data\sandbox.sqlite --role sandbox --confirm`，再 `python start.py local --database <...> --uuid <上一步输出的 UUID> --generation 1 --writable-sandbox`。沙盒有独立 UUID、界面标识为「可写沙盒」，不与正式库合并；打开正式库本体或其同 inode 别名会被拒绝。

发布阻断项（未解决）：公司 IT 授权、Tailscale 双机实测、ISS-019 日历期限、ISS-021 V9/G2、ISS-022 真实行情连通性。

08 审查补充：恢复全程持有正式绑定维护锁与数据库写锁，拒绝活动/残留 WAL/SHM；独立校验副本，原库先备份，切换失败回退。禁止覆盖既有报告路径，发生跨文件切换中断按身份保护拒绝启动，使用 pre-restore 备份人工核对，不删除锁文件。网页仅查询备份及迁移报告，创建/恢复经 CLI。未知现金、市值或旧值不对账为零。
