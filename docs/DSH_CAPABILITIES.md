# DSH 网页、提醒与 Skill

2026-10-28 接入。复用已固定的官方 `deepseek-harness-sdk==0.2.1rc2` 配套原生插件，不另写搜索、HTML 解析、计时规则或 Skill 注册器。该 SDK 仍是预发布版本；“官方实现”不等于已经过长期生产验证。

## 已提供的工具

| 能力 | 模型工具 | 官方实现 |
| --- | --- | --- |
| 公开网页搜索 | `web_search` | `dsh-web`、`dsh-web-search-deepseek`、`dsh-tool-web` |
| 按 URL 读取网页 | `web_fetch` | `dsh-web-fetch-http`、`dsh-tool-web` |
| 持久提醒 | `schedule_create/list/update/delete` | `dsh-schedule`、`dsh-tool-schedule`、原生 JSON storage domain |
| 按需使用知识 | `skill` | `dsh-skill`、`dsh-skill-filesystem`、`dsh-tool-skill` |

默认 DSH 初始提供上述七个工具和七个 World 工具；App 工具仍通过 `world_tools` 展开。`world_remind/world_skills/world_read_skill` 不再在 DSH 中注册，避免双重实现。显式 `--runner native` 保留原有轻量实现，不托管 DSH 的提醒和会话。

浏览器操作后端未安装：没有点击、登录态、表单或 JavaScript 页面交互。匿名 HTTP 读取沿用官方限制，包括私网地址拒绝、重定向边界、类型与体积限制；失败会作为真实工具结果返回。这些单次工具的官方限制不是会话输入 token 预算。

## DeepSeek 官方搜索配置

在本地 `.env` 配置 `DEEPSEEK_API_KEY`。不要将密钥放在聊天、Skill 或 App manifest 中。搜索独立于 `ASTCHO_WORLD_API_KEY/BASE_URL`，不会把聊天网关凭证自动发送给另一家服务。

```dotenv
DEEPSEEK_API_KEY=填入自己的官方密钥
# 可选；省略时沿用官方默认值，不从聊天 base URL 推导
# DEEPSEEK_SEARCH_BASE_URL=https://api.deepseek.com/anthropic/v1
```

DSH 通过独立 Messages 请求调用 DeepSeek 原生搜索，提取真实搜索结果和引用片段，不把普通模型回答伪装成搜索结果。该请求有独立用量，不包含在 World 根决策步数中。本次只验证本机合成端点，未调用收费的真实搜索 API。

当前本地尚无专用搜索密钥。工具已注册，运行状态会标明 `configured: false`，调用时返回凭证缺失；配置后重新启动实际运行服务即可使用。只读 Web 观察服务不会为测试搜索而启动模型。

## 提醒的权威与交付

定时、周期/时区规则、创建、修改、删除和投递历史由原生 Schedule service 负责。数据位于 World 私有 `var/dsh/storages/`，不读取用户全局 DSH 数据。

固定 rc2 的 Schedule service 要求 `sessionController.resolveAgent`。`dsh/schedule.mjs` 只提供嵌入适配：解析到实际 SDK Agent，不启动 Web 控制器或第二套模型循环。`serve` 启动时恢复 SDK 会话和计时器，不等下一条 QQ 消息；首次初始化会创建一个不调用模型的维护回合。

原生到期消息由 pre-step 扩展消费，不直接交给模型续跑。适配器从原生已落盘的投递历史取出该次原始提醒文字，以“提醒 ID + occurrence 时间”去重写入 World 通知。World 原有串行主体循环再送达通知，星回仍可忽略或结束。重启时重新对账原生投递历史，修复原生确认与 World 通知落盘之间的中断。

旧 `reminders` 行保留原始 ID、时间和正文；未触发项通过服务 API 导入，原行 `fired=2` 标记为已迁移，映射保存在 World state。稳定迁移标记可恢复“原生已保存、映射未保存”的窗口。已经触发的旧提醒不重新导入。旧计时器在 DSH 接管后停止，不与新计时器并行。已过期而未触发的旧项按 1 秒延迟补交；原目标时间仍保留在旧行。DSH 会规范化提醒文字的首尾空白，原文仍在旧行。

Web 读取原生目录的只读投影，显示下一次时间、正文和状态；不启动计时器、不将通知标为已读。原版生活日程服务与上述自设提醒是两套含义不同的数据，未修改原版日程。

## Skill 范围与格式

只扫描 `--skills` / `ASTCHO_WORLD_SKILLS_PATH` 明确指定的目录。`includeDefaultRoots=false`，不自动扫描 `~/.agents`、`~/.dsh` 或其他项目的 Skill。

支持 `<name>/SKILL.md` 目录包和单个 `<name>.md`。使用官方 frontmatter：

```markdown
---
name: notes
description: Notes 应用的保存与回看说明，按需使用。
---
正文放在这里。
```

目录只提供名称和描述；正文由 `skill` 工具主动加载。文件变更沿用官方 watcher。示例 `examples/skills/notes.md` 仅补充上述元数据，原有正文不变。其他旧 Markdown 如缺少元数据，需要按此格式补充；文件不会被自动改写。

## 信息与操作边界

网页和 Skill 正文仅在实际工具读取后写入观察记录。DSH 原生调用在执行前记操作，最终 `tool/result` 落入会话后，才记录模型实际收到的内容；读取失败保留失败，写入失败/中断保留不确定性。`world_finish` 后不能继续启动原生操作。未读 App 数据不会因此进入摘要或 Skill。

稳定环境提示和人格原文仍作为原样前缀传给 DSH，原生网页插件在其后附加自己的工具说明。变更仅属于环境层；受保护的原聊天 prompt、拼装和模型参数未改。

## 官方参考

- [Web 工具](https://github.com/deepseek-ai/deepseek-harness/tree/master/packages/tools/tool-web)
- [DeepSeek 搜索 provider](https://github.com/deepseek-ai/deepseek-harness/tree/master/packages/web/web-search-deepseek)
- [Schedule 子系统](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/schedule.md)
- [Skill 文件 provider](https://github.com/deepseek-ai/deepseek-harness/tree/master/packages/skill/skill-filesystem)

链接指向上游当前文档；实现以实际安装的固定版本为准，升级需重新验证插件 API、提醒交付和持久化格式。
