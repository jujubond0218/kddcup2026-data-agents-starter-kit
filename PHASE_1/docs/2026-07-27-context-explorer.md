# Phase 1 Context Explorer

当前实现采用与参考项目 Phase 1 发现流程一致的子 Agent 工作流，不再在主 Agent 请求模型
前自动生成或注入 Inventory，也不再由本地规则判断“歧义”。启用 Explorer 时，主 Agent
首轮只开放无参数 `explore({})`；子 Agent 的首个成功轮次必须单独调用
`inspect_files({})`，第二个成功轮次必须单独调用 `lock_requirements` 锁定题目需求及真实
候选来源，随后才能按需调用 `preview_file`、`grep_context` 和只读
`execute_context_sql`，最后以独占一轮的 `report` 提交数据地图。完成或 fail-open 后，
主 Agent 永久移除 `explore` 并恢复原有工具。

## 子 Agent 工具与数据地图

`inspect_files` 是确定性、有预算的子 Agent 工具，覆盖 CSV/TSV、JSON、SQLite、
Markdown、文本和文本型 PDF，返回相对路径、类型、大小、schema、行数、字段画像、
文档标题、warning 和候选关系。它只负责收集事实，不判断是否存在歧义，也不直接把
`knowledge.md` 正文放进结果；如果扫描发现一个或多个大小写不敏感的 `knowledge.md`，
子 Agent 必须对每个文件显式调用 `preview_file`。未尝试读取会以可恢复的
`KNOWLEDGE_NOT_REVIEWED` 拒绝报告；读取失败则保留 warning evidence 并允许继续报告。

`lock_requirements` 把问题拆成最多 12 个稳定的小写需求 ID，覆盖实体、指标、过滤条件、
时间范围、输出字段、知识规则和连接关系。每项需求只能声明 inspect 已发现的候选路径和
字段，并明确 `needs_discovery`；一个需求出现多个真实候选字段时即表示显式待消歧状态，
不能把命名相似直接当作已确认语义。运行时会自动把多候选字段规范为待探索需求，并为
inspect 发现但模型漏列的 `knowledge.md` 补充读取需求，避免子 Agent 为协议 bookkeeping
反复重提整份计划。锁定成功后计划不可修改，事件只记录需求、候选、规范化与歧义数量，
不记录题目文本或完整计划。

`preview_file` 对 inspect 已发现且已锁定为候选的单个文件做更深入但有界的读取。
`grep_context` 在一个已锁定的文本来源或 SQLite 文本列中执行大小写不敏感的有界正则
搜索，不再接受无路径的全局搜索。Explorer 内的 `execute_context_sql` 仅允许单条
`SELECT`、`WITH`、只读
`PRAGMA` 或 `EXPLAIN`，最多返回 200 行，并通过只读连接、`query_only`、语句校验和执行
时限共同拒绝写入、ATTACH、建索引及长时间查询。

每次 `preview_file`、`grep_context` 和 `execute_context_sql` 都必须携带一个到四个已
锁定的 `requirement_ids`、简短 `purpose`，以及本次实际检查的候选 `target_fields`。
运行时仍拒绝计划之外的路径和新 ID，但会丢弃未锁定的 `target_fields`；当模型漏写字段
且当前路径存在锁定候选时，运行时自动绑定这些候选，避免把可确定修复的参数遗漏变成额外
模型轮次。每项需求的深层调用仍限制为三次。模型
调用 `report` 时只提交语义增量：`relevant_evidence`、`requirement_resolutions`、
`selected_sources`、`field_semantics`、`knowledge`、`etl_candidates`、`join_paths`、
`warnings` 和 `uncertainties`；`task_requirements`、文件清单、schema 与 evidence 来源由
运行时合并，模型不能在最终报告中重写。一项
`relevant_evidence` 只有在 evidence 存在、工具成功、支持的 requirement 已声明，且与
工具调用时记录的 requirement 绑定一致时才会被接受。

