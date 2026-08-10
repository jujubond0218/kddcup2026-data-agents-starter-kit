# Phase 1 模型请求输入指标

## 目标

Python stdout/stderr 有界后，真实任务仍可能因为多轮历史、Explorer 报告、文档预览或工具
schema 累计而增长。只观察服务端最终拒绝无法判断是哪一部分占用上下文，也无法诊断在请求
发出前就被硬超时终止的任务。本改造只增加数值遥测，不截断消息、不压缩历史、不修改 Prompt
或工具行为。

初步基线说明不能先假定 SQLite 是主要来源。`task_344` 的一次运行中，8 条 SQL observation
合计约 3.0 KB，而单条 Explorer observation 约 18.7 KB；`task_396` 的 21 个步骤累计约
47.8 KB observation，服务端报告的 prompt tokens 从 705 增长到最高 17,730。三次
`task_250` 均成功且满分，但都没有再次生成历史上的巨型 Python 输出。这些都只是单轮或
少量重复观测，用于确定测量方向，不用于声称稳定收益。

## 记录内容

`OpenAIModelAdapter` 在把最终请求字典交给 SDK 前，对同一个字典生成紧凑、非 ASCII 转义的
UTF-8 JSON 计数。每次尝试的 `model_request_started` 事件新增 `input_metrics`：

- `payload_bytes`：完整请求字典的紧凑 UTF-8 字节数；
- `message_count`、`messages_bytes`、`max_message_bytes` 和 `max_content_bytes`；
- `messages_by_role`：每个角色的消息数、序列化字节数和 content 字节数；
- `tool_schema_count` 与 `tool_schemas_bytes`。

指标不包含任何消息文本、工具 observation、tool call 参数或 schema 原文。请求发生重试时，
每次 attempt 都会先写入相同口径的输入指标，因此任务随后被强制终止也能恢复最后一次请求的
大小。`model_request_succeeded.usage.prompt_tokens` 仍是服务端可用时的实际 Token 口径；
本地字节数用于稳定拆解来源，不冒充模型 tokenizer 的精确 Token 数。

## 边界与后续决策

该指标衡量传给 SDK 的 JSON 请求体，不包含 HTTP header、TLS 或服务端额外包装。按角色统计
可以分离 system、user、assistant 和 tool 消息，但不会进一步解析或保存敏感内容。工具
schema 独立统计；完整 payload 与各子项之和可能因 JSON 容器、字段名及固定请求参数而不同。

只有测量显示某类工具经常产生大 observation 时，才给该工具增加保留结构和截断元数据的
硬上限；只有单次 observation 已经有界而多轮历史仍逼近模型上限时，才评估历史压缩。自动
summary 不在本改造范围内，因为它会引入语义损失、额外模型调用和 tool call 配对风险。
