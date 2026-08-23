# HZCU Campus Agent

面向浙大城市学院学生的模型原生校园认知 Agent。

本项目不是传统 FAQ、智能客服或“校园知识库套壳”。产品目标是让 Agent 能够结合学生画像、对话上下文和校园当前状态，理解模糊表达与隐含需求，自主调查官方来源，形成有依据、可执行、因人而异的回答。

## 当前状态

- 产品与架构基线已完成
- 阶段 1 可运行纵向骨架已接通
- 阶段 2“校园感知与时间版本”工程实现与当前环境验收已完成
- 阶段 0 至阶段 5 已完成
- 阶段 6 已完成：真实模型、本地镜像、真实 Edge 全流程和重启恢复已验证
- 实际目标机器部署已按用户决定延期，不影响当前本地试用与演示
- 初始入口：独立 Web 应用
- 初始用户：新生及往届学生
- 当前数据范围：公开官方信息 + 已批准的 Campus 本地镜像 + 可选 CA/VPN 实时只读查询
- 明确排除：个人课表、成绩、学分和任何申请代办或业务写操作

当前代码已经包含：

- 模型原生语义感知、动态规划和回答组合接口；
- OpenAI Responses API 模型适配器与透明的无密钥演示适配器；
- 受控 HZCU 官网实时检索、官方域名白名单、正文读取和证据记录；
- 49 个聚合来源、142 个登记入口、条件请求、内容哈希、不可变版本和
  gzip 原文快照；
- HTML、GB2312 旧站、PDF 与字段白名单 JSON API 解析；
- SQLite FTS5 trigram 当前版本检索（单查询 BM25、标题加权、服务端权限过滤）；
- 48 条多领域真实镜像回归曾全部命中预期材料；
- 保留可重建的语义分块、向量和结构化校园实体供管理与后续评测；
- 实时证据安全回写、全历史索引、版本查询与结构化差异；
- 可独立部署的周期同步 Worker 和只读来源状态 API；
- 来源健康、新鲜度告警和桌面/移动端版本工作台；
- 会话、任务、回答与证据持久化；
- SSE 实时任务进度；
- 桌面端与移动端独立 Web 界面、公开来源观测与版本透明入口；
- 可选真实 CAS 登录和校内网络直连通知查询；
- 后端单元/端到端测试和前端生产构建验证。
- 180 天匿名设备主体、可选 CA 显式合并和严格主体隔离；
- 历史会话、画像确认、手动待办、回答反馈、取消/重试/实时复核；
- CA 管理员只读运营台和 SQLite 单机试用部署包；
- 回答非法控制字符清理与服务重启任务恢复。

演示模式会真实运行检索、证据链、任务和界面，但不会伪装成大模型完成语义推理。配置模型 API 后才启用多假设理解、动态调查规划与个性化回答。

新增功能与开发范围只由用户决定。评测记录、历史 Spec 和开发者判断不能自行生成
新的产品需求或阶段门槛。

## 核心原则

1. 模型负责理解与推理，工具负责接触现实。
2. 知识库是长期记忆和缓存，不是 Agent 的大脑。
3. 主模型始终保留用户原始表达、上下文与证据，不被单一意图标签替代。
4. 校园事实必须能够追溯到官方来源和核验时间。
5. 模糊问题优先形成合理假设并提供帮助，仅在结果会实质改变时追问。
6. 权限、密钥、数据隔离和高风险操作由代码强制控制，不交给模型决定。

## 文档入口

从 [文档索引](docs/00-index.md) 开始阅读。

当前开发先读：

1. [当前 PRD](docs/01-prd.md)
2. [开发计划](docs/11-delivery-plan.md)
3. [当前实现状态](docs/13-implementation-status.md)
4. [阶段 6：功能完成与本地可运行交付](docs/20-stage-6-productization.md)
5. [本地试用与运行手册](docs/21-pilot-demo-runbook.md)

实现细节按需查阅[当前系统结构](docs/02-system-spec.md)、
[Agent 行为](docs/03-agent-spec.md)、[采集规格](docs/05-data-ingestion-spec.md)、
[工具与应用 API](docs/06-tool-api-spec.md)和
[Source Registry 运行手册](docs/14-source-registry-operations.md)。历史评测、验收与
早期构想只作背景参考，不能自行生成开发任务。

架构决策记录位于 [`docs/adr`](docs/adr/README.md)，术语定义见 [术语表](docs/glossary.md)。

