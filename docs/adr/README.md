# 架构决策记录

ADR 用于记录已经作出的重要架构决策、选择原因和后果。

## 状态

- `Proposed`：正在讨论。
- `Accepted`：当前有效。
- `Superseded`：已被后续 ADR 替代。
- `Deprecated`：不再推荐，但尚未被具体 ADR 替代。

## 目录

| ADR | 状态 | 决策 |
|---|---|---|
| [0001](0001-model-native-agent.md) | Accepted | 模型原生 Agent，而非知识库问答 |
| [0002](0002-semantic-classifier-as-signal.md) | Accepted | 分类器作为语义信号，不作为硬路由 |
| [0003](0003-live-grounding-and-temporal-memory.md) | Accepted | 实时官方来源与时间版本记忆 |
| [0004](0004-single-coordinator-and-tools.md) | Accepted | 单协调 Agent 与受控工具生态 |
| [0005](0005-source-authority-and-versioning.md) | Accepted | 来源权威和不可变版本 |
| [0006](0006-optional-cas-membership-and-service-identity.md) | Accepted | 用户校园身份与采集服务身份分离 |
| [0007](0007-campus-notice-read-capability-and-vpn-sidecar.md) | Accepted | CA/VPN 仅授予校园通知只读能力 |

## 新增 ADR

新增决策时复制以下结构：

```text
# ADR-NNNN：标题

- 状态
- 日期
- 背景
- 决策
- 后果
- 被否决或推迟的方案
- 关联文档
```

已接受 ADR 不应被静默重写。若决策变化，新增 ADR 并将旧 ADR 标为 `Superseded`。
