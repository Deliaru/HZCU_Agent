# 本地试用与运行手册

| 属性 | 值 |
|---|---|
| 文档编号 | RUNBOOK-PILOT-001 |
| 状态 | Current；Stage 6 已实测 |
| 目标 | 在当前开发机运行真实模型 Web、API 和本地镜像 |
| 范围说明 | 实际目标机器部署已由用户延期 |

本手册只说明如何运行和试用当前产品，不规定彩排、留档或额外验收流程。

## 1. 首次安装

前置环境：Python 3.12、`uv`、Node.js 24 与 npm。

```bash
make api-install
make api-migrate
make web-install
```

## 2. 使用现有模型配置启动

终端一：

```bash
make api-real API_PORT=18000 MODEL_CONFIG=API.txt
```

终端二：

```bash
make web-dev API_PORT=18000 WEB_PORT=13000
```

访问：

```text
Web: http://127.0.0.1:13000/
API: http://127.0.0.1:18000/api/v1/health
```

`api-real` 会在内存中读取 UTF-8/UTF-8 BOM 的 `API.txt`。文件可使用
`HZCU_OPENAI_API_KEY=...`、`HZCU_OPENAI_BASE_URL=...` 的命名格式，也兼容已有的
Key 与 URL 分行格式。程序不会创建 `.env`，不会打印 Key；请勿把配置文件提交或
转发。

如果端口空闲，也可以省略端口参数，使用 API 8000 和 Web 3000。若更换 API 端口，
两个 `make` 命令的 `API_PORT` 必须一致；`web-dev` 会把地址传入 Windows Node
进程，避免 WSL 环境变量丢失后错误代理到 8000。

## 3. 如何试用与登录

打开 Web 后可以直接跳过首次引导并提问，不登录也能搜索已批准的 Public/Campus
本地镜像，包括工程学院培养方案、通知、新闻和已经导入的扫描材料。页面顶部会标明
“PUBLIC + CAMPUS 本地镜像”。

登录只用于部署环境已经配置的实时校内来源、跨设备校园身份和管理员能力。当前本地
试用不要求登录；未配置 CAS 时登录入口不可用，不影响镜像问答。

建议试问：

```text
根据本地镜像中的2025级智能建造专业培养方案，这个专业学制几年、
授予什么学位、毕业总学分多少？请注明来源。
```

已实测答案应引用工程学院 2025 级培养方案第 48—49 页，给出四年、工学学士和
165.0 学分。模型仍会自主选择和翻阅材料，不用该问题的专用脚本。

## 4. 已确认的使用链路

阶段 6 已在真实 Microsoft Edge 390×844 视口完成：

1. 首次引导跳过与匿名提问；
2. 真实模型回答和证据面板；
3. 反馈提交和会话历史恢复；
4. 待办新增、完成和删除；
5. 49 个来源的透明账本；
6. 页面刷新及 API 重启后的回答恢复；
7. 无横向溢出，控制台 0 error / 0 warning。

截图：
[hzcu-stage6-complete-real-edge-390x844.png](../output/playwright/hzcu-stage6-complete-real-edge-390x844.png)

## 5. 停止与数据位置

在两个终端分别按 `Ctrl+C` 停止 Web 和 API。默认数据位置：

```text
data/hzcu_agent.db
data/snapshots/
```

不要删除这两个位置。重新执行第 2 节命令后，原有会话、回答和来源仍可读取。

## 6. 常见问题

| 现象 | 处理 |
|---|---|
| “身份与 Agent 服务暂时无法连接” | 确认 API 与 Web 都已启动，并且两条命令的 `API_PORT` 一致 |
| 来源账本返回 `{"detail":"Not Found"}` | 不要让 Web 误连其他 8000 服务；使用本手册的 18000/13000 命令 |
| 健康检查显示模型未配置 | 确认 `MODEL_CONFIG` 指向现有配置文件；不要在页面或日志粘贴 Key |
| 匿名用户查不到 Campus 镜像 | 使用 `api-real`；该命令已启用匿名试用镜像开关 |
| 上游提示 overloaded | 保持任务运行；网关会对通用瞬时故障有限退避重试 |
| 重启后没有历史会话 | 确认启动前后位于同一项目目录并使用同一个 SQLite 文件 |

## 7. 可选能力与延期部署

`.env`、Docker Compose、CA、校园实时查询和 VPN sidecar 仍作为可选能力保留，但
不是当前本地试用的前置条件。实际目标机器部署已经由用户延期；未来只有在用户重新
提出并给出目标环境后，才开展安装、域名、证书、CAS 或 sidecar 配置。