## 本地运行

前置环境：

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/)
- Node.js 24+ 与 npm

安装并启动后端：

```bash
make api-install
make api-migrate
make api-dev
```

首次同步和检查来源：

```bash
.venv/bin/hzcu-agent list-sources
.venv/bin/hzcu-agent sync-sources --limit 3
.venv/bin/hzcu-agent search-memory "创新训练项目" --top-k 8
.venv/bin/hzcu-agent reindex-memory
```

生产拓扑使用独立 Worker 按 Source Registry 的间隔自动同步；本地也可以运行：

```bash
.venv/bin/hzcu-agent sync-worker --poll-seconds 30
```

另开终端安装并启动前端：

```bash
make web-install
make web-dev
```

然后访问 `http://localhost:3000`。API 健康检查位于
`http://localhost:8000/api/v1/health`。

### Windows 原生启动（不需要 WSL）

本机没有 WSL 时，使用 PowerShell 启动脚本即可。脚本会创建独立的
`.venv-windows`、安装 API/Web 依赖、执行数据库迁移，并在同一个窗口以 Windows
稳定的 Webpack 开发模式启动 API 和 Web；按 `Ctrl+C` 会同时停止两个服务：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-windows.ps1
```

默认使用无需密钥的 Demo 模式，并让 Web/API 监听 `0.0.0.0`。启动完成后终端会同时
打印本机地址和检测到的局域网地址；手机与电脑连接同一局域网后，直接打开类似
`http://192.168.1.23:13000/` 的地址即可试用。脚本会自动把活动网卡 IPv4 加入
Next.js 开发来源白名单和 API CORS，不需要把 `0.0.0.0` 当作访问地址。

可先复制
`config\windows.env.example` 为 `config\windows.env` 做本机配置；如果要使用仓库
根目录现有的 `API.txt`，执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-windows.ps1 -ModelMode Real -ModelConfig .\API.txt
```

需要在页面配置 API 时，使用本地管理员模式：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-windows.ps1 -LocalAdmin
```

然后打开终端打印的本机或局域网地址并进入 `/admin`，首次设置本地管理员账号和密码，进入
“模型与 API”填写协议、端点、API Key、主模型和辅助模型。API Key 只会在服务端加密
保存，不会回显到页面。

脚本不读取或打印模型密钥；`API.txt` 和 `config\windows.env` 均不会提交到仓库。

默认 `api-dev` 使用无需密钥的演示模式。使用项目现有的真实模型配置：

```bash
make api-migrate
make api-real API_PORT=18000 MODEL_CONFIG=API.txt
```

`api-real` 只把 UTF-8/UTF-8 BOM 配置读入当前进程内存，不复制 `.env`，也不输出
API Key。另开终端启动与该 API 端口匹配的 Web：

```bash
make web-dev API_PORT=18000 WEB_PORT=13000
```

然后访问 `http://127.0.0.1:13000/`。不要把 `API.txt`、`.env`、Cookie、统一身份
认证密码或 Token 提交到仓库。

公开版本不包含校外 VPN sidecar、真实登录抓取脚本或信源发现产物。正式校内部署
应使用 `HZCU_CAMPUS_QUERY_ROUTE=direct`，并按目标网络环境实现、审核实时采集器。

## 验证

```bash
make api-test
make web-build
```

当前实现进度、已验证能力和已知边界见
[实现状态](docs/13-implementation-status.md)。
来源配置、同步运行和故障处理见
[Source Registry 运行手册](docs/14-source-registry-operations.md)。

## 阶段 6 本地试用

阶段 6 已在当前开发机完成真实模型和系统 Microsoft Edge 验证。首次安装后启动：

```bash
make api-install
make api-migrate
make api-real API_PORT=18000 MODEL_CONFIG=API.txt
```

另开终端运行：

```bash
make web-install
make web-dev API_PORT=18000 WEB_PORT=13000
```

访问 `http://127.0.0.1:13000/`，API 为 `http://127.0.0.1:18000`。未登录用户也可
读取已经批准用于试用的 Public 与 Campus 本地镜像；登录只增加实时 Campus 查询、
跨设备校园身份和管理员能力。

实际目标机器部署已由用户延期；Docker、CA、VPN 和正式域名配置留待用户重新提出。
完整启动、身份使用和故障处理见[本地试用与运行手册](docs/21-pilot-demo-runbook.md)。
