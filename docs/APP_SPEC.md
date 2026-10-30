# App 协议 v1（当前最小实现）

## 边界

App 拥有自己的连接、配置、数据库和工具。内核只管理注册、调用、事件产生的通知、观察和操作结果。App 不导入其他 App 的实现；跨 App 操作由调用者组合。

面向人发送的正文统一遵守 World 从原聊天模板逐字提取的说话规则；新增 App 不另设一个发言人格，不要求导入 QQ 或自行调用聊天模型。工具描述应明确接收对象和哪些字段会对人可见，内部理由不混入正文。群聊和 QQ 私聊保留各自原场景要求；笔记、内部记录和独立交付文件不套用聊天短句格式。规则来源、范围和验证见 [对外说话规则](OUTBOUND_SPEECH.md)。

当前提供的是进程故障隔离，不是恶意插件的安全沙箱。只安装可信开发者提供的 Python App。进程有同一系统用户的文件权限。

## Manifest

使用 JSON；可执行校验由 `astcho_world.contracts.Manifest` 提供。参考 `examples/manifests/*.json`。

- `app_id`：小写字母开头，只允许字母、数字、下划线；保留 `world` 和双下划线。
- `version`：应用版本。
- `protocol`：当前固定为 1。
- `description`：能力描述，不包含强制生活任务。
- `entrypoint`：`module:factory`。factory 接受 `(data_dir: Path, config: dict)`。
- `tools`：工具定义数组。名称在 App 内唯一。
- `events`：声明该 App 的事件种类。当前通用投递入口实现通知子集；其他后台事件分发尚未实现。

工具字段：`name`、`description`、`input_schema`、`output_schema`、`effect`、`timeout`。schema 使用 JSON Schema，顶层必须是 object。effect 为 read/write。工具超时范围为 0–120 秒（不含 0）。

原生 App 的模型工具名为 `app_id__tool_name`。模型先发现应用，再展开工具，不自动读取应用记录。

## 进程协议

每个 App 独立子进程。stdin/stdout 为 UTF-8 JSON Lines，stdout 专用于协议，普通日志写入应用目录中的 `worker.log`。

请求：

```json
{"id":"request-id","method":"call","params":{"tool":"read","arguments":{"id":"note-id"},"operation_id":"operation-id"}}
```

`params` 还可带主机提供的 `activity_id`（字符串或 null）。它是可选的执行元数据，不属于模型可填写的工具参数；当前 DSH/native 主体沿用同一活动标识，App 可用于活动内去重或限次，不需要依赖运行器实现。无活动的直接调用仍保留操作 ID 去重。

回复：

```json
{"id":"request-id","result":{"id":"note-id","title":"标题","text":"内容"}}
```

回复可附带可选 `dashboard_state` 管理元数据，由 App 的同步只读 `dashboard_state()` 方法产生。主机将其存入本 App 目录的管理快照，不作为工具结果或模型观察。该投影应来自现有内存/本地计数，不启动学习、模型请求或外部副作用；发布失败不改变业务操作结果。

执行异常返回 `error` 字符串。内核在发送前校验输入，收到后校验输出，记录统一 Result：`operation_id/status/data/error`。

App 的 `async dispatch(method, params)` 实现业务。单个 App 的请求串行；不同 App 无全局执行锁。超时、连接中断或协议损坏只终止该 App 进程，其他应用继续。

## 生命周期

`Kernel.register` 只注册，不启动外部连接。首次调用时启动进程。`stop_app` 停止并禁止自动拉起，`start_app` 恢复；`unregister` 停止并移除注册，保留数据。

应用目录为 `<world-dir>/apps/<app_id>/`。升级时由 App 自己处理持久化格式兼容；当前参考应用仅为首版，尚无通用迁移管理器。

环境目录由单个 Kernel 拥有，文件锁防止两个主体同时恢复或执行同一份状态。当前文件锁实现适用于 macOS/Linux。

## 通知与观察

可信传输入口调用 `Kernel.ingest(app_id, payload)`，App 在自身数据库存储原始事实，返回：

```json
{"notifications":[{"id":"stable-event-id","source":"来源","message":"单条消息","locator":{"chat_id":"group:123","anchor_message_id":"456"}}]}
```

通知按 App 和事件 ID 去重。`locator` 是内部定位，不作为通知正文展示。通知只向星回展示来源与单条消息，另附可操作的通知 ID 和 App ID。

通知送达与读取群聊是不同记录。`world_open` 必须引用已送达通知，随后走公开读取工具。QQ 返回固定观察快照与分页位置；新消息不会自动进入旧快照。主动读取不要求先有通知。

