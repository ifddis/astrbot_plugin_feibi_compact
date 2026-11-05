# 菲比主动上下文压缩 1.0.3

针对 AstrBot **4.29.1、内置 AI（local runner）** 开发。提供真实的 `/compact` 指令及独立历史长度触发阈值，不需要修改人格提示词来假装执行压缩。

## 安装和首次使用

1. AstrBot 后台「插件 → 从本地文件安装/上传 ZIP」选择本安装包。如果当前面板不支持 ZIP，把包内 `astrbot_plugin_feibi_compact` 文件夹放到容器 `/AstrBot/data/plugins/`。不要多套一层同名文件夹。
2. 重载插件；安装后在插件配置中填写 `summary_provider_id`，使用 **DeepSeek 对话 Provider ID**（在模型提供商页面查看），**不能填 `openai_embedding`**。留空会优先采用 AstrBot 已配置的压缩模型，再回退当前聊天模型。
3. 在 `allowed_sessions` 填入目标群的完整 UMO（用 `/sid` 查看）；空列表表示所有启用本插件的会话都允许自动压缩。普通群成员没有手动压缩或恢复权限。
4. 首次建议把 `auto_enabled` 关闭，保存，先由管理员在目标群真正 @ 机器人发送 `/compact_status`，再发送 `/compact`。确认结果及普通聊天正常后再开启自动压缩。
5. 当前历史很长时会连续调用摘要模型，可能需要数分钟，并产生模型调用费用。期间该群正常聊天会等待会话锁；请勿连续触发。不建议第一次就用几十万 Token 的唯一历史做无备份测试；插件本身也会在调用模型前自动备份。

## 指令

| 指令 | 行为 |
|---|---|
| `/compact_status` | 查看当前数据库历史的 Token 估算、消息数、自动阈值和全局压缩状态 |
| `/compact` | 管理员立即压缩当前对话，等待本群正在处理的回复完成再执行 |
| `/compact_restore 文件名.json` | 管理员恢复本会话、本对话的指定备份，替换整个历史；恢复前再备份当前历史 |

这些指令在代码内部再次执行 `event.is_admin()` 检查；修改面板指令权限不会绕过该检查。管理员指 AstrBot 管理员，不是自动识别的 QQ 群管理员。

## 自动触发与默认值

- `auto_threshold_tokens=120000`：估算历史达到该值时，在**下一次正常 LLM 请求之前**触发；不是定时器，不在无人聊天时自动扫描，也不是按模型窗口的 82% 触发。
- `keep_recent_rounds=6`：按 user 消息边界，原样保留最近六轮。最近工具调用、结果跟随完整轮次保留，当前新问题和新群聊增量不纳入旧历史总结。
- `chunk_chars=24000`：每段最多 24000 字符；单次请求还附带上一份至多 12000 字符（默认安全上限）的滚动摘要及说明。应保证摘要模型真实输入窗口足够。
- `summary_max_tokens=3000`、`summary_max_chars=6000`：摘要输出预算和目标字符数。字符安全上限为目标的两倍，超过才最多两次精简；仍超限、空摘要或没有缩小历史，均拒绝提交。
- `max_summary_calls=100`、`request_timeout=120`、`total_timeout=7200`：每次运行调用预算（含精简）、单次请求超时及总超时。已有配置1800不会自动覆盖，请手动调到7200。超大历史可先选更大的输入分块，但必须匹配摘要模型窗口。
- `cooldown_seconds=300`：同一对话自动尝试冷却，失败也冷却。
- 全部群合计只执行一个摘要链，避免同时增加多个摘要任务的压力。这不是所有聊天请求的全局并发限制。

Token 使用的是本插件的字符类型估算（ASCII 约 1/3、其他字符约 1.5，图片/音频有固定估算），**不是 DeepSeek 的分词器计数或计费数字**。估算包含保存的历史文本及工具调用，**不包含本次新输入、独立人格提示词和本次工具定义**。不要用此阈值代替模型真实的 `max_context_tokens` 配置；不修改你原来的模型窗口。

