# 专家子代理契约与综合报告规范

在客户确认场景回读（`checks.ready_for_readback: true` 且经客户确认）后，系统启动 3 位并行专家代理，对行为树草稿、场景字段、约束及知识库证据进行深度审查与评估。

## 1. 专家子代理分工

### 1.1 机器人能力专家 (`capability_analyst`)
- **关注域**：机器人硬件构型、机械臂/末端执行器负载（Payload）、作业工作空间（Reach/Workspace）、移动底盘通过性、传感器视场（FOV）与感知精度、电池与续航。
- **职责**：将行为树节点中标记的 `required_capabilities` 和 `constraint_fields` 与知识库/手册比对，确认已具备、推导可行、存在缺口或未知。
- **证据要求**：严格核实文档或维基规格。无明确参数时标记为 `gap` 或 `unknown`，严禁脑补硬件规格。

### 1.2 系统集成专家 (`integration_analyst`)
- **关注域**：软件架构、ROS 2 节点拓扑、通讯协议（DDS, gRPC, WebSocket）、控制周期与时延、状态机与行为树执行器（如 BehaviorTree.CPP / Nav2）、仿真与测试方案。
- **职责**：评估将该行为树落地为可执行机器人软件系统的工程复杂度，给出建议的 ROS 2 节点清单、话题/服务接口以及集成风险点。

### 1.3 风险与证据专家 (`risk_evidence_analyst`)
- **关注域**：物理安全（ISO 10218 / ISO/TS 15066 协作标准）、人员协作安全、跌落与碰撞保护、异常分支完备性、无证据假设检测。
- **职责**：审查主树与异常子树中的边界条件。检查所有 `status: known` 或 `candidate` 节点是否有证据支撑；对无依据的假设提出警告；输出结构化风险矩阵（严重度、发生率、缓解措施、证据引用）。

---

## 2. 结构化断言契约 (Structured Claims)

每位专家代理在其分析输出中必须包含结构化断言列表，便于 Orchestrator 提取与聚合：

```json
{
  "category": "capability | integration | risk",
  "target": "n_lift | f_mass | system_ros",
  "status": "verified | feasible | gap | unknown",
  "claim": "机械臂额定负载 5kg 满足当前需求，但在最大伸展半径下负载降至 3.5kg",
  "evidence_type": "wiki_doc | spec_sheet | inferred | missing",
  "citation": "wiki/hardware/arm_spec.md#L45",
  "confidence": "high | medium | low",
  "detail": "客户指定箱重最大 3kg，但在末端伸长 850mm 时需注意惯性力矩限制。"
}
```

---

## 3. 最终综合报告 (`grill_report.json`) 格式

Orchestrator 聚合三位专家的分析后，输出标准结构化综合报告：

```json
{
  "scenario_summary": {
    "task": "string",
    "target_robot": "string | null",
    "confirmed_parameters": {
      "f_id": {
        "label": "string",
        "value": "any",
        "unit": "string | null"
      }
    },
    "remaining_open_items": [
      {
        "id": "issue_id",
        "description": "string",
        "owner": "customer | engineering"
      }
    ]
  },
  "capabilities": {
    "summary": "string",
    "claims": [
      {
        "target": "string",
        "status": "verified | feasible | gap | unknown",
        "claim": "string",
        "citation": "string | null",
        "confidence": "high | medium | low"
      }
    ]
  },
  "architecture": {
    "summary": "string",
    "ros_nodes": [
      {
        "name": "string",
        "package": "string",
        "responsibility": "string",
        "interfaces": ["string"]
      }
    ],
    "integration_points": ["string"],
    "claims": []
  },
  "risk_matrix": {
    "summary": "string",
    "risks": [
      {
        "risk_id": "string",
        "severity": "high | medium | low",
        "likelihood": "high | medium | low",
        "description": "string",
        "mitigation": "string",
        "evidence_citation": "string | null"
      }
    ],
    "claims": []
  },
  "behavior_tree": {
    "root_id": "string",
    "nodes": []
  }
}
```
