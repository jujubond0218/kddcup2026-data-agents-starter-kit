# Phase 1 确定性答案 Verifier

本次改造在原生 `answer` 工具调用后、Agent 终止前增加了一个无状态、无 LLM 的
Verifier。它不读取 gold，不修改评分规则，也不尝试判断计算结果的语义正确性；职责仅是
阻止无法可靠写入 `prediction.csv` 的候选表被当作成功答案。

## 终止与纠错流程

```text
assistant tool_call(answer)
  → Pydantic 参数校验
  → 候选 AnswerTable
  → 确定性 Verifier
      → 通过：设置 state.answer，Runner 写入 prediction.csv
      → 拒绝：ANSWER_VERIFICATION_ERROR 作为 role=tool 返回模型
               → 模型在剩余步骤中修正后重新调用 answer
```

候选答案仍是当前 inline `AnswerTable`，本 PR 不引入 `from_csv`、候选 artifact manager、
LLM 审计、回退到被拒绝候选答案或视频约束。被拒绝的 `answer` 调用消耗正常 ReAct step；
若在 `max_steps` 内没有通过验证的答案，任务按现有失败路径结束，Runner 不会写出
`prediction.csv`。

## 确定性检查

Verifier 拒绝以下情况：

- 去除首尾空白后为空或重复的列名，以及列名或字符串单元格中的控制字符；
- 行宽与列数不一致；
- 嵌套对象等非 CSV 标量单元格，或 `NaN` / `Infinity`；
- 任意一整列均为 `None` 或空字符串；
- 使用 Runner 同一 CSV 读写规则后无法完整 round-trip 的表格。

零行答案不因 Verifier 而被拒绝；题目单复数、答案列是否足够、输出是否命中标准答案等
语义问题继续由模型推理和本地 scorer 在实验后衡量。

## Trace、事件与测试

验证通过会写入 `answer_verification_passed`，拒绝会写入
`answer_verification_rejected`，两者都带有原生 `tool_call_id`、行列数；拒绝同时保留既有
`tool_failed` 事件，错误码为 `ANSWER_VERIFICATION_ERROR`，具体规则在
`verification_code` 中。`trace.json` 继续通过该 step 的 observation 保存同一错误，因此旧
Trace 读取逻辑无需迁移。

单元测试覆盖每条结构规则、合法 CSV round-trip 与零行答案；scripted ReAct 测试覆盖
拒绝后以同一 call ID 纠正、步骤耗尽和成功终止；Runner 集成测试确认只会写出最终通过的
预测文件。完整自动化测试共 70 项，pytest、Ruff 静态检查、格式检查、lock 检查和
`git diff --check` 均通过。

## 全量实验与结果解释

本 PR 使用与上一轮原生工具调用正式实验相同的模型和运行参数完成了 50 题全量实验：
`qwen3.5-35b-a3b`、`temperature=0`、`max_steps=16`、`max_workers=2`、20 秒模型请求
超时、一次应用层重试和 120 秒任务硬超时。运行产物、模型响应、Trace、预测和本地配置
继续只保存在 ignored 目录中。

| 指标 | 原生工具调用上一轮 | 加入 Verifier 本轮 |
| --- | ---: | ---: |
| 墙钟时间 | 887.745 秒（14 分 48 秒） | 863.205 秒（14 分 23 秒） |
| Runner 成功 / 失败 | 38 / 12 | 40 / 10 |
| 总分 / Mean Recall | 0.5303 / 0.5333 | **0.5703 / 0.5733** |
| 满分 / 部分匹配 / 无匹配 / 缺失 | 25 / 3 / 10 / 12 | 27 / 3 / 10 / 10 |
| 额外列总数 | 14 | 15 |
| 任务硬超时 | 4 | 3 |

本轮 40 次成功 `answer` 全部产生了 `answer_verification_passed`，每次都能与同一
`tool_call_id` 的终止事件和最终预测一一对应；没有出现
`answer_verification_rejected`。因此，本轮确认了 Verifier 确实位于最终写出之前，且未
观察到运行或得分负担，但没有在真实任务中触发纠错闭环。

总分比上一轮提高 0.0400，任务级表现为 4 题得分提高、2 题退化、44 题得分不变。由于
所有候选答案都直接通过验证，Verifier 没有向模型返回任何新观察，也没有改变答案内容，
所以这次分数变化不能归因于 Verifier，更合理的解释是真实模型服务和 Agent 轨迹的波动。
本 PR 的实证结论是增加了确定性的结构安全边界而未观察到负担，而不是已经证明提高了答案
语义正确率。若要量化其直接收益，需要在真实结构错误出现时统计挽救任务数，或另做受控的
非法候选注入实验。