## 数据与备份

压缩前历史保存到：

```
/AstrBot/data/plugin_data/astrbot_plugin_feibi_compact/backups/
```

目录通过 `StarTools.get_data_dir` 获取，若数据根目录不同，以实际目录为准。已有 Docker `/AstrBot/data` 挂载会把文件保存到对应宿主机目录。备份包含原始聊天文本和媒体引用，请按聊天记录保管。

默认保留全部群合计最近 **20 份**备份；成功落盘新备份后轮换最旧的超额备份，失败摘要的备份也计入。它不是无限归档，请自行备份重要数据。恢复使用报告中的完整文件名，不支持跨会话恢复；恢复点之后的新聊天将不在恢复内容里，但恢复前会再备份当前历史。

写入采用 AstrBot 会话锁，并在提交前比较对话 ID 和历史快照。摘要 API 错误、超时或历史发生外部更改时，不提交半成品。插件卸载时取消正在执行的摘要调用；备份不会随卸载主动清空。

## 与现有插件和内置压缩共存

- **LivingMemory**：本插件不改写它的长期记忆数据库，也不代替其总结；只压缩 AstrBot 当前对话历史。两套系统都可能遗漏细节。请先确认重要内容已成功记入长期记忆。
- **group_context_flow**：自动处理仅在请求历史以数据库历史为完整前缀时生效，保留末尾新增群聊消息；如果其他插件重排或清理了历史导致无法对齐，则记录警告并跳过，管理员可用 `/compact` 单独操作。
- **内置压缩**：可以保留作真实模型窗口的后备机制。把内置“最大携带对话轮数”设为 `-1` 可避免旧历史先按条数被丢弃；应按你的需求决定，不要为本插件改大真实模型窗口。
- 不修改人格、管理员名单、模型配置、定时任务。摘要不授予任何权限。
- `/compact` 不会经过一次普通人格聊天，不需要让模型“自行理解”这个指令。

## 限制

- 仅核对并声明支持 4.29.1 所在 4.29 系列，未在用户实际服务器运行验证；第三方 Dify/Coze runner 不支持。
- 旧轮次的图片、音频只写“多媒体”占位，不下载、识别或转发原图；原始引用在备份里，最近轮次仍原样保留。
- 摘要是有损的。大量历史压为少量摘要不保证记住所有群友梗、字句和细节；可增加摘要预算或保留轮数，但仍会有损。
- 初次长历史需要多次远程模型调用；没有本地大模型和额外 pip 依赖，但备份、解析历史和文本处理仍占 RAM/云盘，**不能把它当作 OOM 的完整解决方案**。先观察你已有的 feibi-monitor。
- 当摘要失败时，自动模式继续原来的聊天请求；内置压缩仍可能按它自己的配置执行。
- 运行中不要同时在后台重置或编辑相同对话。插件内比较能检测部分外部变更，但不能使所有其他插件和外部数据库写入原子化。

## 排查和验证

查看日志（Ubuntu）：

```bash
sudo docker logs --since 10m --timestamps astrbot 2>&1 | grep -E 'FeibiCompact|Traceback|ERROR'
sudo docker stats astrbot --no-stream
```

成功日志包含 `start`、每段 `summarized_chunk`、最终 `committed`，显示压缩前后估算和调用次数。不在日志中输出历史正文或 API Key。

测试管理员 `/compact_status` → `/compact` → `/compact_status` 的前后数量，再正常聊天；普通用户 `/compact` 应被拒绝。若命令与其他插件重名，应先禁用对方的同名指令。

本包附带 `tests/test_compact.py`，在插件目录的父目录运行：

```bash
python -m unittest discover -s astrbot_plugin_feibi_compact/tests -v
```