运行时确定性跟踪需求覆盖度：`needs_discovery=false` 的需求由 inspect 满足；普通发现
需求至少需要一条成功且绑定一致的深层 evidence；有多个候选字段的需求必须实际覆盖所有
候选字段，或由绑定到该需求的 `knowledge.md` evidence 支持。覆盖完成后子 Agent 只再看到
`report`，避免对已满足需求继续交叉检查。在必须读取的 knowledge 已尝试后，未完全覆盖时
也会同时开放 `report`，允许模型把无法继续判定的需求标为 `unresolved` 后及时结束，而
不是为了满足机械覆盖条件耗尽轮次。`requirement_resolutions` 只能在已锁定候选中
选择或排除字段，并引用已经选中的相关 evidence；无法确定时必须保留为 `unresolved`。

运行时始终生成全文件的极简背景 `files/schema_map`；未选择来源最多保留 inspect 得到的
16 个基础字段。只有被 `relevant_evidence` 接受的 preview/grep/SQL observation 才能
进入 `evidence_summaries`、深层 schema 和 `value_samples`。inspect 产生的弱
relation candidates 不再自动变成 `join_paths`；连接必须由子 Agent 明确选择并引用相关
证据。未知路径、字段、需求或 evidence 引用只会忽略对应语义项并产生 warning，不会让
背景数据地图整体失效。所有连接始终标记为 candidate；`etl_candidates` 仅是咨询性发现，
本 PR 不执行 ETL，也不因没有 ETL 产物拒绝报告。

## 预算、协议与 fail-open

Explorer 默认最多 10 个普通模型轮次，软墙钟上限为 60 秒；10 轮是复杂任务的硬上限，
不是期望平均值。运行时在达到 70% 和 90% 轮次预算时分别注入收敛提醒并记录聚合事件。
`inspect_files`、`lock_requirements` 与 `report` 必须各自独占一轮；中间轮次最多包含两个独立发现工具调用，
运行时按原顺序执行，并为每个调用返回匹配原始 `tool_call_id` 的独立 observation。子
Agent 每轮只看到当前阶段合法的工具：首轮只有 `inspect_files`，第二轮只有
`lock_requirements`，没有 SQLite 来源时不暴露 SQL；knowledge 读取完成后，`report`
与仍可用的定向发现工具同时开放，需求覆盖完成或最后一轮时则只开放 `report`。若最后
一轮没有调用 report，或 report 参数/语义契约
被拒绝，运行时最多再请求两次只允许 report 的纠正响应；这些请求不增加
`steps_used`，但仍受 60 秒软时限、模型请求超时和 usage/事件记录约束。

`explore` 和 `inspect_files` 的公开 schema 仍为空对象；对于部分 OpenAI-compatible
模型为无参数工具生成的无意义占位字段，运行时仅在这两个空输入边界丢弃字段，避免参数
形状错误阻止 fail-open。主 Agent 的“一轮一个工具”协议不变。

扫描默认最多处理 64 个文件、总读取 4 MiB、普通单文件 256 KiB，文本型 PDF 最多读取
2 MiB 和前三页；inspect 结果最多 12,000 字符，单个 preview 最多 2,000 字符，正常报告
最多 4,000 字符且 preview 最多两次。所有路径限制在任务 `context/` 内。损坏文件、非法
正则、越权路径、超限与不支持输入返回结构化可恢复错误或 warning。

正常 report 与 fallback 共用同一个确定性投影器。若模型失败、10 轮及免费终止重试内
没有合法 report、子工具失败或到达 60 秒软时限，fallback 从成功且带 requirement 绑定
的深层 observation 中优先保留 knowledge 读取、每个“需求 × 工具”组合的最新 evidence，
再按新近程度补足，最多选择 8 项。它直接复用不可变的 locked requirements，并为每项
需求生成显式 `unresolved` 结果，因此不需要依赖最终 report 才能恢复问题拆解；未选择的
中间尝试不会进入主报告。
输出超过 12,000 字符时优先裁剪重复或咨询性内容，再裁剪深层 observation 摘要。没有
证据时仍返回结构化失败，然后恢复主 Agent 原工具。Explorer 生命周期、预算提醒和免费
重试的聚合信息写入 `events.jsonl`；完成事件只额外记录 requirement、相关 evidence、
选中来源的数量和报告字符数，不写原始报告或样本。主 Trace、模型请求超时与重试、
120 秒任务硬超时、恢复和失败重跑语义保持不变。

