# 环境运行器：DSH Python SDK

2026-10-28 按用户要求纠正实现方向。`run` 和非只读 `serve` 默认使用官方 `deepseek-harness-sdk==0.2.1rc2`，由 SDK 启动配套原生运行时。此前自写的 Python Agent 只保留为显式 `--runner native`，不在 DSH 出错时自动回退。

已在本机 macOS ARM64 安装 SDK 和同版本 `deepseek-harness-runtime-bin`，实际启动原生进程，以本机合成模型服务完成集成测试。未调用真实模型或连接真实 NapCat。

## 分工

```text
唤醒事件 / 自设提醒
  → DSHAgent（官方 Python SDK 的异步适配）
  → DSH sdk-minimal（模型调用、工具循环、持久会话、上下文压缩）
  → World Cordis 插件（工具注册和每步状态）
  → 私有 Unix socket → WorldTools / Kernel → 独立 App 进程
```

`DSHAgent` 不实现另一套模型循环。Python 调用 `DeepSeekHarness.start_session/resume_session` 和 `Session.run`，DSH 发起模型请求并执行原生注册工具。World 保留应用契约、通知/观察边界、数据隔离和操作回执。

`src/astcho_world/dsh/world.mjs` 是原生 Cordis 插件。通过 `ctx.tools.register` 注册七项 World 工具（含主动回看经历）；原有提醒和 Skill 三个入口已由官方原生七个工具替代。`world_tools` 成功后动态注册对应 App 工具。`agent/pre-step` 从 World 获取当前时间、额度和状态；`world_finish` 使用 DSH 的 `concludeTurn()` 正常结束。无工具的普通输出直接结束，不额外塞入状态导致循环继续。网页、提醒和 Skill 的组成见 [能力接入](DSH_CAPABILITIES.md)。

App 不依赖 DSH、模型 SDK 或其他 App 内部对象。新增 App 仍只需 Manifest、实现和配置。2026-10-28 经用户讨论授权，群聊默认改由主体组织正文并调用 `qq.reply_group`，不另调用 Planner/Replyer；原模板通过 QQ 场景适配复用。`qq.participate` 保留普通私聊和显式 legacy 群聊路径。详见 [群聊主体回复](QQ_SUBJECT_REPLY.md)。

## 配置与上下文

- 采用官方 `sdk-minimal`，将现有环境提示和人格原文作为 `DSH_SYSTEM_PROMPT`，不使用默认软件工程师身份。
- 通过官方 `@deepseek-ai/dsh-llm-pi-ai` 的自定义 OpenAI-compatible 路由使用现有 `ASTCHO_WORLD_MODEL/API_KEY/BASE_URL`。没有自建协议转换网关。
- `ASTCHO_WORLD_CONTEXT_WINDOW` 默认 65536；`ASTCHO_WORLD_MAX_TOKENS` 默认 8192。须按实际模型设置，前者参与 DSH 的上下文管理；这两个值不影响原聊天参数。
- 默认最多 20 次根决策步骤。DSH 自动压缩等辅助请求不计入这个步骤限额，因此它不是总请求数或费用上限。
- 使用独立 `<data-dir>/dsh/home` 和 `dsh/workspace`，不读取用户 `~/.dsh`。socket 目录和令牌仅本进程使用；API key 不写入生成的 patch。
- DSH 保存完整会话日志，后续活动和重启恢复相同会话；上下文过长时由 DSH 压缩。压缩只处理已经进入会话的内容，不读取 App 私有存档。会话缺失或恢复失败时报错，不静默创建新身份历史。
- 旧 native 的 `agent_history` 与 DSH 会话独立，当前没有自动转换。已有笔记、通知、App 数据和操作回执继续由同一个 World 管理。
- DSH 原始会话事件写入 `debug.dsh_event`，关联对应活动。DSH 日志和 World 数据均属于私有运行数据，不提交。

本次组合未开放 DSH 默认的 shell、文件与工作目录操作工具，避免绕过 App 读取边界；也禁用了 DeepSeek 额外请求日志与插件清单扩展。SDK 本身支持扩展这些能力，但接入后不会自动拥有它们。工作目录服务仍会提供独立 workspace 的运行上下文，不授予文件操作能力。

## 实测范围与限制

集成测试使用安装的真实 SDK 与原生 DSH，模型端是本机 HTTP/SSE 合成服务。覆盖工具发现和连续调用、笔记读写、保持安静、结束、多活动会话与重启恢复、动态状态、原人格逐字保留、未读 QQ 隔离、调用额度、结束批次后的动作阻止、结果未知不重放、持续服务的提醒唤醒、私有桥鉴权与取消清理。

已补充真实 SDK 的压缩/失败恢复测试及上下文维护，见 [上下文维护说明](CONTEXT_MANAGEMENT.md)。未完成真实提供商兼容性、长期摘要质量、进程被强杀后的各个崩溃窗口和长期运行验证。DSH 当前为预发布版，固定版本以便复现；升级前需重新验证持久化格式和插件 API。没有因此修改原聊天 prompt，也没有切换已有观察服务或启用 QQ 发送。

官方依据：[Python SDK](https://github.com/deepseek-ai/deepseek-harness/blob/master/python/sdk/README.md)、[sdk-minimal](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/bundle/sdk-minimal/README.md)、[pi-ai 路由](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/llm/llm-pi-ai/README.md)。实际 API 兼容性以固定版本的集成测试为准。
