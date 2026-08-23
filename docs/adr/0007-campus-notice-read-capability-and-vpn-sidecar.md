# ADR-0007：CA/VPN 仅授予校园通知只读能力

| 属性 | 值 |
|---|---|
| 状态 | Accepted |
| 日期 | 2026-07-26 |
| 修订 | 信息中心授权后 |

本文只记录已经实现的可选 CA/VPN 只读边界，不要求阶段 6 配置正式 sidecar、证书、
容量或网络平台。只有用户选择在实际目标环境启用该能力时，才处理对应部署条件。

## 背景

信息中心已允许 Campus Agent 接入真实 CA，并允许在校外查询场景中接收学生凭据
建立 VPN 会话。现有 aTrust 链路实测依赖 Windows 本地客户端、Headless Edge、
运行时 CSRF/trace 头和浏览器会话，不能把 Cookie 复制给 Linux API，也不能把
它伪装成普通无状态 VPN API。

授权同时给出明确业务边界：CA/VPN 只用于查询校园通知，不允许 Agent 代替学生
申请、提交、撤回、报名、选退课、评价、预约或执行任何其他业务写操作。

## 决策

### 网络路由

- API 部署节点在校园网且只读探针成功时，使用校内直连同步登记的 Campus 通知源。
- API 节点不具备校内路由时，使用学校批准的 Windows aTrust + Headless Edge
  sidecar。
- `auto` 模式先探测服务端网络，而不是根据学生浏览器 IP 猜测可达性。
- VPN sidecar 不是通用代理，只接受查询文本和固定能力，没有独立目标 URL、
  HTTP 方法、表单字段或任意脚本参数；查询原文已有 URL 时也只能直读登记主机。
- 经信源发现验证的 37 条 aTrust 通知代理路由作为独立只读路由注册表；运行时
  仍必须与主 Source Registry 的来源 ID、规范主机和只读内容边界交叉校验。

### 凭据生命周期

- 标准校内登录仍优先使用 CAS Redirect + 一次性 Service Ticket。
- 校外凭据表单只在 `HZCU_CREDENTIAL_VPN_ENABLED=true` 时出现。
- Web/API 与 API/sidecar 两段在生产均必须使用 HTTPS；sidecar 还应置于 mTLS 或
  等价的受控反向代理之后。
- 密码以 `SecretStr` 接收，一次性转交 sidecar 登录 Edge，随后清空引用；不进入
  数据库、日志、快照、Source Registry、审计元数据或模型上下文。
- 中心 API 只在进程内保存 sidecar 生成的随机不透明句柄；sidecar 只在内存保存
  Edge Context，默认 15 分钟，到期或退出即关闭。
- 本地用户仍只保存 HMAC 主体、脱敏尾号、随机应用会话哈希和权限范围。

### 能力与协议边界

唯一授权能力为：

```text
campus_notice.read
```

- 模型工具只接受 `query` 与 `limit`。
- 通知详情必须同时通过 Source Registry 的来源 ID、精确主机和只读内容边界；
  详情路径模式用于栏目归属与解析，不作为新页面的语义硬门。
- 普通业务网络请求只允许 `GET`、`HEAD`、`OPTIONS`。
- `POST` 只允许 CA 登录、aTrust 会话交换和资源目录控制路径；仅匹配认证主机但
  路径不匹配的 POST 仍被阻断，业务主机上的 POST 一律阻断。
- `PUT`、`PATCH`、`DELETE` 在浏览器网络层无条件阻断。
- Planner 提示不是安全边界；Tool Gateway、sidecar 路由策略和来源白名单共同构成
  不依赖 LLM 的强制边界。
- Personal 数据端点与所有写操作均不因本授权进入范围。

### 与后台采集的关系

学生 VPN 会话只服务该学生当前的实时通知查询，不作为全局 Worker 身份，也不能
进入共享的无人值守采集配置。长期定时采集仍使用校内节点或单独批准的服务身份。

## 后果

正面：

- 校内与校外都能使用同一只读通知工具。
- 真实凭据不会进入模型或持久层。
- 即使模型误规划或页面含提示注入，也无法扩大为申请代办。
- aTrust 的浏览器依赖被隔离在 Windows 节点，不污染 Linux API 拓扑。
- 真实 Headless Edge 验收已证明 CA→aTrust→登记通知详情查询闭环可用，且验收
  只保留来源 ID 与数量，不保留凭据或正文。

代价：

- 校外模式需要维护 Windows、aTrust、Edge、HTTPS/mTLS 和容量上限。
- sidecar 进程重启会使短时 VPN 句柄失效，用户需要重新认证。
- 额外验证码、动态口令或设备确认无法自动绕过，必须显式失败并交还用户。
- 信息中心仍需给出正式回调 Service、证书、部署网段和 sidecar 机器清单。

## 明确拒绝

- 保存学生密码供后台反复登录。
- 把负责人测试账号共享给所有用户。
- 允许模型指定任意目标 URL、方法或业务表单。
- 通过 CA 登录自动获得课表、成绩、申请状态等 Personal 数据。
- 为了“智能”而隐藏或绕过验证码、二次认证与访问控制。

## 关联

- [ADR-0006](0006-optional-cas-membership-and-service-identity.md)
- [安全与隐私规格](../08-security-privacy-spec.md)
- [工具与 API 规格](../06-tool-api-spec.md)
- [阶段 2 验收](../16-stage-2-acceptance-and-ca.md)