## 历史实验

下述 v1、v2 和 bad-case 定向增强结果是已淘汰设计的历史记录，用于解释为何移除自动
Inventory、歧义规则和要求模型复制 `focus/candidate_paths` 的接口。它们不能代表当前
工作流对齐版本的结果；当前版本必须先通过固定 9 题门槛，才会运行一次 50 题实验。

## 50 题实验结果

2026-07-27 使用本地目标配置完成一次 50 题实验，并与当前 `master` 已记录的最佳一次
50 题结果比较。两次 Runner 均成功 40/50，缺失预测均为 10；目标方案总分为 0.5303，
Mean Recall 为 0.5333，低于对照的 0.5703 与 0.5733。目标方案得到 25 个满分、3 个
部分匹配和 12 个零分任务；对照为 27 个满分、3 个部分匹配和 10 个零分任务。逐题分数
匿名比较显示 48/50 不变，2 个对照满分任务分别退化为零分和缺失，没有任务提升。

| 指标 | 当前 `master` 最佳一次 | Explorer + 20 主步骤 |
| --- | ---: | ---: |
| 总分 / Mean Recall | 0.5703 / 0.5733 | 0.5303 / 0.5333 |
| Runner 成功 / 缺失预测 | 40 / 10 | 40 / 10 |
| 满分 / 部分匹配 / 零分 | 27 / 3 / 10 | 25 / 3 / 12 |
| 墙钟时间 | 863.205 秒 | 1,263.989 秒 |
| 主 Agent 工具调用 | 513 | 496 |
| 主 Agent 平均完成步骤 | 10.22 | 9.90 |
| 主步骤耗尽任务 | 7 | 6 |
| 成功模型请求 | 513 | 754 |

49/50 个任务调用了 Explorer，其中 47 次正常完成、2 次 fail-open；正常完成平均使用
5.23 步，25 次用满 6 步。Explorer 消耗 460,289 Token，占目标方案总计 4,028,412
Token 的 11.4%；旧对照事件未记录可直接汇总的 Token，因此不报告其 Token 数。目标方案
墙钟增加约 46%，成功模型请求增加约 47%。

这次结果对应已经淘汰的 v1 强制多轮设计，没有证明收益，反而观测到分数和延迟退化。当前
evidence-first 版本改为自动 Inventory 加可选定向 Explorer；它的 50 题结果将在自动化检查
通过后单独记录，不能预先宣称提升。

## Evidence-first v2 50 题实验

2026-07-27 完成一次 v2 目标方案实验。总分为 0.5713，Mean Recall 为 0.5733；Runner
成功 38/50，缺失预测 12，得到 28 个满分、2 个部分匹配和 8 个非缺失零分任务。与当前
`master` 最佳一次相比，总分仅增加 0.0010、Mean Recall 相同，但 Runner 成功减少 2、
缺失增加 2，因此没有通过预先设定的成功率与缺失门槛。

| 指标 | 当前 `master` 最佳一次 | 强制 Explorer v1 | Evidence-first v2 |
| --- | ---: | ---: | ---: |
| 总分 / Mean Recall | 0.5703 / 0.5733 | 0.5303 / 0.5333 | 0.5713 / 0.5733 |
| Runner 成功 / 缺失预测 | 40 / 10 | 40 / 10 | 38 / 12 |
| 满分 / 部分匹配 / 零分 | 27 / 3 / 10 | 25 / 3 / 12 | 28 / 2 / 8 |
| 墙钟时间 | 863.205 秒 | 1,263.989 秒 | 953.882 秒 |
| 主 Agent 工具调用 | 513 | 496 | 434 |
| 主 Agent 平均完成步骤 | 10.22 | 9.90 | 8.66 |
| 主步骤耗尽任务 | 7 | 6 | 6 |
| 成功模型请求 | 513 | 754 | 434 |
| 总 Token | 未记录 | 4,028,412 | 2,909,542 |

50 个任务均成功生成 Inventory，平均 Prompt Inventory 为 2,225 字符，最大 5,591 字符，
没有 Inventory 失败。主 Agent 没有在任何任务中调用可选 `explore`，因此本次没有产生
Explorer 子 Agent 请求；真实服务实验实际验证的是“自动确定性 Inventory + 现有工具”，
定向子 Agent 只经过了 scripted 自动化测试。

