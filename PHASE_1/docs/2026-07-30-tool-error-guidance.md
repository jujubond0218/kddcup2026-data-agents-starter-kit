# Phase 1 Tool Error Guidance

本改造只丰富主 Agent 已有工具失败后的 observation，不改变工具注册、参数 schema、
Explorer、Agent 循环、Verifier、Scorer、ETL、数据或 gold。目标是让模型在文件类型或
路径选择错误后，下一轮直接纠正，而不是根据底层异常自行猜测。

## 基线问题与固定任务

2026-07-29 的 50 题 Explorer 实验中，主 Agent 有 13 次对真实非 SQLite 文件执行 SQL，
另有 2 次 SQL 路径不存在。多数任务没有重复同一个 SQL 调用，但固定的三题出现了相邻
失败：

- `task_74`：错误 SQL → 对同一非 SQLite 文件检查 schema → Python 语法错误 → Python
  成功；
- `task_80`：错误 SQL → 对同一非 SQLite 文件检查 schema → Python 成功，最终耗尽
  20 步；
- `task_330`：错误 SQL → Python 运行错误 → Python 成功。

对应回归任务清单位于 `configs/tool_error_guidance_tasks.example.txt`。旧 observation
统一使用 `TOOL_EXECUTION_ERROR`，只透传 `file is not a database`、SQL syntax error
或 `Missing context asset` 等底层异常，不提示正确工具、`list_context` 或此前 Explorer
报告。

## 轻量错误接口

工具注册和输入 schema 保持不变。Registry 捕获 handler 异常后，只增加以下语义映射：

- 路径不存在时返回 `PATH_NOT_FOUND`，提示不要猜测或重试相同路径，建议
  `list_context` 或复用 Explorer `files/recommended_sources` 中的精确路径；
- SQL/schema 工具的目标文件真实存在但不是 SQLite 时返回 `NOT_SQLITE`，根据扩展名
  建议 `read_csv`、`read_json`、`read_doc` 或 `execute_python`；
- 两类 observation 增加 `guidance`、`suggested_tools` 和
  `do_not_retry_same_call: true`；其他 handler 异常继续返回原来的
  `TOOL_EXECUTION_ERROR`。

文件类型判断只用于选择错误说明。真实 SQLite 上的 SQL 语法错误不会被误分类为
`NOT_SQLITE`。本改造不能阻止第一次错误，也不会隐藏 SQL 工具；它只改善下一轮恢复。

## 自动化验证

Registry 单元测试覆盖非 SQLite CSV、缺失 JSON 路径和真实 SQLite 语法错误。
Scripted Agent 测试验证完整 `NOT_SQLITE` observation 会带匹配的 `tool_call_id` 进入
下一次模型请求，并可在下一轮改用 `read_csv`。实现后的全量测试为 `104 passed`，
Ruff 检查通过。

## 三题真实对照

2026-07-30 使用相同本地目标配置重跑固定三题。三题都再次触发对非 SQLite 文件执行
SQL，并收到带 guidance 的 `NOT_SQLITE`；三题下一轮均成功执行了非 SQLite 读取方式：

三题的 context 都没有 SQLite 文件。错误 SQL 的目标均为 Explorer 推荐的 CSV：
`task_80` 与 `task_330` 下一轮 `read_csv` 使用同一精确路径，`task_74` 下一轮 Python
代码也读取同一 CSV。因此这里的正确纠正是换成适合 CSV 的读取方式，而不是寻找并不存在
的 `.db` 后继续执行 SQL。

| 任务 | 旧序列 | 新序列 | Runner / 评分 |
| --- | --- | --- | --- |
| `task_74` | SQL 失败 → schema 失败 → Python 失败 → Python 成功 | SQL 失败 → Python 成功 | 成功，满分；总步骤 8 → 6 |
| `task_80` | SQL 失败 → schema 失败 → Python 成功 | SQL 失败 → `read_csv` 成功 | 120 秒超时，缺失；纠错目标通过 |
| `task_330` | SQL 失败 → Python 失败 → Python 成功 | SQL 失败 → `read_csv` 成功 | 成功，满分 |

