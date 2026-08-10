# Phase 1 Evidence Plan 独立实验

## 实验目的与边界

PR9 三轮 50 题实验中，24 个任务至少一次没有满分。其中 11 个任务在满分与非满分之间
切换，主要根因是字段、过滤、关系谓词、去重单位和聚合对象在不同轮次被模型随机解释为
不同语义（`task_25/27/86/194/196/259/352` 等）。此前七类轻量实验（projection 一致性、
静态字段 Prompt、must_verify、read_doc 关键词等）都只改变提示位置或消费位置，没有让
主 Agent 在动手前固定“对未解需求采取哪种解释”。

本实验引入一个**默认关闭**的动态工具 `commit_evidence_plan`：当 Explorer 报告仍带未解
requirement 或 uncertainty 时，强制主 Agent 在下一次请求里只提交一份结构化证据计划，
由运行时做确定性校验。目标是检验：对未解需求强制一次可验证的计划提交，是否能稳定
字段、过滤、关系、去重和聚合路线。

边界（本实验明确不做什么）：

- 不改 Explorer 的语义产物（报告仍由现有 Explorer 生成）；
- 不读 gold，不把已知答案写进提示或校验；
- 不改 Verifier 与 Scorer；
- 计划默认只是提示承诺，不执行计划里的核验动作本身；Option C（见下文）在计划提交后至多
  拦截一次"未完成核验的首次 answer"，但守卫自身仍不执行任何核验动作；
- 不处理输出投影或长文档 ETL。

## 配置与触发条件

```yaml
evidence_plan:
  enabled: false
  max_commit_attempts: 2
  strict_keys: true
  verification_gate: true
```

`enabled` 默认关闭；启用后默认同时使用 Option B 严格键和 Option C 答题前核验守卫。
`max_commit_attempts` 仅允许 `1` 或 `2`，其余值在配置加载时被拒绝。`strict_keys=false`
和 `verification_gate=false` 只保留给 Option A、A+B 消融，不是推荐运行配置。Runner 把
四项配置传入 `ReActAgentConfig`。

`commit_evidence_plan` 只有在 `enabled=true` **且** Explorer 报告满足以下任一条件时才
暴露为下一轮唯一工具：

- 存在 `status=unresolved` 的 `task_requirements`；
- 存在 `uncertainties`。

触发集合定义为“未解 requirement ID 与 `uncertainty.requirement_id` 的并集”。

## 计划 Schema 与确定性校验

推荐配置使用动态严格 Pydantic schema（`extra=forbid, strict=True`）。每个 pending ID
直接成为 `items` 对象的必填键，模型不再生成 `requirement_id`：

```text
items
  <pending_requirement_id>（每个 ID 都是必填键，禁止额外键）
    candidates[1..4]
      claim: 候选字段/聚合/关系解释
      source_fields[1..4]: path(必填), table(可选), field(可选)
      operation: measure | filter | time_scope | join（必填）
    verifications[1..3]
      tool: read_csv | read_json | read_doc |
            inspect_sqlite_schema | execute_context_sql
      path: 一个精确 context 相对路径
      purpose: 此动作如何区分候选
```

`strict_keys=false` 时使用旧的 `items[] + requirement_id` 列表形状，仅用于 Option A 消融。
无论输入形状如何，控制器都会恢复为统一的 `EvidenceItem` 列表后执行相同内容校验。

确定性校验（跨字段检查，全部返回可恢复错误）：

| 校验 | 错误码 |
| --- | --- |
| items 必须与“未解 requirement ID 和 uncertainty.requirement_id 的并集”一一对应，不能漏、重或额外声明 | `EVIDENCE_PLAN_REQUIREMENT_COVERAGE` |
| 所有 path、table、field 必须来自 Explorer report 的 files/schema_map；结构化文件上的 field 必须真实存在；叙事文档（markdown/text/PDF）只允许引用 path，不允许伪造字段 | `EVIDENCE_PLAN_INVALID_SOURCE` |
| 每个 verification path 必须属于其候选 source_fields；verification tool 必须与 Inventory 文件类型兼容；不允许 `execute_python` 或 `list_context` 作为核验动作 | `EVIDENCE_PLAN_INVALID_VERIFICATION` |

文件类型与核验工具兼容矩阵：

