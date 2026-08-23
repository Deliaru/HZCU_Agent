# HZCU Campus Agent 文档索引

| 属性 | 值 |
|---|---|
| 文档状态 | Stage 6 Complete |
| 基线日期 | 2026-08-11 |
| 产品形态 | 独立 Web 校园认知 Agent |
| 当前信息范围 | 浙大城市学院公开官方信息，以及用户明确指定导入的正式材料 |

## 1. 当前开发文档规则

当前开发只以用户的最新明确指令和以下文档为准：

- [`01-prd.md`](01-prd.md)：当前产品范围；
- [`11-delivery-plan.md`](11-delivery-plan.md)：当前开发顺序；
- [`13-implementation-status.md`](13-implementation-status.md)：实际完成情况；
- [`20-stage-6-productization.md`](20-stage-6-productization.md)：阶段 6 完成范围与证据；
- [`21-pilot-demo-runbook.md`](21-pilot-demo-runbook.md)：本地真实模型试用方法。

其他 Spec、ADR、评测记录和阶段验收文档用于说明既有设计或历史结果，不能自行产生
新功能、新指标、新安全工程、新效率工程或流程门槛。新增功能和范围变化必须由用户
明确提出，开发者不得根据历史文档或个人判断扩写 PRD。

阶段 6 已完成。当前没有由文档自动延续的开发任务；实际目标机器部署和任何新增功能
只在用户明确提出后开始。

## 2. 文档地图

| 文档 | 定位 | 内容 |
|---|---|---|
| [01-prd.md](01-prd.md) | 当前范围 | 已确认用户价值、产品能力和明确不做事项 |
| [02-system-spec.md](02-system-spec.md) | 架构参考 | 系统组件、运行拓扑、故障与降级 |
| [03-agent-spec.md](03-agent-spec.md) | 实现参考 | 模型原生 Agent、规划和工具使用方式 |
| [04-domain-model.md](04-domain-model.md) | 实现参考 | 领域实体、关系、约束和时间语义 |
| [05-data-ingestion-spec.md](05-data-ingestion-spec.md) | 实现参考 | 来源登记、采集、版本和索引 |
| [06-tool-api-spec.md](06-tool-api-spec.md) | 实现参考 | Agent 工具和 Web API 契约 |
| [07-memory-personalization-spec.md](07-memory-personalization-spec.md) | 实现参考 | 已确认的画像、对话记忆与主体数据 |
| [08-security-privacy-spec.md](08-security-privacy-spec.md) | 边界参考 | 已实现的权限、凭据和数据保护边界，不构成新增工程清单 |
| [09-evaluation-spec.md](09-evaluation-spec.md) | 历史参考 | 早期评测设想，不构成当前完成门槛 |
| [10-observability-operations-spec.md](10-observability-operations-spec.md) | 历史参考 | 既有诊断与运维设想，不构成当前完成门槛 |
| [11-delivery-plan.md](11-delivery-plan.md) | 当前计划 | 当前阶段、开发顺序与完成条件 |
| [12-requirements-traceability.md](12-requirements-traceability.md) | 当前对应表 | 已确认功能与现有实现的对应关系 |
| [13-implementation-status.md](13-implementation-status.md) | 当前状态 | 已实现能力、历史验证与阶段完成结果 |
| [14-source-registry-operations.md](14-source-registry-operations.md) | 操作参考 | 已登记来源、同步命令和故障处理 |
| [15-frontend-visual-system.md](15-frontend-visual-system.md) | 设计参考 | 视觉语言与响应式规则 |
| [16-stage-2-acceptance-and-ca.md](16-stage-2-acceptance-and-ca.md) | 历史记录 | 阶段 2 数据与 CA 实现记录 |
| [17-stage-4-acceptance.md](17-stage-4-acceptance.md) | 历史记录 | 阶段 4 真实模型验收记录 |
| [18-stage-5-implementation.md](18-stage-5-implementation.md) | 历史记录 | 阶段 5 实现与历史性能样本 |
| [19-stage-5-handoff.md](19-stage-5-handoff.md) | 历史记录 | 阶段 5 冻结结果，不向阶段 6 移交门槛 |
| [20-stage-6-productization.md](20-stage-6-productization.md) | 完成记录 | 功能完成与本地可运行交付 |
| [21-pilot-demo-runbook.md](21-pilot-demo-runbook.md) | 当前操作 | 本地真实模型试用与恢复方法 |
| [glossary.md](glossary.md) | 术语参考 | 统一术语 |

## 3. 架构决策

| ADR | 决策 |
|---|---|
| [ADR-0001](adr/0001-model-native-agent.md) | 采用模型原生 Agent，而非知识库问答 |
| [ADR-0002](adr/0002-semantic-classifier-as-signal.md) | 已废止：分类信号不再参与工具选择、查询扩展或材料过滤 |
| [ADR-0003](adr/0003-live-grounding-and-temporal-memory.md) | 实时官方来源与时间版本记忆共同构成事实基础 |
| [ADR-0004](adr/0004-single-coordinator-and-tools.md) | 采用单协调 Agent 与通用原子工具 |
| [ADR-0005](adr/0005-source-authority-and-versioning.md) | 校园事实按来源权威与版本管理 |
| [ADR-0006](adr/0006-optional-cas-membership-and-service-identity.md) | 用户校园身份与采集服务身份分离 |
| [ADR-0007](adr/0007-campus-notice-read-capability-and-vpn-sidecar.md) | CA/VPN 只提供校园通知读取能力 |

ADR 记录已经采用的架构理由，不得被解释为新增产品需求或阶段完成门槛。

## 4. 当前产品决策

- 服务对象为浙大城市学院新生及往届学生，入口为独立 Web 应用。
- Agent 采用 Everything is model：模型理解问题、选择资料并使用通用原子工具探索，
  代码只执行工具契约、权限和资源边界，不用业务分类器替模型决策。
- 匿名用户可以使用本地公开镜像库；CA 登录仅在实际配置可用时增加校园级只读来源。
- 首个版本不查询个人成绩、课表或选课状态，不执行校园业务写操作。
- 当前已有会话历史、画像、手动待办、反馈、来源账本和错误恢复。
- 阶段 6 已完成；实际目标机器部署已由用户延期，新能力由用户另行决定。

## 5. 后续事项的处理方式

正式部署位置、域名、CA、VPN、证书、消息渠道或新增数据范围，都在用户给出实际
目标和配置后按需处理。它们不是文档自动生成的待办，也不影响阶段 6 已完成状态。

## 6. 文档维护规则

- 用户确认新功能后，才将它写入 PRD 和开发计划。
- 实现状态只记录真实完成情况和当前实际故障，不从测试报告推导新需求。
- 历史指标、样本、评审、演示和彩排不能转化为发布或开发门槛。
- 不为尚未提出的规模、平台、流程或安全场景提前建设系统。
- 示例中不得包含真实密码、Cookie、Token、身份证号、学号或个人成绩。
- 文档统一使用 UTF-8 编码。
