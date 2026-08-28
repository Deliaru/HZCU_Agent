# 当前功能与实现对应表

| 属性 | 值 |
|---|---|
| 文档编号 | TRACE-001 |
| 状态 | Current |

## 1. 使用规则

本表只记录用户已经确认的功能与当前实现位置，不创建新需求，不规定额外指标，也不
要求为每项功能建立独立评测工程。新增一行前必须先获得用户对新功能的明确确认。

## 2. 当前功能

| 已确认功能 | 主要实现 | 当前状态 |
|---|---|---|
| 自然语言与多轮上下文 | Model Gateway、Coordinator、会话 API | 已实现 |
| 模型自主调查 | 动态工具目录、调查步骤、Tool Gateway | 已实现 |
| 校园本地镜像搜索 | Source Registry、SQLite FTS5、Campus Memory | 已实现 |
| 通用文档探索 | inspect/find/read locator/read segment | 已实现 |
| PDF、图片和扫描材料 | PDF 解析、通用 OCR、操作员核验导入 | 已实现 |
| 来源与适用时间 | Evidence Workspace、回答证据、来源 API | 已实现 |
| 匿名直接使用 | ProductSubject、访客会话、Pilot 镜像开关 | 已实现 |
| 历史会话与刷新恢复 | conversations/tasks/answers、Web 会话轨 | 已实现 |
| 画像、待办与反馈 | Profile、Todo、Feedback API 与“我的空间” | 已实现 |
| 来源账本 | Sources API 与 Source Observatory | 已实现 |
| 桌面与移动 Web | Next.js Web | 已实现 |
| 简洁/琮羽双主题与设备端偏好 | ThemeProvider、ThemePicker、CongyuAgentView、CongyuSourceLibrary、CongyuArtwork | 已实现 |
| 真实模型配置 | OpenAI-compatible Model Gateway、`hzcu-agent serve` | 已实现并在本地真实调用验证 |
| 单机数据持久化 | SQLite、snapshots、迁移 | 已实现并完成 API 重启恢复验证 |
| CA/VPN/运营台 | Auth、sidecar、admin API/Web | 可选已有能力，不阻塞本地试用 |

## 3. 当前状态

阶段 6 已完成：用户提供的模型配置已驱动真实 Agent，匿名镜像问答、来源打开、刷新
恢复和 API 重启后的数据持久化均已验证。实际目标机器部署由用户明确延期，不作为
遗留开发任务；后续只按用户新指令补充资料或功能。

## 4. 非任务文档

评测规格、可观测性规格、历史阶段验收和 ADR 用于解释已有设计或记录历史，不会自动
生成开发任务。它们与当前 PRD 冲突时，以用户最新指示、当前 PRD、阶段 6 文档和
实现状态为准。