| 文件 kind | 兼容核验工具 |
| --- | --- |
| tabular（csv/tsv） | `read_csv` |
| json | `read_json` |
| sqlite | `inspect_sqlite_schema`、`execute_context_sql` |
| markdown / text / pdf | `read_doc` |

JSON/Pydantic 参数错误沿用现有 `ARGUMENT_VALIDATION_ERROR`，不引入新错误码。字段存在性
依据报告 `schema_map`（缺失时回退到 files summary），与 Explorer 的 `_summary_fields`
口径一致：顶层 `columns/field_paths/keys` 与 sqlite `tables[].columns`。

## ReAct 状态流转

```text
explore
  → 若不触发：恢复现有正常工具
  → 若触发：下一轮仅暴露 commit_evidence_plan
    → 提交成功：恢复现有正常工具
    → 提交失败：最多重试 max_commit_attempts
    → 达到重试上限或无法保留一个后续正常步骤：fail-open，恢复正常工具
```

- 仅当 Explorer 完成后仍至少保留两个正常步骤时才激活计划；否则记录
  `INSUFFICIENT_STEP_BUDGET` 并跳过，避免 `max_steps=2` 等配置把原本可提交的答案
  变成缺失。
- 无效计划、无 tool call、多调用和缺 call ID/name 会消耗一次提交尝试；pending 期间误调
  普通工具时，Option A 不执行 handler、不消耗提交尝试，而是返回
  `EVIDENCE_PLAN_PENDING` 纠正。失败次数达到 `max_commit_attempts`，或当前步骤之后剩余
  正常步骤 `< 2` 时 fail-open。若 pending 状态残留到最后一个正常步骤，也会在请求前
  fail-open，保证确定性 answer 守卫仍能生效。
- 开启 `evidence_plan` 时，系统提示增加一条计划模式说明：pending 期间
  `commit_evidence_plan` 是唯一可用工具；提交被拒绝时按错误修正后重试，直到成功或
  运行时 fail-open；成功后优先用正常工具执行声明的核验动作，在步数预算允许时先完成
  声明的核验再提交答案。
- 提交计划后，运行时记录后续成功工具调用是否精确匹配某个 `verification.tool` +
  `verification.path`；未完成核验时，计划提交后的首次 answer 会被 Option C 守卫拒绝，
  此后 fail-open（详见下文的守卫小节）。
- 最终答案通过现有 Verifier 时，只写入计划完成摘要（事件），不改变提交行为。
- 计划激活期间不会重新暴露 `explore`；成功提交后恢复正常工具。

## 事件

| 事件 | 记录内容 |
| --- | --- |
| `evidence_plan_required` | step、剩余步数、待处理 requirement 数量、最大提交尝试数 |
| `evidence_plan_committed` | step、call ID、item/candidate/verification 数量、requirement ID 列表 |
| `evidence_plan_commit_rejected` | step、call ID、错误码、第几次尝试、最大尝试数、是否 fail-open、剩余步数 |
| `evidence_plan_skipped` | step、跳过原因、剩余步数、待处理数量 |
| `evidence_plan_verification_observed` | step、call ID、tool、path |
| `evidence_plan_answered` | step、是否提交计划、requirement 数量、核验总数/已观察数/是否全部观察 |
| `evidence_plan_answer_blocked`（Option C） | step、call ID、缺失核验数量、缺失的唯一 tool/path 列表 |
| `evidence_plan_verification_gate_skipped`（Option C） | step、跳过原因（预算不足/已使用一次纠正/最终 answer-only retry） |

上述 `evidence_plan_*` 专属事件只记录步骤、数量、requirement ID、错误码和预算状态，不记录
原始计划、数据行、模型响应或 SQL/Python 内容。通用 `tool_started`/`tool_failed` 事件仍
按现有协议保存工具参数（含 `commit_evidence_plan` 的原始计划 JSON），这与
`execute_context_sql`/`execute_python` 保存 SQL/Python 内容一致；这些本地 diagnostics
只写入 artifacts，不提交。

## Option C：答题前核验完成守卫（2026-08-09）

