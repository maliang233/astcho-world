# 本地原版兼容性基线

2026-10-27 由执行者直接检查 `../astcho-github` 当前工作区。
完整源文件与测试哈希见 `legacy-baseline.json`。`src/astcho` 和 `tests/legacy` 为本地文件逐字节复制，后续新增适配均在 `src/astcho_world`。不复制真实配置、密钥或运行数据。

## 基线检查

- 原目录执行 `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider`：100 passed，1 failed。
- 原目录执行 `.venv/bin/ruff check --no-cache src tests`：通过。
- 已有失败：`test_prompts.py::test_planner_prompt_preserves_decision_contract` 第 50 行期望的文案与当前本地 `planner_prompt` 不一致。保留两者，不能修改 prompt 或测试来掩盖。
- `HEAD` 只记录溯源；本地未提交改动属于本次基准。

## 代码对应与保护范围

下列行号均指本地原版；新项目 `src/astcho` 保持对应。

| 位置 | 直接调查到的契约 | 处理方式 |
| --- | --- | --- |
| `prompts.py` 全文件 | 人格、群聊 Planner、普通/Reasoning Replyer、私聊、转发、记忆、视觉、表情与表达学习提示 | 文件字节哈希保护，不改任何字符 |
| `services/expression.py:143` | 内嵌表达审核提示；relevant_hint 亦构建模型输入文本 | 整文件原样保留 |
| `services/vision.py:127` | 内嵌视频提示 | 整文件原样保留 |
| `services/chat.py:29` | Planner 使用独立客户端，temperature/max_tokens/thinking 为 None，user 消息 | 直接复用原 ChatService |
| `services/chat.py:78` | Reasoning 路径、普通路径与降级；role、请求组织、解析 | 直接复用，外层环境提示不注入 |
| `services/chat.py:143` | 私聊独立提示与清洗 | 保留，不用群聊代替私聊 |
| `services/llm.py` | thinking 控制、默认参数、JSON 校验与重试 | 整文件原样保留 |
| `services/attention.py:27` | 概率、日程、沉默、频率、观望与直接触发 | 通知机会调用原实现，不复制公式 |
| `services/attention.py:131` | 上下文格式、相对时间、引用和方向标记 | 打开后聊天仍使用原格式化方法 |
| `handlers/group.py:114` | 2–5 秒聚合、一次窗口的触发选择、Planner 与后续副作用 | 新入口负责通知；显式进入聊天只跳过第二次概率抽样 |
| `handlers/group.py:183` | 目标身份、情绪关系、记忆与回复生成 | 原执行函数作为集成适配器后端；原样保留 |
| `handlers/group.py:250` | 错字、分段、引用、间隔、表情发送和成功后记忆队列 | 不使用外层模型重写 |
| `handlers/private.py:18` | 每用户独立历史与私聊反馈入口 | 保留原入口；新环境暂不自动注册此 handler |
| `handlers/common.py` | @、昵称、引用解析、分段与发送消息编码 | 原样保留 |

## 已授权入口差异

- 旧触发机会在新 QQ App 中生成通知，不直接执行原 Planner。
- 已经明确选择参与时不再做第二次概率抽样。旧算法仍原封不动保存；适配器只替换这次调用的入口 gate。
- 免打扰过滤普通消息；明确 @/回复穿透。无文字的明确 @/回复也允许通知，这是用户指定通知逻辑的例外。
- 收到通知和忽略通知均不调用 `update_after_planner`；原 Planner 实际执行后才更新其状态。
- 新收件箱与观察快照独立。原上下文格式只处理显式观察快照，后续新消息不自动附加。
- 新环境不注册旧自动消息 handler，旧项目继续独立运行。

## 尚需单独处理的兼容冲突

1. `handlers/group.py:83–98` 在是否触发前做媒体学习与表达学习。新入口不会调用此自动处理链，避免未观察信息被学习。原服务代码与 prompt 均保存，接下来需以已观察消息驱动，验证去重与原节奏。
2. `handlers/group.py:99` / `_handle_custom_follow` 可直接发送跟风消息。保留原函数，但新环境不自动调用。后续接入必须变成明确动作，不能悄悄删除原规则或恢复后台自动发送。
3. 原私聊 handler 自动回复且包含管理员表达反馈。新 QQ App 目前只实现私聊的接收、通知和读取；私聊发送与该反馈入口迁移需要独立回放验证。
4. 原群聊执行函数会在发送之前更新部分情绪/关系。新适配器不得自动重试失败的参与动作；新事务/恢复路径仍需验证，不能宣称跨进程、跨外部平台恰好一次。

这些条目不通过改 prompt 解决。尚未完成的路径不用于生产替换。
