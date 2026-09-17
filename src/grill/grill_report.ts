import { GrillReport, GrillSession, BehaviorTreeNode } from './types';
import { formatRobotName } from './grill_intake';
import { t } from '../i18n';
import { QuestionsPanel } from '../questions';

export type GrillReportTab = 'scenario' | 'capabilities' | 'architecture' | 'risk' | 'tree';

export class GrillReportView {
  private report: GrillReport;
  private session: GrillSession;
  private container: HTMLElement;
  private activeTab: GrillReportTab = 'scenario';

  constructor(report: GrillReport, session: GrillSession, container: HTMLElement) {
    this.report = report;
    this.session = session;
    this.container = container;
  }

  public render(): void {
    this.container.replaceChildren();

    const wrapper = document.createElement('div');
    wrapper.className = 'grill-report-wrapper';

    // Top report header
    const header = document.createElement('div');
    header.className = 'grill-report-header';
    header.innerHTML = `
      <div class="report-header-top">
        <div>
          <span class="grill-badge status-badge completed">${t('Report Ready', '评估报告已就绪')}</span>
          <h1 class="report-title">${this.escape(this.session.task_intent || t('Robot Scenario Analysis', '机器人场景分析'))}</h1>
        </div>
        <div class="report-export-actions">
          <button type="button" class="grill-btn grill-btn-secondary" id="btn-export-json">${t('Export JSON', '导出 JSON')}</button>
          <button type="button" class="grill-btn grill-btn-secondary" id="btn-export-md">${t('Export Markdown', '导出 Markdown')}</button>
        </div>
      </div>
      <div class="report-meta">
        <span><strong>${t('Robot:', '机器人：')}</strong> ${this.escape(formatRobotName(this.session.referenced_robot))}</span>
        <span><strong>${t('Questions Answered:', '已回答问题数：')}</strong> ${this.session.question_count} / 25</span>
        <span><strong>${t('Completed:', '完成时间：')}</strong> ${new Date(this.session.finished_at || Date.now()).toLocaleString()}</span>
      </div>
    `;
    wrapper.appendChild(header);

    // Tab navigation
    const tabNav = document.createElement('nav');
    tabNav.className = 'grill-tabs';

    const tabs: Array<{ id: GrillReportTab; label: string; icon: string }> = [
      { id: 'scenario', label: t('Scenario & Goal', '场景与目标'), icon: '🎯' },
      { id: 'capabilities', label: t('Robot Capabilities', '机器人能力评估'), icon: '🤖' },
      { id: 'architecture', label: t('System Architecture', '系统集成架构'), icon: '⚙️' },
      { id: 'risk', label: t('Risk & Evidence Matrix', '风险与引证矩阵'), icon: '🛡️' },
      { id: 'tree', label: t('Behavior Tree Visualizer', '行为树可视化'), icon: '🌲' },
    ];

    for (const tab of tabs) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = `grill-tab-btn ${this.activeTab === tab.id ? 'active' : ''}`;
      btn.innerHTML = `<span class="tab-icon">${tab.icon}</span> ${tab.label}`;
      btn.addEventListener('click', () => {
        this.activeTab = tab.id;
        this.render();
      });
      tabNav.appendChild(btn);
    }
    wrapper.appendChild(tabNav);

    // Tab content container
    const contentEl = document.createElement('div');
    contentEl.className = 'grill-tab-content';

    switch (this.activeTab) {
      case 'scenario':
        contentEl.appendChild(this.renderScenarioTab());
        break;
      case 'capabilities':
        contentEl.appendChild(this.renderCapabilitiesTab());
        break;
      case 'architecture':
        contentEl.appendChild(this.renderArchitectureTab());
        break;
      case 'risk':
        contentEl.appendChild(this.renderRiskTab());
        break;
      case 'tree':
        contentEl.appendChild(this.renderTreeTab());
        break;
    }

    wrapper.appendChild(contentEl);

    // Attach post-interview streaming Q&A panel below the report
    const qaPanel = new QuestionsPanel(this.session.id, '/api/grill/sessions', this.session.token);
    wrapper.appendChild(qaPanel.element);
    void qaPanel.poll();

