# 本地配置迁移

2026-10-28，按用户要求，从 `../astcho-github` 迁入新项目。原项目与原文件保持不变。

后续用户已明确授权正式运行：本地 QQ 发送现已开启，NapCat 反向地址和鉴权已对齐到本项目，command 默认启动主体。下面保留初始迁移记录；当前行为以 [正式运行与私聊说明](PRIVATE_ROUTING.md) 为准。

## 已迁入

| 目标文件 | 内容 |
| --- | --- |
| `.env` | 原模型、API、聊天参数、管理员、群白名单与开关；追加新环境参数 |
| `config/profile.local.json` | 原 `.env` 引用的人格文件，逐字节复制 |
| `config/schedule.local.json` | 原 `.env` 引用的日程文件，逐字节复制 |
| `config/apps.local.json` | 指向本地 Manifest 的应用配置，使用相同名字与群白名单，发送关闭 |

以上文件均为 Git 忽略的本地私密文件，权限为 0600。没有在文档中写入密钥、QQ 号或人格内容。

原 `.env` 中只有数据目录、人格路径、日程路径更换为新项目独立位置；其余原有变量值保留。原文件没有修改。

新环境的 `ASTCHO_WORLD_MODEL/API_KEY/BASE_URL` 默认取原 Planner 配置，独立 Key 或地址为空时取原文本接口配置。只作用于新环境选择，不改变原聊天调用参数或 prompt。

`run/serve` 自动读取 `.env`，默认使用 `config/apps.local.json`。数据写入新项目目录，既有数据库和记忆未迁移。环境工具循环与原聊天的认知数据仍各自位于原定独立位置。

## 反向 WebSocket（默认）

现已恢复原版接入方式。NapCat 配置 Universal 反向 WebSocket：

```text
ws://127.0.0.1:8091/onebot/v11/ws
```

- 账号由握手的 `X-Self-ID` 自动识别并绑定本环境，不需要补填 `ASTCHO_WORLD_BOT_ID`；该变量可用于预先限定账号。
- 收事件、回复查询和发送 API 共用 WS，不需要 `ASTCHO_WORLD_QQ_API`。
- 认证优先使用 `ASTCHO_WORLD_WS_TOKEN`，未配置时沿用原 `ONEBOT_ACCESS_TOKEN`。NapCat 填相同令牌；原来为空时可以保持为空。
- 支持 `/onebot/v11/`、`/onebot/v11/ws` 与尾部斜杠路径。
- 监听优先使用 CLI `--host`、`ASTCHO_WORLD_HOST`、原 `.env` 的 `HOST`，端口默认 8091。NapCat 在 Docker 时可填主机可达地址，例如 `ws://host.docker.internal:8091/onebot/v11/ws`。
- 断开后等待 NapCat 重连，不自动重发结果未知的发送。

真实发送开关在 `config/apps.local.json` 中 `qq.config.conversation.allow_send`，迁移后仍为 false。本轮没有修改 NapCat 配置、连接真实账号或替换原机器人。

## 可选 HTTP 兼容模式

仅显式使用 `serve --qq-transport http` 时需要 `ASTCHO_WORLD_BOT_ID`、`ASTCHO_WORLD_INGRESS_TOKEN`，事件 POST 到 `/onebot/events`，认证 `Authorization: Bearer <ASTCHO_WORLD_INGRESS_TOKEN>`。发送需另配 `ASTCHO_WORLD_QQ_API` 和可选 `ASTCHO_WORLD_QQ_TOKEN`。此前生成的 ingress token 保留，不用于默认 WS。

## 原日程与 Web

迁入的日程由原 `ScheduleService` 读取：通知概率和原聊天上下文都使用它。`serve --schedule` 可指定同格式文件。Web 的“日程与状态”只读展示同一个服务的当前状态、今日时段、特殊日期与临时覆盖；独立观察模式读取本地配置。

人格和原聊天服务按需加载，原 prompt 未改。原数据库、记忆和关系历史未迁移；Web 只显示当前 QQ 进程实际上报的情绪和记忆数量，未初始化时留空。媒体与后台学习尚未完整接入。
