# 行为树草稿输出契约 v1

本契约是场景草稿的交换格式，不是可直接下发机器人的执行格式。顶层只能有下表所列字段；数组没有内容时使用 `[]`，未知值使用 `null`，不要用字符串“未知”代替空值。

## 1. 顶层

| 字段 | 类型与含义 |
|---|---|
| `schema_version` | 固定字符串 `1.0` |
| `scenario_id` | 本场景稳定 ID |
| `revision` | 从 1 开始的整数；每次修订递增 |
| `summary` | 简短描述客户要求的结果、范围及仍未明确的关键边界 |
| `sources` | 本轮及沿用的来源记录 |
| `fields` | 场景事实、要求和共享状态字段 |
| `root_id` | 主行为树根节点 ID |
| `nodes` | 主树与候选子树的所有节点实例 |
| `alternatives` | 尚待选择或已选的候选方案组 |
| `issues` | 当前未解决缺口／冲突／选择／证据事项 |
| `questions` | 下一轮问客户的问题，0～3 个 |
| `changes` | 新增、修改、删除及受影响节点记录 |
| `checks` | 校验执行情况及回读条件，不包含技术可行性判断 |

ID 在场景内唯一，建议使用 `src_001`、`f_001`、`n_001`、`alt_001`、`issue_001`、`q_001`。元数据 ID 可自行分配；这不允许编造物理事实。更新时保持原 ID，不因排序改变重新编号。

## 2. 来源和字段

来源对象：

```json
{"id":"src_001","kind":"user","locator":"本轮客户消息","excerpt":"让机器人把A点的箱子搬到B点"}
```

`kind` 为 `user`、`document` 或 `robot_evidence`。`locator` 是消息轮次、用户提供的文件／页码等真实定位；`excerpt` 是已读取的原文，不得编造。工程推导使用节点／字段的 `rationale`，不伪装成来源证据。

字段对象（所有键均需提供）：

```json
{
  "id":"f_001","domain":"constraints","role":"requirement",
  "label":"箱子重量上限","value":null,"unit":"kg","status":"unknown",
  "source_ids":[],"rationale":"抬升和负载运输需要明确质量范围",
  "confirmed":false,"blocking":true
}
```

- `domain`：`boundary`、`fact_state`、`requirement_graph`、`constraints`、`disturbances`、`acceptance_value`。
- `role`：`requirement`（期望／限制）、`initial_fact`（客户描述的现场事实）、`runtime`（执行时由观察或动作复核获得）。
- `status`：`known`、`unknown`、`candidate`、`conflict`、`not_applicable`。
- `known` 必须有真实来源；表示原话已明确，不等于现场实测或整场景已确认。
- `candidate` 必须有推导理由，客户未确认；数字估计须明确用途和来源，不作为验收阈值。
- `unknown` 的 `value` 为 null。`conflict` 的 `value` 为互相冲突的陈述列表，并保留各自来源。
- `runtime` 默认 `value:null`、`status:unknown`，这是未来观测变量，不能仅因当前没有运行就判草稿缺失。需通过节点读写说明谁产生数据；没有产生方式或所需精度／时效未知时才建立相关缺口。
- `confirmed` 仅在客户明确确认对应内容时为 true；不得由结构校验通过自动设置。
- 未知／冲突／待确认且 `blocking:true` 的非 runtime 字段，必须关联一个阻塞 issue。

## 3. 节点

所有节点使用同一种对象；无关数组为空、可空引用为 null：

```json
{
  "id":"n_001","type":"Action","label":"导航到A点作业准备位置",
  "children":[],"subtree_id":null,"alternative_group":null,
  "read_fields":[],"write_fields":[],"constraint_fields":[],"location_fields":[],
  "preconditions":["到位目标和对应状态下的可通行条件已核实"],
  "success_criteria":["相对位置及朝向进入后续操作的有效范围"],
  "failure_cases":["定位信息无效时停止继续接近，并向上返回原因；处置规则需确认"],
  "parameters":{},
  "basis":{"status":"candidate","source_ids":[],"rationale":"搬运前需要到达取物位置"},
  "required_capabilities":["空载导航到位"]
}
```

`type` 仅允许 `Sequence`、`Fallback`、`Condition`、`Action`、`SubTree`、`Retry`、`Timeout`、`Loop`；后三者是 PRD 中 Decorator 的具体类型。

- `Sequence`／`Fallback` 至少一个有序 `children`；`Retry`／`Timeout`／`Loop` 恰好一个；`Action`／`Condition`／`SubTree` 无 `children`。
- `SubTree.subtree_id` 指向一个节点根；其他类型该值为 null。未选择方案可为 null，但须设置 `alternative_group` 并建立阻塞选择 issue。不可凭空引用未定义的子树。
- `read_fields`、`write_fields`、`constraint_fields`、`location_fields` 都引用 `fields.id`。Condition 和控制／修饰节点不能写共享字段，实际观察和更新由 Action 承担；SubTree 的读写由展开节点表达。
- 动作预期效果写在 `success_criteria`，`write_fields` 只声明输出变量，不能据此把输出变量提前改成已知事实。
- `preconditions`、`success_criteria`、`failure_cases` 是人可读规则。对应数值、时效和状态条件必须引用相关字段，避免在文字内藏一份独立阈值。
- `basis.status` 为 `known` 或 `candidate`。直接由客户指定的节点可为 known，并列来源；为补全流程推导的节点保持 candidate 和理由。
- `required_capabilities` 用可验收行为命名，不写“已具备”。未展开的 Action 可作为初期占位，但须以阻塞 issue 说明还需拆解的范围；不能假装复杂任务已被一个万能动作解决。