    this.container.appendChild(wrapper);

    // Bind export buttons
    const btnJson = wrapper.querySelector('#btn-export-json');
    btnJson?.addEventListener('click', () => this.downloadJson());
    const btnMd = wrapper.querySelector('#btn-export-md');
    btnMd?.addEventListener('click', () => this.downloadMarkdown());
  }

  private renderScenarioTab(): HTMLElement {
    const card = document.createElement('div');
    card.className = 'grill-card report-panel';

    const summaryText = this.report.scenario_summary || this.session.readback_summary || this.session.task_intent;
    const tree = this.report.behavior_tree || this.session.scenario_state;
    const nodeCount = tree?.nodes?.length || 0;

    card.innerHTML = `
      <h2 class="panel-heading">${t('🎯 Confirmed Scenario & Objectives', '🎯 确认的场景与目标')}</h2>
      <div class="scenario-summary-box">
        <p class="summary-paragraph">${this.escape(summaryText)}</p>
      </div>

      <div class="metrics-grid">
        <div class="metric-card">
          <div class="metric-value">${this.escape(formatRobotName(this.session.referenced_robot))}</div>
          <div class="metric-label">${t('Target Hardware', '目标硬件')}</div>
        </div>
        <div class="metric-card">
          <div class="metric-value">${this.session.question_count}</div>
          <div class="metric-label">${t('Interview Turns / Questions', '访谈轮次 / 问题数')}</div>
        </div>
        <div class="metric-card">
          <div class="metric-value">${nodeCount}</div>
          <div class="metric-label">${t('Modeled BT Nodes', '建模行为树节点数')}</div>
        </div>
        <div class="metric-card">
          <div class="metric-value">${this.report.risk_matrix?.risks?.length || 0}</div>
          <div class="metric-label">${t('Assessed Risks', '评估风险项数')}</div>
        </div>
      </div>

      <h3 class="panel-subheading">${t('Operational Context', '运行上下文')}</h3>
      <ul class="context-list">
        <li><strong>${t('Task Intent:', '任务意图：')}</strong> ${this.escape(this.session.task_intent)}</li>
        <li><strong>${t('Behavior Tree Root:', '行为树根节点：')}</strong> <code>${this.escape(tree?.root_id || 'root')}</code></li>
        <li><strong>${t('Revision Index:', '迭代版本：')}</strong> Rev ${this.session.current_revision}</li>
      </ul>
    `;

    if (this.session.files && this.session.files.length > 0) {
      const filesCard = document.createElement('div');
      filesCard.className = 'grill-card session-files-card';
      filesCard.style.marginTop = '20px';
      const h3 = document.createElement('h3');
      h3.textContent = `${t('Uploaded Documents & Diagrams', '上传参考文档与图纸')} (${this.session.files.length})`;
      filesCard.appendChild(h3);

      for (const f of this.session.files) {
        const fileRow = document.createElement('div');
        fileRow.className = 'file-item';
        const fileLink = document.createElement('a');
        fileLink.href = `/api/grill/sessions/${this.session.id}/files/${f.id}?token=${encodeURIComponent(this.session.token || '')}`;
        fileLink.target = '_blank';
        fileLink.className = 'file-link';
        fileLink.textContent = `📎 ${f.name} (${f.size} B)`;
        fileRow.appendChild(fileLink);
        filesCard.appendChild(fileRow);
      }
      card.appendChild(filesCard);
    }

    if (this.session.turns && this.session.turns.length > 0) {
      const turnsCard = document.createElement('div');
      turnsCard.className = 'grill-card turns-history-card';
      turnsCard.style.marginTop = '20px';
      const h3 = document.createElement('h3');
      h3.textContent = t('Past Turn Transcript', '往轮问答推演记录');
      turnsCard.appendChild(h3);

      for (const turn of this.session.turns) {
        const turnRow = document.createElement('div');
        turnRow.className = 'turn-row';
        turnRow.setAttribute('data-turn-index', String(turn.turn_index));

        const turnHeader = document.createElement('div');
        turnHeader.className = 'turn-row-header';
        turnHeader.textContent = `${t('Turn', '第')} ${turn.turn_index} ${t('', '轮问答')}`;
        turnRow.appendChild(turnHeader);

        for (const q of turn.questions) {
          const qBox = document.createElement('div');
          qBox.className = 'past-question-box';
          qBox.textContent = `Q: ${q.text} ${q.why ? `(💡 ${q.why})` : ''}`;
          turnRow.appendChild(qBox);
        }
        for (const a of turn.answers) {
          const aBox = document.createElement('div');
          aBox.className = 'past-answer-box';
          const ansText = a.selected_option || a.free_text_answer || (a.is_unknown ? 'Unknown' : '');
          aBox.textContent = `A: ${ansText}`;
          turnRow.appendChild(aBox);
        }
        turnsCard.appendChild(turnRow);
      }
      card.appendChild(turnsCard);
    }

    return card;
  }

  private renderCapabilitiesTab(): HTMLElement {
    const card = document.createElement('div');
    card.className = 'grill-card report-panel';

    const caps = this.report.capabilities;
    const claims = caps?.claims || [];

    let html = `
      <h2 class="panel-heading">${t('🤖 Robot Hardware & Capabilities Assessment', '🤖 机器人硬件与能力评估')}</h2>
      <p class="panel-intro">${this.escape(caps?.summary || t('Analysis of robot physical capabilities, payload limits, reach, and perception suitability.', '分析机器人物理硬件能力、有效负载极限、机械臂工作半径及感知适配性。'))}</p>
    `;

    if (claims.length === 0) {
      html += `<div class="empty-state">${t('No specific hardware claims recorded.', '暂无具体硬件指标断言记录。')}</div>`;
    } else {
      html += `
        <div class="claims-table-wrapper">
          <table class="grill-table">
            <thead>
              <tr>
                <th>${t('Status', '状态')}</th>
                <th>${t('Category', '分类')}</th>
                <th>${t('Claim / Capability', '能力 / 指标断言')}</th>
                <th>${t('Assessment Statement', '评估论断')}</th>
                <th>${t('Citations', '引证依据')}</th>
              </tr>
            </thead>
            <tbody>
      `;

      for (const claim of claims) {
        const statusClass = `claim-badge-${claim.status}`;
        const citations = (claim.citations || []).map(c => `<span class="citation-tag">${this.escape(c)}</span>`).join(' ');
        html += `
          <tr>
            <td><span class="claim-badge ${statusClass}">${this.escape(claim.status)}</span></td>
            <td><strong>${this.escape(claim.category)}</strong></td>
            <td>${this.escape(claim.title)}</td>
            <td>${this.escape(claim.statement)}</td>
            <td>${citations || '<span class="text-muted">—</span>'}</td>
          </tr>
        `;
      }

      html += `
            </tbody>
          </table>
        </div>
      `;
    }

    card.innerHTML = html;
    return card;
  }

  private renderArchitectureTab(): HTMLElement {
    const card = document.createElement('div');
    card.className = 'grill-card report-panel';

    const arch = this.report.system_architecture;
    const nodes = arch?.nodes || [];

    let html = `
      <h2 class="panel-heading">${t('⚙️ Proposed Integration Architecture', '⚙️ 推荐系统集成架构')}</h2>
      <p class="panel-intro">${this.escape(arch?.summary || t('ROS 2 software architecture, node topology, and communication graph.', 'ROS 2 软件架构、节点拓扑与通信图谱。'))}</p>
      <div class="arch-meta-box">
        <span><strong>${t('Middleware:', '中间件：')}</strong> <code>${this.escape(arch?.middleware || 'ROS 2 Humble / CycloneDDS')}</code></span>
      </div>
    `;

    if (nodes.length === 0) {
      html += `<div class="empty-state">${t('No ROS 2 nodes defined.', '暂无 ROS 2 节点定义。')}</div>`;
    } else {
      html += `
        <h3 class="panel-subheading">${t('ROS 2 Nodes & Interfaces', 'ROS 2 节点与接口')}</h3>
        <div class="arch-nodes-grid">
      `;

      for (const node of nodes) {
        const subTopics = (node.topics_sub || []).map(t => `<code>${this.escape(t)}</code>`).join(', ');
        const pubTopics = (node.topics_pub || []).map(t => `<code>${this.escape(t)}</code>`).join(', ');

        html += `
          <div class="arch-node-card">
            <div class="node-header">
              <span class="node-package">${this.escape(node.package)}</span>
              <strong class="node-name">${this.escape(node.name)}</strong>
              <span class="node-type">${this.escape(node.type)}</span>
            </div>
            <div class="node-topics">
              ${subTopics ? `<div class="topic-row"><span>Sub:</span> ${subTopics}</div>` : ''}
              ${pubTopics ? `<div class="topic-row"><span>Pub:</span> ${pubTopics}</div>` : ''}
            </div>
          </div>
        `;
      }

      html += `</div>`;
    }

    if (arch?.recommendations && arch.recommendations.length > 0) {
      html += `
        <h3 class="panel-subheading">${t('Integration Recommendations', '集成架构建议')}</h3>
        <ul class="recommendations-list">
          ${arch.recommendations.map(r => `<li>${this.escape(r)}</li>`).join('')}
        </ul>
      `;
    }

    card.innerHTML = html;
    return card;
  }

  private renderRiskTab(): HTMLElement {
    const card = document.createElement('div');
    card.className = 'grill-card report-panel';

    const rm = this.report.risk_matrix;
    const risks = rm?.risks || [];

    let html = `
      <h2 class="panel-heading">${t('🛡️ Operational Risk & Evidence Matrix', '🛡️ 运行风险与证据矩阵')}</h2>
      <p class="panel-intro">${this.escape(rm?.summary || t('Identified failure modes, safety boundaries, and recommended mitigations.', '识别的故障模式、安全边界及推荐的缓解措施。'))}</p>
    `;

    if (risks.length === 0) {
      html += `<div class="empty-state">${t('No risk items cataloged.', '暂无编目的风险项。')}</div>`;
    } else {
      html += `
        <div class="risks-table-wrapper">
          <table class="grill-table">
            <thead>
              <tr>
                <th>${t('Severity', '严重性')}</th>
                <th>${t('Likelihood', '可能性')}</th>
                <th>${t('Hazard / Risk Title', '危险源 / 风险标题')}</th>
                <th>${t('Mitigation Strategy', '缓解策略')}</th>
                <th>${t('Evidence / Citations', '证据 / 引证')}</th>
              </tr>
            </thead>
            <tbody>
      `;

      for (const risk of risks) {
        const sevClass = `risk-sev-${risk.severity}`;
        const likeClass = `risk-like-${risk.likelihood}`;
        const citations = (risk.citations || []).map(c => `<span class="citation-tag">${this.escape(c)}</span>`).join(' ');

        html += `
          <tr>
            <td><span class="risk-badge ${sevClass}">${this.escape(risk.severity)}</span></td>
            <td><span class="risk-badge ${likeClass}">${this.escape(risk.likelihood)}</span></td>
            <td><strong>${this.escape(risk.title)}</strong></td>
            <td>${this.escape(risk.mitigation)}</td>
            <td>${citations || '<span class="text-muted">—</span>'}</td>
          </tr>
        `;
      }

      html += `
            </tbody>
          </table>
        </div>
      `;
    }

    card.innerHTML = html;
    return card;
  }

  private renderTreeTab(): HTMLElement {
    const card = document.createElement('div');
    card.className = 'grill-card report-panel';

    const treeData = this.report.behavior_tree || this.session.scenario_state;
    const nodes = treeData?.nodes || [];
    const rootId = treeData?.root_id || (nodes[0] ? nodes[0].id : 'root');

    let html = `
      <h2 class="panel-heading">${t('🌲 Behavior Tree Visualizer', '🌲 行为树可视化')}</h2>
      <p class="panel-intro">${t('Formal control flow model synthesized during the interview turns.', '问答访谈过程中合成的形式化控制流模型。')}</p>
    `;

    if (nodes.length === 0) {
      html += `<div class="empty-state">${t('No Behavior Tree nodes recorded.', '暂无行为树节点记录。')}</div>`;
    } else {
      const nodeMap = new Map<string, BehaviorTreeNode>();
      for (const n of nodes) nodeMap.set(n.id, n);

      html += `<div class="bt-tree-container">`;
      html += this.renderTreeNode(rootId, nodeMap, 0);
      html += `</div>`;
    }

    card.innerHTML = html;
    return card;
  }

  private renderTreeNode(nodeId: string, nodeMap: Map<string, BehaviorTreeNode>, depth: number): string {
    const node = nodeMap.get(nodeId);
    if (!node) return '';

    const iconMap: Record<string, string> = {
      sequence: t('➡️ Sequence', '➡️ 顺序节点 (Sequence)'),
      fallback: t('❓ Fallback', '❓ 选择节点 (Fallback)'),
      action: t('⚡ Action', '⚡ 动作节点 (Action)'),
      condition: t('🔍 Condition', '🔍 条件节点 (Condition)'),
      decorator: t('🔄 Decorator', '🔄 装饰节点 (Decorator)'),
    };

    const typeLabel = iconMap[node.type] || node.type;
    const typeClass = `bt-node-${node.type}`;

    let html = `
      <div class="bt-node-item" style="margin-left: ${depth * 24}px">
        <div class="bt-node-box ${typeClass}">
          <span class="bt-node-type-pill">${this.escape(typeLabel)}</span>
          <strong class="bt-node-name">${this.escape(node.name || node.id)}</strong>
          ${node.description ? `<span class="bt-node-desc">${this.escape(node.description)}</span>` : ''}
        </div>
      </div>
    `;

    if (node.children && node.children.length > 0) {
      for (const childId of node.children) {
        html += this.renderTreeNode(childId, nodeMap, depth + 1);
      }
    }

    return html;
  }

  private downloadJson(): void {
    const blob = new Blob([JSON.stringify(this.report, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `grill-report-${this.session.id}.json`;
    a.click();
    URL.revokeObjectURL(url);
  }

  private downloadMarkdown(): void {
    const md = generateGrillMarkdown(this.report, this.session);
    const blob = new Blob([md], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `grill-report-${this.session.id}.md`;
    a.click();
    URL.revokeObjectURL(url);
  }

  private escape(str: string): string {
    const p = document.createElement('p');
    p.textContent = str;
    return p.innerHTML;
  }
}

export function generateGrillMarkdown(report: GrillReport, session: GrillSession): string {
  const lines: string[] = [];
  lines.push(`# Robot Scenario Assessment: ${session.task_intent}`);
  lines.push(`**Target Robot:** ${formatRobotName(session.referenced_robot)}`);
  lines.push(`**Date:** ${new Date(session.finished_at || Date.now()).toISOString()}`);
  lines.push(`**Questions Answered:** ${session.question_count} / 30\n`);

  lines.push(`## 1. Scenario Summary\n${report.scenario_summary || session.readback_summary || ''}\n`);

  lines.push(`## 2. Capabilities Assessment\n${report.capabilities?.summary || ''}\n`);
  for (const c of report.capabilities?.claims || []) {
    lines.push(`- **[${c.status.toUpperCase()}]** ${c.title} (${c.category}): ${c.statement}`);
  }
  lines.push('');

  lines.push(`## 3. Integration Architecture\n${report.system_architecture?.summary || ''}\n`);
  for (const n of report.system_architecture?.nodes || []) {
    lines.push(`- **Node \`${n.name}\`** (\`${n.package}\`): Type \`${n.type}\``);
  }
  lines.push('');

  lines.push(`## 4. Operational Risk Matrix\n${report.risk_matrix?.summary || ''}\n`);
  for (const r of report.risk_matrix?.risks || []) {
    lines.push(`- **${r.title}** (Severity: ${r.severity}, Likelihood: ${r.likelihood}): ${r.mitigation}`);
  }
  lines.push('');

  return lines.join('\n');
}