通知送达、工具结果、Skill 读取分别进入观察记录；原始收件箱内容不自动加入模型输入。数据保存在本地 SQLite，不导出到其他 App。

## 副作用、重试与恢复

操作 ID 与 App、工具、参数绑定，重复 ID 返回已有结果，不重复执行；改变参数必须使用新 ID。

写入超时、取消、连接丢失或进程崩溃后，状态可能是 unknown。不能换一个 ID 自动重试外部发送。内核不宣称跨系统恰好一次；需按实际平台回执查询和处理。

Notes 在自身事务中记录保存回执。QQ 的同一观察快照在 `reply_group`、`no_reply` 与兼容参与之间只执行一次社交决定；相同决定返回已有回执，冲突决定拒绝执行。发送逐段保存实际消息和回执，中断后保持 unknown，不能通过新操作 ID 重放。正文不经第二个模型生成，也不自动再次转发；具体迁移见 [群聊主体回复](QQ_SUBJECT_REPLY.md)。

QQ 的可选 `preamble` 使用独立账本，不占最终社交决定。同一活动的同一会话、同一会话的同一目标消息分别最多一次；失败或未知尝试仍占位，不换 ID 重放。具体机制见 [一次行动前说明](QQ_SUBJECT_REPLY.md#可选的一次行动前说明)。

Result 定义预留 running/cancelled；当前内核未实现通用后台 Job 服务。应用级后台工作需要自行提供 start/status/cancel 工具，后续再加入统一事件推进。

## Skill

当前支持显式配置目录中的 Markdown 文件，按需列出与读取。Skill 不授予能力，不替代工具参数，不在安装后自动进入 prompt。通用 Skill 安装包和版本解析尚未实现。

## 新 App 接入步骤

1. 写自己的 factory 和 dispatch，不依赖 QQ 或 Notes。
2. 写 Manifest，声明工具 schema、效果与超时。
3. 在配置中添加 Manifest 路径和非敏感配置。路径可使用 `${CONFIG_DIR}`。
4. 校验读取、写入回执、错误/超时、停止恢复和数据隔离。
5. 若需要模型使用说明，单独提供可选 Skill。

不需要修改 Kernel 或 Agent 的应用分支。Library 是新增第三个应用的实例。

## DSH 适配

默认主体由官方 DSH Python SDK 运行。`WorldTools` 统一提供工具定义和执行入口，原生 Cordis 插件通过本机私有 socket 调用，不改变 App 的 JSONL 协议。`world_tools` 展开后注册 `<app_id>__<tool>`，DSH 的 call ID 与持久 session ID 组合为 World 操作 ID，保留回执与未知状态。

App 不导入 DSH，不直接持有模型对象。DSH 插件也不能绕过 Kernel 访问其他 App 的数据库。操作是否可用仍由 Kernel/App 的实际状态决定。新增 DSH 工具、插件或 Skill 时，需明确其数据与副作用入口，避免绕过未读内容边界；安装 SDK 不等于自动开放其全部默认工具。

## 可选环境投影、账号控制与意图防重（2026-10-29）

协议1兼容增加以下可选字段；原有 App 无需实现：

- Manifest `ambient_schema`：严格 JSON Schema。worker 的 `ambient_state()` 只能发布该 Schema 允许的环境元数据，宿主缓存时间并在运行上下文标明过期；不能把管理员 `dashboard_state` 直接当成主体观察。无效投影不改变实际工具回执。
- Manifest `admin_tools`：允许管理页面显式调用的少量账号设置工具。仍需本地认证和同源检查；管理动作不记录为主体观察，也不启动主体。
- Tool `observed_url_fields`：指定链接字段必须来自主体已有观察，Kernel 校验后才能交给 App。
- Tool `idempotency_field`：模型提供行动意图ID，Kernel 以 App/工具/意图组成稳定操作键。相同意图的不同调用ID仍防重，未知写入不重放；新意图允许新行动。字段必须出现在输入 Schema。不能把此机制解释为外部平台严格恰好一次。
- 调用元数据 `activity_id` 继续表示运行回合，新增宿主提供的 `logical_activity_id` 表示持续活动。它们不是模型参数。
- 受信任 ingest 元数据 `_world_subscription` 包含主体明确选择的 scope、expires_at、activity_id。App只在自己支持的范围内产生单条事件，不把全部未读上下文旁路送达；长期静音与临时订阅分别保存。宿主在结束、过期或App停止时撤销订阅。
- 本地 App 配置可选 `runtime_python` 指向独立依赖环境；安装配置 `enabled_env` 提供单 App 开关。这些由管理员配置，不允许模型通过工具修改任意执行路径。