已通过 18 项标准库 unittest 测试：分块边界、完整最近轮次/工具、空和超长摘要拒绝、API 中途失败、调用上限、媒体数据不传摘要模型、权限、会话锁、增量保留、外部变更拒绝提交、备份和恢复作用域。框架交互使用模拟对象，不等于真实 QQ / DeepSeek 集成验证。

## 核对过的 AstrBot 源码

- https://github.com/AstrBotDevs/AstrBot/blob/v4.29.1/astrbot/core/utils/session_lock.py
- https://github.com/AstrBotDevs/AstrBot/blob/v4.29.1/astrbot/core/pipeline/process_stage/method/agent_sub_stages/internal.py
- https://github.com/AstrBotDevs/AstrBot/blob/v4.29.1/astrbot/core/conversation_mgr.py
- https://github.com/AstrBotDevs/AstrBot/blob/v4.29.1/astrbot/core/provider/provider.py

实现沿用 AstrBot 的 Provider、会话锁与历史更新接口；超长分块摘要由插件实现，未直接调用会在失败后丢弃历史的内置兜底截断流程。

## 1.0.2 断点续跑与排查

每块成功后，将滚动摘要和已处理历史的指纹原子写入 `data/plugin_data/astrbot_plugin_feibi_compact/checkpoints/`（容器内通常以 `/AstrBot/` 为前缀）。最多保留最近20个会话断点，包含聊天摘要，请妥善保管数据目录。最终提交成功才删除对应断点。

超时、网络失败或重载后，再次 `/compact` 会先校验断点。已处理的文本前缀、分块和摘要设置匹配时，从下一块继续；新增未处理消息通常不影响续跑。若最后已处理块是末尾不满块且追加内容改变了该块，或旧历史/设置改变，则安全地从头开始。本插件不会将半成品摘要写入正式聊天历史。旧版1.0.1未存断点，因此升级后的首次运行仍从头开始。

`summary_max_chars=6000` 是目标，最多接受12000字符，不截断摘要；超过安全上限才最多两次精简。所有模型请求计入每次运行的调用预算。建议首次百万Token估算历史设置 `total_timeout=7200`，保持 `chunk_chars=24000`，避免扩大分块超过摘要模型窗口。若超时，可再次手动续跑；不会自行后台无限重试。

日志 `summarized_chunk=4/52` 是整体进度，`resumed_chunk=24/52` 是恢复断点，`shortening_chunk` 是精简。`auto_check` 记录历史估算和阈值，`auto_skip` 记录冷却、未授权、关闭、其他压缩任务占用等原因。请求历史不匹配数据库前缀时仍拒绝自动改写，需检查其他插件或手动压缩。

本插件阈值独立于AstrBot自带压缩和真实模型窗口。失败的自动尝试仍保留原请求，不保证其一定能在模型窗口内；请检查失败日志。上下文压缩也不能替代服务器内存和并发控制。

## 1.0.3 分段精简兜底

整份摘要精简两次仍超安全上限时，将完整候选分成最多4000字符的小段，逐段精简后拼合；最多3轮，且必须缩短，所有请求仍计入 max_summary_calls 和超时预算。不会截断正文，也不能保证不遵守精简要求的模型一定成功；无法缩短时建议更换 summary_provider_id。更换Provider不改变断点指纹，但不要修改 chunk_chars、keep_recent_rounds、summary_max_chars，否则断点可能失效。

兼容1.0.2已完成块的断点（未修改v2指纹及摘要指令）。手动或自动尝试结束时启动冷却，防止长任务失败后立即自动重复。超时信息区分 request_timeout 和 total_timeout。日志 reducing_chunk=N/T round=R/3 piece=P/Q 表示分段精简。

本版本新增回归测试；当前执行环境不可用，新增测试尚未运行。测试使用模拟框架，不能替代真实AstrBot集成验证。停止插件时最多等待取消任务10秒，已停止任务不得提交历史。