相对 `master` 最佳一次，逐题分数有 46 题不变、2 题提升、2 题退化，净总分仅增加
0.0010，远小于单轮模型服务波动所能排除的范围。v2 明确消除了 v1 的固定子 Agent 成本，
但没有同时保持 Runner 可靠性，因此不满足创建 PR 的验收条件。该实现仅作为后续优化的
可复现检查点提交到个人 Fork 分支，尚未创建 PR。

## Bad-case 定向增强

Evidence-first v2 的确定性 Inventory 能消除强制子 Agent 的固定成本，但真实实验中主
Agent 从未主动调用可选的 `explore`，说明仅靠 Prompt 建议无法稳定触发需要补充证据的
任务。本轮增强不按任务 ID 编写规则，而是从 Inventory 中可观察的结构事实生成客观歧义：
大 JSON 的部分结构、宽表与其他结构化来源并存，以及多个来源之间的同名字段、兼容类型
或有限样本值重叠。

Inventory schema 升级为 v2，并增加以下只读派生信息：

- `relation_candidates` 最多 12 个，记录路径、表、字段和候选关系信号；所有连接关系都
  明确标记为 candidate，不能作为已验证事实。
- `exploration` 包含 `recommended`、合并后的 `focus`、最多 8 个 `candidate_paths` 和
  `ambiguity_codes`。这些字段由通用确定性规则产生。
- 大 JSON 使用 `ijson` 在现有单文件字节预算内流式提取顶层类型、最多 48 个嵌套字段
  路径、类型和 3 个有界对象样本；预算耗尽保留已有证据并标记截断，损坏输入仍然
  fail-open。

当 `exploration.recommended=true` 时，主 Agent 的首次模型请求只暴露 `explore`，并要求
使用 Inventory 给出的精确 `focus` 和 `candidate_paths`。Explorer 完成或返回可恢复失败
后，运行时移除 `explore` 并恢复原有工具，避免重复探索。没有客观歧义、Explorer 被禁用
或 Inventory 扫描失败时，链路保持 v2 行为。

Explorer 报告增加 `recommended_checks`，仅允许 `source_relevance`、`field_semantics`、
`join_coverage` 和 `filter_domain` 四类检查。每项检查必须引用已存在的 evidence ID、候选
路径和真实字段，不允许携带可执行 SQL 或 Python。`context_inventory_created` 事件只增加
是否推荐探索、歧义数量和候选关系数量等聚合字段，不写入 Inventory、原始样本或模型内容。

固定快速评测清单位于 `configs/explorer_bad_cases.example.txt`。其中 9 个活动任务用于
验证 Explorer 可改善的核心组和观察组，6 个注释任务作为非 Explorer 对照。任务 ID 只属于
评测配置，不参与任何运行时分支。9 题进入全量实验的门槛为至少 2 题从零变为非零、Runner
成功不少于 6/9、缺失不超过 3，并满足每个被标记任务仅调用一次 Explorer、子 Agent 请求
和 preview 预算。只有通过该门槛才运行一次 50 题实验；单轮结果只记录为当前配置下的
观测，不用于宣称稳定因果提升。

## Bad-case 定向增强 9 题实验

2026-07-27 在提交 `ba3db7f` 上运行一次固定 9 题目标集。Runner 成功 6/9、缺失 3；
`task_86` 得 1.0，`task_259` 得 0.95，其余 7 题为零分。目标集分数之和为 1.95，目标集
均分为 0.2167，Mean Recall 为 0.2222。scorer 对完整 50 题清单报告的 0.0390 包含 41 个
未运行任务，不能描述成一次 50 题 benchmark 成绩。

