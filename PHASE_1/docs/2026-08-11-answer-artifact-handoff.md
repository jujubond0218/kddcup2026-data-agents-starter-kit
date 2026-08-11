# Phase 1 大结果 CSV Artifact 交付

## 问题与范围

原有 `answer(columns, rows)` 要求模型在终局工具参数中逐行生成完整结果。对于数百到数千行的
答案，这会把模型同时当成计算控制面和表格数据通道：即使 Python 已经算出完整候选，模型仍
可能只复制 stdout 预览、在生成过程中截断，或为重复的大段 JSON 消耗额外 completion。

本改造只处理 LLM 到运行时的最终结果交付。它不改变任务计算、Explorer、Evidence Plan、
Scorer 或语义判断，也不修改输入 CSV 的读取方式。第一版将 artifact 载入现有
`AnswerTable`，继续保留 `trace.answer` 并由 Runner 写出 `prediction.csv`，因此只能称为
“LLM 侧有界交付”，不能称为 zero-copy、全链路流式或常量内存。

## 协议

`answer` 保持唯一终止工具，并提供严格互斥的两种输入：

```json
{"columns":["value"],"rows":[["1"]]}
```

```json
{"from_csv":"answer.csv"}
```

内联答案达到 20 行或 100 个数据单元格时返回可恢复的
`INLINE_ANSWER_TOO_LARGE`。只要当前 attempt 的规范 artifact 已经存在，任何内联提交都会
返回 `ANSWER_ARTIFACT_AVAILABLE`，防止模型把已有完整文件的前几行重新当作答案。

Runner 为每次 task attempt 创建独立临时目录，并向 `execute_python` 命名空间注入固定
`Path`：`answer_csv_path`。模型把完整结果写到该路径后，只提交固定相对 handle
`answer.csv`。输入文件仍限制在 `context/`；生成答案属于另一项仅指向当前 attempt 临时目录
的 capability，不能引用绝对路径、`..`、其他 attempt 或任意 `context/` 文件。
`answer_csv_path` 是只读保留名；代码在执行前经 AST 检查，给该变量赋值或删除会被明确拒绝。
这不把 `execute_python` 变成安全沙箱：模型仍可写其他任务内路径。父进程会记录 attempt 开始
前 `context/answer.csv` 是否存在，并只清理由本次错误路线新生成的同名 spill；预先存在的真实
输入不会删除或被 artifact loader 接受。

## 有界加载与校验

加载器只接受当前 attempt 根目录中的普通、非符号链接 `answer.csv`，并在解析前执行 5 MiB
硬上限。CSV 使用 `utf-8-sig` 和标准库 `csv.reader` 读取，允许 UTF-8 BOM、引号逗号与合法
换行，拒绝缺失、目录、符号链接、非法编码、空文件、语法错误和不等宽行。所有单元格都保留
为字符串，不做 dtype 推断，因此前导零不会丢失。

合法 CSV 转成原有 `AnswerTable` 后，仍经过 Explorer projection gate 和确定性
`AnswerVerifier`。重复或空表头、控制字符和全空列等规则仍由 Verifier 统一处理；拒绝时使用
原始 `tool_call_id` 返回可恢复 observation，文件保留供下一次 Python 调用覆盖。父进程在
成功、失败、异常和硬超时后统一清理 attempt 临时目录。

Python observation 只报告固定 handle、字节数和提交提示，不回传行内容。专用
`answer_artifact_detected/submitted/rejected` 事件只记录 step、call ID、行列数、字节数、
SHA-256 或错误码；终局参数大小不随答案行数线性增长。

## 自动化边界

自动化测试覆盖双模式互斥、20 行/100 单元格边界、10,000 行端到端保真、UTF-8 BOM、引号
字段、前导零、ragged rows、错误编码、空文件、5 MiB 边界、固定 handle、符号链接、跨
attempt 清理、失败重跑和四个并行 attempt 隔离。真实模型实验只使用四题目标集，结果需要
与同期三轮基线分别报告；它不能替代 50 题回归，也不能证明 task_199 的聚合语义已解决。

## 实验结果

在最终实现上固定 `300s task timeout / 4 workers / 20 steps / 20s request / 1 retry /
temperature=0`，对 `task_38/86/199/250` 连续运行三轮，run ID 为
`20260811T024100Z`、`20260811T024313Z` 和 `20260811T024447Z`。同期 master 基线三轮为
`20260811T020632Z`、`20260811T020924Z` 和 `20260811T021156Z`。

基线 Runner 成功 9/12；功能组为 12/12。功能组三轮目标集得分和分别约为
1.9167、2.9250、2.9250（按四题计算的 12 个样本均值约 0.6472），基线三轮分别为
1、1、2（均值 0.3333）。这些是四题同期观测，不是 50 题 benchmark，也不能单独归因于
artifact：模型路线有明显波动，`task_199` 仍有两轮 `no_match`，唯一 `perfect_match` 轮使用
的是小型 inline 答案。

真实 artifact 共成功提交 4 次：`task_38` 三轮分别为 140×6、140×4、140×4，`task_199`
一轮为 454×2。两次先提交空 handle 的错误都在同一任务内恢复，没有 artifact 专属未恢复
失败。四次事件行列数、`trace.answer` 与 `prediction.csv` 的列和值全部一致；校验后的终局
action input 均为 48 字节，而按同一答案重建的等价 inline 参数为 5,424–13,710 字节，缩小
约 113–286 倍。三轮结束后 attempt scratch、`task_38/context/answer.csv` 和
`task_199/context/answer.csv` 残留均为 0。

`task_86` 和 `task_250` 三轮都保持 inline，未出现由新 schema 或门禁造成的 missing；
`task_86` 在基线与功能组都呈现两轮 `no_match`、一轮 `perfect_match`，`task_250` 六轮均为
`perfect_match`。对功能组首轮 `task_199` 的 454 行完整 prediction 与前 20 行版本离线评分，
两者都为 0，因此该轨迹只证明完整性交付和协议保真，不能证明 task_199 语义得分提升。