特殊参数使用字段引用，未知参数必须落在字段与 issue 中：

| 类型 | `parameters` 内容 |
|---|---|
| Retry | `max_attempts_field` 和／或 `max_duration_field`；`retryable_reasons`、`reentry_checks` 两个非空字符串数组 |
| Timeout | `max_duration_field` |
| Loop | `completion_field`；`max_iterations_field` 和／或 `max_duration_field` |
| 其他 | 无需附加参数时 `{}` |

次数上限为正整数且单位 `count`，时间上限为正数且单位 `s`。未知仍填 null，不允许 `-1` 或无限。`completion_field` 是 runtime 布尔量，由循环前的观察 Action 初始化、每轮子树中的 Action 更新。条件判定未知时返回未满足／未知原因，不能当成“没有剩余对象”。

主树和候选子树的父子／引用关系必须无环。一个节点实例只属于一个父节点；复用模板要实例化独立 ID。候选根被选中的 SubTree 引用不构成另一套模型。

## 4. 候选方案

```json
{
  "id":"alt_001","requirement_node_id":"n_010",
  "options":[
    {"id":"opt_open","label":"操作防鼠板后通过并复位","root_id":"n_020","precondition_fields":[],"implementation_hints":["VLA为候选方法，未验证"]},
    {"id":"opt_step","label":"直接跨越","root_id":"n_030","precondition_fields":[],"implementation_hints":["专用跨越策略待核实或研发"]}
  ],
  "selected_option_id":null
}
```

候选组对应主树的一个 SubTree 节点。每个 option 的 root 必须定义在 nodes 中。未选择时主树不引用任何候选根；选择后仅绑定所选根。未选根保留用于评审，不得在主树中另接执行路径。`implementation_hints` 只是候选技术路线，不是可用能力清单。

## 5. 缺口与问题

```json
{
  "id":"issue_001","kind":"missing","target_ids":["f_001","n_001"],
  "owner":"customer","blocking":true,
  "description":"箱重范围未明确，影响抬升及负载运输",
  "resolve_by":"客户提供重量范围，无法提供则安排称重"
}
```

`kind` 为 `missing`、`conflict`、`choice`、`evidence` 或 `structural`。`owner` 为 `customer` 或 `engineering`；不能把机器人性能验证交给客户猜测。`target_ids` 引用字段、节点或候选组。

```json
{
  "id":"q_001","text":"箱子的重量范围目前是否有依据？",
  "target_ids":["f_001"],"why":"决定抬升与持物运输的负载要求",
  "options":[
    {"label":"有实测重量，我来提供","interpretation":"取得具体范围后记录为有来源的数据"},
    {"label":"只有估计范围","interpretation":"记录为候选估计，保留验证缺口"},
    {"label":"还不清楚","interpretation":"字段保持未知，记录称重事项"}
  ],
  "free_text":true,"allow_unknown":true
}
```

每轮最多三个问题；选项必须与实际问题相关。选择“有实测值”但未提供数值时，数值仍为 null。问题只解决已记录的 customer issue；工程 issue 通过 `resolve_by` 交给下游资料核实。

## 6. 修订与检查

```json
{
  "changes":{"added_ids":[],"updated_ids":[],"removed_ids":[],"affected_node_ids":[]},
  "checks":{"validation":"not_run","blocking_issue_ids":[],"ready_for_readback":false,"notes":[]}
}
```

`changes` 不再维护一套执行图。`added_ids`／`updated_ids` 对应当前字段、节点或候选组；`removed_ids` 保留已删除对象的旧 ID，不能再被当前结构引用；`affected_node_ids` 提供给后续评审模块，不能替人工改判断。

`checks.validation` 为 `not_run` 或 `passed`，仅代表配套脚本的结构检查；未经运行不得写 passed。`blocking_issue_ids` 必须完整列出当前阻塞 issue。

`ready_for_readback` 只表示正常路径、必要分支及其未知项已足够清楚可供客户回读，不代表场景已确认、参数齐全或能力可行。初建且任务边界尚模糊时为 false。有阻塞未知也可回读，但必须清晰展示，不得自动生成客户确认或人工判断。

## 7. 生成约束

结构元数据不需要客户决定。未明确的物理事实与技术选择必须保留未知、候选或冲突；生成这样的草稿不等于替客户决定需求。若调用环境的更高优先级指令禁止在模糊条件下生成任何结构，则先提出必要问题，并说明尚未生成草稿。

默认返回完整新快照，避免要求接收方从自然语言中推断补丁。除用户明确要求保存，skill 自身不决定写入位置。不要输出虚构的已运行节点状态、执行成功率、现场证据或人工审批结论。