| 任务 | Runner | 得分 | 结果分类 |
| --- | --- | ---: | --- |
| `task_19` | 成功 | 0.00 | 非缺失零分 |
| `task_25` | 成功 | 0.00 | 非缺失零分 |
| `task_38` | 失败 | 0.00 | 模型请求超时 |
| `task_80` | 失败 | 0.00 | 120 秒任务硬超时 |
| `task_86` | 成功 | 1.00 | 满分 |
| `task_89` | 成功 | 0.00 | 非缺失零分 |
| `task_173` | 失败 | 0.00 | 主步骤耗尽 |
| `task_199` | 成功 | 0.00 | 非缺失零分 |
| `task_259` | 成功 | 0.95 | Recall 1.0，额外列受罚 |

作为参照，v2 的同一批任务在其 50 题实验中均为零分、Runner 成功 5/9、缺失 4。定向增强
观察到 2 题从零变为非零，Runner 成功增加 1，主 Agent 完成步骤从 96 降至 85，
SQL/Python 调用从 66 降至 43，失败的 SQL/Python 调用从 5 降至 4，步骤耗尽从 2 降至
1。本次 9 题墙钟为 320.697 秒；v2 数据来自全量并发运行，不能用其目标任务时间跨度作为
严格墙钟对照。

不过，本次没有通过“一次 Explorer 调用”的契约门槛。9 个任务均被确定性规则推荐探索，
也均成功启动一次子 Agent；子 Agent 合计 26 次模型请求和 17 次 preview，单任务未超过
3 次请求、2 次 preview。但主 Agent 的第一次 `explore` 在 9/9 任务中都因
`EXPLORATION_REQUEST_MISMATCH` 被拒绝，第二次才成功，因此实际产生 18 次主 Agent
`explore` 调用。

只读审计显示，9 个首次调用都改写了自然语言 `focus`；7 个任务的 `candidate_paths`
完全一致，另 2 个任务缩小了候选路径。这说明故障位于“要求模型逐字复制确定性参数”的
接口设计，而不是路径安全、Inventory 扫描或 Explorer 报告执行失败。额外重试和子 Agent
使成功模型请求从 v2 同批任务的 96 增至 112，总 Token 从 633,798 增至 964,372。因此，
尽管本轮观察到两个任务得分提升和较少的计算调用，仍未达到预设验收线，不运行 50 题，
也不创建 PR。

## 子 Agent 工作流对齐 9 题实验

2026-07-28 在工作流对齐提交 `31674eb` 上第一次运行固定 9 题时，9 题均只调用一次主
`explore`，但 8 题在 handler 前得到 `ARGUMENT_VALIDATION_ERROR`。脱敏参数形状审计
显示，OpenAI-compatible 模型把空对象生成成带 `"{}"` 键或示例占位键的对象；只有一题
真正进入子 Agent。提交 `941b868` 保持公开 schema 为无参数空对象，只在 `explore` 和
`inspect_files` 两个空输入边界丢弃无意义占位属性，并让子 Agent 按阶段只看到合法工具。
自动化检查通过后，使用相同任务和配置重新运行一次。

修复后的 9 题 Runner 成功 5/9、缺失 4；只有 `task_259` 非零且为满分，目标集分数之和
为 1.00。scorer 对完整 50 题清单报告的总分 0.0200 包含 41 个未运行任务，不能描述成
50 题 benchmark 成绩。相对 `ba3db7f` 的 9 题结果，非零题从 2 降为 1、目标集分数和从
1.95 降为 1.00、Runner 成功从 6 降为 5，因此没有达到进入全量实验的门槛。

| 指标 | Bad-case 定向增强 `ba3db7f` | 工作流对齐 `941b868` |
| --- | ---: | ---: |
| 目标集非零题 / 分数和 | 2 / 1.95 | 1 / 1.00 |
| Runner 成功 / 缺失 | 6 / 3 | 5 / 4 |
| 主 explore 调用 | 18 | 9 |
| Explorer inspect 成功 | 不适用 | 9 |
| Explorer 模型请求 / 平均轮数 | 26 / 2.89 | 89 / 9.89 |
| Explorer 正常 report / fallback | 6 / 3 | 0 / 9 |
| 主 Agent 完成步骤 | 85 | 92 |
| 主 SQL/Python 调用 | 43 | 50 |
| 主步骤耗尽任务 | 1 | 2 |
| 成功模型请求 / 总 Token | 112 / 964,372 | 182 / 1,102,074 |
| 墙钟 | 320.697 秒 | 337.563 秒 |