前两轮实验（Option A 运行时强制、Option B strict_keys）把 COVERAGE 结构性消除、提交率
提到 72.4%，但脱敏数据表明**过半声明核验从未被调用、分数 0.363 纹丝不动**。Option C
在不改计划内容校验、Explorer、评分器和工具集合的前提下，把"计划只是提示承诺"
升级为一次有界的执行守卫：**计划提交成功后，若 Agent 调用 `answer` 时仍有声明的
verification 未被现有"成功工具调用 + 精确 tool/path"观测逻辑命中，则用原始 answer
`tool_call_id` 返回一次可恢复错误 `EVIDENCE_PLAN_VERIFICATION_PENDING`，列出缺失的唯一
tool/path，要求先完成这些核验再提交答案。**

`verification_gate` 默认为 true；设置为 false 可复现 A+B 消融。系统 Prompt 使用中性措辞
要求在预算允许时先完成声明核验，开关不会改变 Prompt，因此两组只隔离运行时守卫。

触发与状态流转：

- 仅在计划成功提交后生效；无触发、计划未提交或核验已全部观测到时行为完全不变，answer
  照常进入 projection gate 与 Answer Verifier。
- 每次任务**最多拦截一次 answer**：第一次未核验 answer 被拒后，第二次 answer 无条件
  fail-open（事件 reason `MAX_ANSWER_BLOCKS`），避免无限纠正循环。
- 仅当剩余正常步骤 `remaining_steps = max_steps - step_index` 能覆盖每个缺失的
  唯一核验再加一次 answer（`required_steps = 缺失唯一数 + 1`，即
  `remaining_steps >= required_steps`）时才拦截；预算不足（reason
  `INSUFFICIENT_STEP_BUDGET`）、最终 answer-only retry（reason
  `FINALIZATION_RETRY`）时 fail-open，不抢占确定性 answer 收尾。
- 被拦截的 answer 不写 prediction、不计为成功；后续成功匹配的读取/SQL 调用计入
  `observed_verifications`，全部核验命中后再次 answer 正常通过现有 Verifier 并终止。
- 沿用既有观测口径：只认"工具调用成功 + `tool`+`path` 精确匹配"，错误路径、错误工具、
  失败调用均不计数；不读取 observation 内容、不新增语义判断、不读 gold、不自动调用工具。
- **注意**：Option C 取代了本文件上文"未完成核验不阻断答案"的旧契约（仅限计划已提交后
  的第一次 answer）。系统提示（`prompt.py`）已同步为中性与守卫一致的措辞——"Complete
  the declared verification actions before submitting the answer when the step budget
  permits."，不再有"未完成核验永不阻断"的表述。

新增事件（全部只记录步骤、数量、call ID 与缺失 tool/path，不含 observation 内容）：
`evidence_plan_answer_blocked`、`evidence_plan_verification_gate_skipped`。

自动化验证覆盖：首次未核验 answer 被拒且保留原始 call ID；完成缺失核验后再次 answer
正常终止；错误路径/错误工具/失败调用不计数；无触发/未提交/核验完成时行为不变；预算不足
（含多缺失核验时预算不足、预算恰好够用、重复 tool/path 只计一次）与第二次 answer
fail-open 且不抢占最终 answer-only 收尾；事件不含 observation；既有 Option A、Option B、
停止守卫与 Answer Verifier 测试全部通过。

## 自动化验证

单元测试覆盖：配置默认关闭、YAML 开启、非法 `max_commit_attempts` 拒绝；schema 严格性
（额外字段拒绝）、需求全集覆盖、重复/未知 ID、虚构路径/表/字段、文件类型与工具不
匹配、叙事文档字段限制、有效多源 join 与多动作核验。

Scripted model/ReAct 测试覆盖：无未解项时不暴露计划工具；触发时第二次请求只看到
`commit_evidence_plan`，成功后恢复正常工具且永不重新暴露 `explore`；无效计划用原始
call ID 返回可恢复 observation 并仅重试计划工具；两次无效调用、无 tool call 或错误
工具后 fail-open 仍保留至少一次正常 answer 机会；小步数预算跳过计划；Explorer fallback
的 `task_goal` 仍能触发并提交计划；后续匹配的读取/SQL 调用记录 verification observed；
计划提交后的首次未核验 answer 被拒、第二次 answer fail-open；final-step guard、
参数校验、事件刷新和 Trace 字段无回归。

## 实验结果与合并边界