task_330 本轮在 Explorer 之前另有两次与本改造无关的未知工具名错误，因此总步骤由 9
变为 10，不能用总步骤评价本改造。task_80 在第三步成功读取 CSV 后触发任务硬超时，
说明 observation 已实现直接纠正，但 Runner 结果仍受真实服务耗时影响。

本次单轮真实对照支持“错误后的下一轮纠正”这一局部目标：3/3 均从旧版的相邻失败变为
下一轮工具成功。它不证明稳定分数提升，也不能替代重复实验或 50 题对照。

## 50 题全量实验

2026-07-30 使用正式 50 题、`max_steps=20`、`max_workers=4`、20 秒模型请求超时和
120 秒任务硬超时完成一次全量运行，run ID 为 `20260730T021213Z`。运行期间按 10 分钟
间隔检查终端：第 10 分钟完成 45/50，其中 39 个成功、6 个失败、4 个运行中、1 个排队；
第 20 分钟检查时整轮已经结束。事件时间戳显示实际墙钟时间为 752.582 秒
（12 分 32.582 秒），50 题累计执行时间 2708.178 秒，中位数 40.546 秒，P95 为
120.141 秒。

Runner 最终成功 40/50。评分为 **0.6888**，36/50 题取得非零分，32 题完美匹配、
4 题部分匹配、4 题无匹配、10 题缺失预测。作为非严格参照，改造前 2026-07-29 的
并发 4 单轮运行 `20260729T092153Z` 为 38/50 Runner 成功、评分 0.5903、31 题非零、
12 题缺失；两次真实服务运行存在随机性，本轮差值不能单独证明错误提示带来语义分数
提升。

本轮主 Agent 共触发 10 次 `NOT_SQLITE` 和 2 次 `PATH_NOT_FOUND`。两类错误之后的
下一轮工具调用 12/12 全部成功：`NOT_SQLITE` 后 8 次改用 `execute_python`、2 次改用
`read_csv`，`PATH_NOT_FOUND` 后分别改用 `list_context` 和正确路径的 `read_json`。
没有任务原样重试相同失败调用。全轮 143 次 `execute_python` 没有工具执行失败，因此
没有出现 Python 结构错误后连续打转。本结果支持本 PR 的局部目标，但第一次错误仍然
存在；轻量 observation 不会隐藏 SQL 工具或提前阻止错误选择。

## Bad case 记录

10 个缺失预测按终止原因分为三类：

- `task_11`、`task_19`、`task_38`、`task_80`、`task_173` 触发 120 秒硬超时。
  `task_11`、`task_19`、`task_38` 最后活动为尚未返回的 `execute_python`；
  `task_80` 与 `task_173` 最后活动为第 18、20 步模型请求。相比改造前单轮的 4 个
  硬超时，本轮为 5 个，运行可靠性没有随分数同步改善。
- `task_344`、`task_396`、`task_418`、`task_420` 耗尽 20 步。它们的工具调用大多
  成功，但在多次 SQL、文档读取或 Python 计算后仍未收敛到 `answer`，属于主 Agent
  停止与任务求解问题，不是本 PR 处理的错误 observation 循环。
- `task_379` 的模型请求在两次尝试后超时。全轮共有 521 次成功请求、3 次失败请求和
  2 次重试；成功请求耗时中位数为 2.736 秒，P95 为 14.793 秒。

有预测但无匹配的任务为 `task_25`、`task_89`、`task_163`、`task_169`；部分匹配为
`task_27`、`task_180`、`task_259`、`task_355`。其中 `task_25` 的缺失路径错误已在
下一轮成功恢复，但最终仍然零分，说明工具恢复正确不等于答案语义正确。全轮还出现
3 次非法工具调用和 4 次未知工具名，以及真实 SQLite 上 4 次 SQL
`OperationalError`；这些调用均在下一轮执行了成功工具，但不属于本 PR 新增的两类
错误映射。

因此，全量实验能够支持的结论仍限定为：`NOT_SQLITE` 和 `PATH_NOT_FOUND` 的结构化
observation 在本轮实现了 12/12 下一轮成功纠正，并消除了相同失败调用的紧邻重复。
单轮分数上升、硬超时变化和其他工具选择不能归因于这项轻量改造。