工作流对齐版本满足了“一题一次无参数 explore”“一题一次 inspect”和显式读取
`knowledge.md` 的协议目标，也消除了 `focus/candidate_paths` 复制重试。但子 Agent
没有形成可执行的收敛策略：9 题都用到 fallback，7 题在最后一轮仍未选择 report，一题
达到软时限，另一题的 report 连续出现参数或完整文件地图校验错误。总计尝试 38 次 grep、
19 次 preview 和 12 次探索 SQL，正常 report 为零；10 轮上限从复杂任务余量变成了普遍
行为。

因此，本次失败主要位于 Explorer 的“停止探索并压缩为合法报告”阶段，而不是入口参数、
inspect 扫描、knowledge 显式读取或 call ID 配对。扩大子 Agent 预算并没有提高报告质量，
反而增加模型请求、Token、主计算调用和步骤耗尽。该版本不运行 50 题、不创建 PR；当前
结果仅作为可复现的负向实验记录，后续若继续优化，应先简化 report 契约或引入确定性的
报告合成/停止条件，再用同一 9 题验证。

## 运行时报告合并与收敛重试 9 题实验

2026-07-28 在提交 `420637c` 上再次运行固定 9 题。该版本让模型只向 `report` 提交语义
增量，由运行时自动合并文件、schema、样本和 evidence；同时增加 70%/90% 预算提醒、末步
最多两次免费 report 重试，并让正常 report 与 fallback 共用一个吸收全部成功 observation
的确定性汇总器。

结构目标得到验证：9 个任务均正常生成 report，fallback 从上一版的 9 次降为 0；3 个任务
在末轮选择了非 report 工具，全部经一次免费重试成功报告。Explorer 请求从 89 降至 80，
平均普通轮数从 9.89 降至 8.56；主 Agent 完成步骤从 92 降至 87，SQL/Python 调用从 50
降至 49，成功模型请求从 182 降至 168，总 Token 从 1,102,074 降至 1,003,519。Runner
成功从 5/9 增至 6/9；失败任务分别为一次模型请求超时、一次 120 秒任务硬超时和一次主
步骤耗尽。

| 指标 | 工作流对齐 `941b868` | 运行时合并 `420637c` |
| --- | ---: | ---: |
| 目标集非零题 / 分数和 | 1 / 1.00 | 0 / 0.00 |
| Runner 成功 / 缺失 | 5 / 4 | 6 / 3 |
| Explorer 正常 report / fallback | 0 / 9 | 9 / 0 |
| Explorer 请求 / 平均普通轮数 | 89 / 9.89 | 80 / 8.56 |
| 末步免费 report 重试 | 不适用 | 3 |
| 主 Agent 完成步骤 | 92 | 87 |
| 主 SQL/Python 调用 | 50 | 49 |
| 主步骤耗尽任务 | 2 | 1 |
| 成功模型请求 / 总 Token | 182 / 1,102,074 | 168 / 1,003,519 |
| 事件时间跨度 | 337.563 秒 | 353.846 秒 |

尽管协议收敛、报告交付和 Runner 成功数有所改善，6 个成功任务的预测均未匹配 gold，
目标集没有非零题，且 Explorer 平均轮数仍高于预设的 6。因此该版本没有通过进入 50 题
实验的门槛，不运行全量实验，也不创建 PR。这次单轮结果只能证明运行时合并与免费重试
解决了“没有合法报告”和“深层探索无法交付”的结构问题，不能证明报告提高了答案语义
准确率。当前数据也不足以区分负向分数来自报告内容改变还是模型服务轨迹波动；后续若
继续优化，应先在同一 9 题上降低无关报告内容和普遍满预算探索，再重新验证。

## 按题目需求投影 evidence 的 9 题实验

2026-07-28 在提交 `1db3867` 上运行同一固定 9 题。该版本要求每个深层探索调用携带稳定
的 `requirement_ids` 和 `purpose`；正常报告只投影被 `relevant_evidence` 选择、且与调用
时需求绑定一致的 preview/grep/SQL observation。全文件背景仍保留为极简清单，inspect
生成的弱关系候选不再自动进入报告；fallback 最多选择 8 条带需求绑定的高优先级证据。

