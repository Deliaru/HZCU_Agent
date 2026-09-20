# Agent 任务网络准入

管理员后台「Agent 策略」中的「Agent 任务网络限制」支持独立开关、IP/CIDR 白名单、管理员与贡献者豁免以及拒绝提示。保存后本进程立即生效，配置持久化在 agent_runtime_policies；当前部署为单 API 进程。多进程部署前需增加策略缓存同步。

该开关独立于配额 observe/enforce 模式；开启且白名单为空时，仅已勾选豁免的认证角色可发起任务。关闭角色豁免不影响进入后台。普通提问、重试、重新核验均经同一准入服务检查，拒绝不创建任务、不扣任务额度、不调用模型。既有任务继续运行，历史数据仍按主体隔离。

贡献者可使用 Agent 会话与个人功能，不获得管理员权限或额外校园数据权限。IP 豁免不豁免其他既有预算、CSRF、身份和队列规则。

## 可信来源链

生产使用 Nginx → Caddy → API。Nginx 仅传递来源，不承担访问筛选。在 Agent 站点的 proxy location 中设置：

```nginx
proxy_set_header X-HZCU-Client-IP $remote_addr;
```

Nginx 必须覆盖客户端传入的同名头。Caddy 仅绑定宿主机回环地址，并在转发前对非宿主网关来源移除该头，例如（实际网关须由 docker inspect 确认）：

```caddyfile
@untrusted not remote_ip 172.18.0.1
request_header @untrusted -X-HZCU-Client-IP
```

API 通过 HZCU_NETWORK_TRUSTED_PROXY_CIDRS 配置受控内部代理网段（当前生产为 172.18.0.0/16），Uvicorn 禁用 proxy headers，保留真实连接 peer。只有配置内的 peer 能声明 X-HZCU-Client-IP；缺失、重复、非法声明按未知来源拒绝。非可信 peer 的所有来源头均忽略，使用其 socket IP。容器网络中的服务视为受信基础设施，API 端口不得公开映射。

IPv4、IPv6 CIDR 均支持；IPv4-mapped IPv6 来源规范化为 IPv4。CIDR 主机位不为零会被拒绝，防止误填导致扩大授权。已知三个出口应各填 /32，不能推断整个 /24 已获授权。

## 发布与回滚

先备份数据库及代理配置，构建新镜像，执行 alembic upgrade head。迁移默认关闭网络限制，避免升级过程中丢失访问；发布时按批准的白名单启用并记录配置审计。检查公网首页和 health 可访问，公网匿名任务返回 AGENT_NETWORK_DENIED，伪造来源头无效，受控代理内的允许来源及角色豁免有效。

日常停用限制可直接在后台关闭；代码回滚可使用旧镜像，新增数据库字段可保留，无需破坏性降级。代理转发和可信网段变动时应重新核验来源链。
