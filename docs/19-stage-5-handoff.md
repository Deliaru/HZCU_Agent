# 阶段 5 历史交接记录

| 属性 | 值 |
|---|---|
| 文档编号 | HANDOFF-S5-001 |
| 状态 | Complete / Historical |
| 冻结日期 | 2026-07-28 |
| 当前用途 | 记录阶段 5 已完成实现，不产生后续门槛 |

本文只保存阶段 5 当时的实现和回归结果。文中的样本、指标与工具不构成阶段 6
开发任务；当前范围以 [`20-stage-6-productization.md`](20-stage-6-productization.md)
为准。

## 1. 交接结论

阶段 5 的检索、证据接地和 Agent 主链路停止扩建。正式主路径固定为：

```text
Prepare（最多 3 个独立问题）
→ 每个问题独立执行 SQLite FTS5 Top 8
→ Composer 一次完成回答与 Claim—Evidence 映射
→ 结构校验；仅高风险、冲突或结构失败时运行 Verifier
```

FTS 使用 trigram tokenizer 和 BM25，标题权重 5、正文权重 1。严格短语查询零命中
时只执行一次同词 OR 放宽，不累计两轮分数。当前版本、启用来源、身份可见性、
质量状态和 2023 年时间边界由关系查询统一约束。

禁止在后续阶段重新引入：

- 业务语义正则硬门或按问题标签决定“能否检索”；
- 多查询共享候选池、跨查询累加打分或公平轮转；
- 主链路分块向量扫描、embedding JSON 水合或 Coordinator 二次排序；
- 仅因材料来自历史镜像而触发实时工具或独立 Verifier。

48 题、两道历史题和两道随机题可在相关实现发生问题时作为回归材料使用，但不会
自动成为每次改动的阻塞条件。

## 2. 冻结验证证据

| 项目 | 结果 | 记录 |
|---|---:|---|
| 多领域检索集 | 48/48，Top 8 期望 URL 100% | `output/fts-eval-kiss-relax.json` |
| 完整标题查询 | Top 3 100% | 同上 |
| 热态检索 P95 | 119.137 ms | 同上 |
| 历史两题 | 2/2 | `output/fts-agent-regression-kiss.json` |
| 随机两题 | 2/2 | `output/fts-agent-random-probe-kiss.json` |
| 后端测试 | 66 项通过 | 阶段 5 冻结运行 |
| Ruff / Web | 通过 / typecheck + build 通过 | 阶段 5 冻结运行 |

当时两道历史题及预期结果为：

1. “这个学年暑假后什么时候开学？”预期区分新生报到、往届生报到和开课日期。
2. “国创大概什么时候会中期检查，校创需不需要？”预期明确校创也有中期检查，
   并把历年规律和当年正式通知分开。

当前模型配置基线为 OpenAI Responses API，Agent 默认
`gpt-5.6-sol / medium`，Utility 默认 `gpt-5.6-terra / low`。无密钥时只进入明确
标识的 demo adapter，不伪装成真实语义推理。

## 3. 运行与恢复

- 数据库：单节点 SQLite，正式路径由 `HZCU_DATABASE_URL` 指定。
- 索引：`campus_search_fts_v1`，启动时幂等创建并用触发器同步。
- 完整重建：`.venv/bin/hzcu-agent reindex-memory`。
- Prompt：`apps/api/src/hzcu_agent/prompts.py`。
- API：`/api/v1/conversations`、`/tasks`、`/answers` 与 SSE 契约保持兼容。
- 回滚：应用代码可回滚；FTS 是可丢弃派生索引，回滚前保留 SQLite 备份，不删除
  chunks、embedding 或 entity 表。

## 4. 历史事项说明

- 回答持久化和返回前的非法控制字符清理已经实现。
- 当时提出的额外规模评测和人工评审设想已经撤销，不向阶段 6 移交，也不作为
  开发、部署或完成条件。
- 如果实际使用暴露功能或部署错误，直接按错误修复；不为历史指标另建项目。