所有真实模型实验固定公开 50 题或同一 10 题 bad-case 集、同一模型与 scorer，并使用
`300s task timeout / 4 workers / 20 steps / 20s request / 1 retry / temperature=0`。这里只
报告脱敏聚合指标；端点在 `temperature=0` 下仍有路线、超时和非法参数波动，单轮分数不能
解释为稳定因果收益。

### 协议演进

| 阶段 | 主要改动 | 协议结果 | 语义结果 |
| --- | --- | --- | --- |
| 原始计划 | pending 时只暴露计划工具 | 六轮触发 27/30，有效提交 4/27=14.8%，错误工具 45 次 | 未形成可执行样本 |
| Option A | 错误工具不执行、不消耗提交次数，注入纠正 | 有效提交 17/27=63.0%，错误工具降至 15 次 | 10 题三轮均值 0.363 |
| Option B | pending ID 成为 strict schema 必填键 | COVERAGE 拒绝 18→0，有效提交 21/29=72.4% | 10 题三轮均值仍为 0.363 |
| Option C | 首次未完成核验 answer 有界阻断 | 10 题两轮全量核验完成 9/10=90% | 两轮均值 0.17，含较多端点 missing，未证明语义收益 |

Option B 达成了窄目标：结构上消除 ID 漏填/额外键导致的 COVERAGE 拒绝。剩余拒收转为
`INVALID_SOURCE`、`INVALID_VERIFICATION` 等内容质量问题。Option C 也达成窄目标：把
“计划写了但核验未调用”转为可观测、可纠正的一次运行时契约。

### 50 题观测

| 配置 | 轮次 | total | Mean Recall | non-zero | missing | Runner 成功 | 全量核验完成 | answer blocked |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| PR9 baseline | 3 轮 | 0.7177 / 0.6647 / 0.6783 | — | — | 4 / 3 / 1 | 46 / 47 / 49 | — | — |
| A only | 1 轮 | 0.7237 | 0.7267 | 38 | 3 | 47 | — | 0 |
| A+B | 1 轮 | 0.7017 | 0.7067 | 37 | 3 | 47 | 16/28=57% | 0 |
| A+B+C | 第 1 轮 | 0.5457 | 0.5467 | 28 | 4 | 46 | 23/25=92% | 8 |
| A+B+C | 第 2 轮 | 0.6203 | 0.6233 | 33 | 4 | 46 | 29/31=93.5% | 11 |

A only 的 0.7237 是一次高于 PR9 三轮均值 0.6869 的正向观测，但仅有单轮、且相对 PR9
最高轮只高 0.006，不能宣称稳定提分。A+B 单轮与 A only 的差异也落在既有运行波动内。

两轮 C 的事件级复核推翻了“总分下降由 gate 直接造成”的早期归因：第一轮真正触发
`answer_blocked` 的 8 题相对 A+B 合计约 +1，第二轮触发的 11 题相对 A+B 合计为 0；主要
跨轮掉分发生在没有触发 gate 的任务上。由于这些仍是不同模型运行，不能反向宣称 gate
提分；只能确认**当前证据既未证明 C 稳定提高语义分，也未证明 C 造成整体负优化**。要测
直接影响，应在同一 C 轨迹内离线比较被拒绝的第一次答案与最终答案。

### 合并定位

本功能以**默认关闭的协议与可审计性能力**合并，不作为准确率优化宣传：

- 已证明：计划提交、ID 完整性、声明核验完成率和事件可追踪性明显改善；
- 未证明：计划语义正确、核验 observation 支持 claim、最终答案消费核验结果或分数稳定提高；
- 运行时不读 gold、不判断语义、不自动执行核验，所有纠正都有提交次数或步数上限并
  fail-open；
- 后续不继续收紧 schema。若研究语义收益，应另开独立问题，把 claim 变成可执行断言，
  或比较同轨迹首答与终答，不能把过程指标等同于准确率。

## Bad case 目标集

`configs/evidence_plan_bad_cases.example.txt` 列出 10 个语义路线不稳定任务
（`task_25/86/89/163/173/180/194/196/257/352`）。该文件只用于实验，不参与运行时 task
ID 分支。

## 完成标准

- 只修改 `PHASE_1`，不提交本地 artifacts、gold、trace、prediction、配置密钥或模型原始
  响应；公开文档只保留脱敏聚合指标。
- 发布前执行完整 pytest、Ruff、相关文件 format check、lock check、`git diff --check`，
  并核对暂存文件与 PR 目标。