相关性过滤按预期生效：9 个任务共声明 30 个 requirements，55 条成功深层 observation
中只有 13 条进入正常报告，选中 23 个来源；平均每题分别为 3.33、1.44 和 2.56。平均
报告长度为 4,415 字符。9 个任务均正常 report、没有 fallback；4 次 report 参数错误和
一次深层 preview 参数错误均通过可恢复 observation 继续执行。

| 指标 | 运行时合并 `420637c` | 相关性投影 `1db3867` |
| --- | ---: | ---: |
| 目标集非零题 / 分数和 | 0 / 0.00 | 1 / 1.00 |
| Runner 成功 / 缺失 | 6 / 3 | 7 / 2 |
| Explorer 正常 report / fallback | 9 / 0 | 9 / 0 |
| Explorer 请求 / 平均普通轮数 | 80 / 8.56 | 84 / 9.11 |
| 主 Agent 完成步骤 | 87 | 69 |
| 主 SQL/Python 调用 | 49 | 29 |
| 成功模型请求 / 总 Token | 168 / 1,003,519 | 153 / 921,027 |
| 事件时间跨度 | 353.846 秒 | 357.656 秒 |

本轮 `task_89` 从零分变为满分，其余 8 题为零；完整 scorer 输出中的 0.0200 仍包含 41
个未运行任务，不能描述成一次 50 题 benchmark 成绩。与紧邻版本相比，报告更聚焦，
Runner、主步骤、计算调用和 Token 均改善，但 Explorer 本身没有更早停止，平均轮数和
墙钟略有增加。相对固定门槛，本轮只有 1 题非零且目标集分数和为 1.00，仍低于至少 2 题
非零和 1.95 分，因此不运行 50 题、不创建 PR。该单轮结果只能描述为“相关性投影带来
局部恢复并减少主 Agent 重复探索”，不能证明稳定的语义准确率提升。

## 需求锁定与候选字段覆盖的 9 题实验

2026-07-28 在提交 `ad03ef6` 上运行同一固定 9 题。该版本在 inspect 后增加独占的
`lock_requirements`，将实体、指标、过滤、时间、输出和知识映射到真实候选路径与字段，
并要求显式混淆字段取得覆盖证据后才开放 `report`。结构边界生效，但一次性锁定接口过严：
9 题对 `lock_requirements` 共发起 41 次调用，其中 23 次因多候选字段没有同时声明
`needs_discovery` 被拒绝，8 次因模型漏建 knowledge 需求被拒绝。后续又出现 7 次阶段性
未知工具、4 次遗漏目标字段，以及少量候选字段或路径不匹配。

| 指标 | 相关性投影 `1db3867` | 严格需求锁定 `ad03ef6` |
| --- | ---: | ---: |
| 目标集非零题 / 分数和 | 1 / 1.00 | 1 / 1.00 |
| Runner 成功 / 缺失 | 7 / 2 | 5 / 4 |
| Explorer 正常 report / fallback | 9 / 0 | 6 / 3 |
| Explorer 请求 / 平均普通轮数 | 84 / 9.11 | 95 / 10.00 |
| 主 Agent 完成步骤 | 69 | 86 |
| 主 SQL/Python 调用 | 29 | 19 |
| 成功模型请求 / 总 Token | 153 / 921,027 | 182 / 1,167,551 |
| 事件时间跨度 | 357.656 秒 | 421.250 秒 |

只有 `task_89` 非零；4 个任务触发 120 秒硬超时，3 个 Explorer 使用 fallback。该结果
没有通过 Runner、非零题数和分数门槛，因此不运行 50 题。负向变化的主要可观测原因不是
“没有拆题”，而是模型需要逐项满足运行时可确定补全的 bookkeeping，10 轮大量消耗在
重提锁定参数和补字段，而不是查证据。后续版本因此保留不可变需求与路径安全边界，但由
运行时自动规范多候选字段的探索标记、补充 knowledge 需求、收敛目标字段，并在 knowledge
读取后允许提前提交 unresolved report；该修复需在同一 9 题上重新验证，不能根据结构测试
预先宣称分数提升。
