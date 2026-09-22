import { SessionCreateResponse } from './types';
import { t } from '../i18n';

export const ALLOWED_EXTENSIONS = ['.pdf', '.png', '.jpg', '.jpeg', '.webp', '.gif', '.md', '.txt'];

export function isAllowedGrillExtension(filename: string): boolean {
  const ext = '.' + filename.split('.').pop()?.toLowerCase();
  return ALLOWED_EXTENSIONS.includes(ext);
}

export const SUPPORTED_ROBOTS = [
  { id: 'Walker_Tienkung_DEX', en: 'Walker_Tienkung_DEX', zh: '天工行者DEX' },
  { id: 'Walker_C1', en: 'Walker_C1', zh: 'Walker_C1_EDU共创者' },
  { id: 'TienKung', en: 'TienKung', zh: '天工行者无界&无疆' },
  { id: 'Walker_S2', en: 'Walker_S2', zh: 'Walker_S2_EDU探索者' },
];

export function formatRobotName(name: string | null | undefined): string {
  if (!name) return t('Generic / Undefined', '通用 / 未指定');
  const found = SUPPORTED_ROBOTS.find(r => r.id === name || r.en === name || r.zh === name);
  if (found) return t(found.en, found.zh);
  return name;
}

export class GrillIntakeView {
  private container: HTMLElement;
  private onNavigate: (path: string) => void;
  private selectedFiles: File[] = [];
  private isSubmitting = false;

  constructor(container: HTMLElement, onNavigate: (path: string) => void) {
    this.container = container;
    this.onNavigate = onNavigate;
  }

  public render(): void {
    this.container.replaceChildren();

    const wrapper = document.createElement('div');
    wrapper.className = 'grill-intake-container';

    // Header banner
    const header = document.createElement('div');
    header.className = 'grill-header';
    header.innerHTML = `
      <div class="grill-badge">${t('🤖 Robot Scenario Grill Bot', '🤖 机器人场景推演')}</div>
      <h1 class="grill-title">${t('Turn-based Scenario Modeling & Assessment', '基于多轮问答的机器人场景建模与评估')}</h1>
      <p class="grill-subtitle">
        ${t(
          'Describe what you want the robot to do. Answer a few focused questions to clarify your scenario, then receive an assessment of hardware capabilities, system integration, and operational risks.',
          '描述您希望机器人完成的任务，通过几轮有针对性的问答明确场景需求，随后获取硬件能力、系统集成与运行风险的评估报告。'
        )}
      </p>
    `;
    wrapper.appendChild(header);

    // Form
    const formCard = document.createElement('div');
    formCard.className = 'grill-card intake-form-card';

    // Notice banner
    const noticeEl = document.createElement('div');
    noticeEl.className = 'grill-notice';
    noticeEl.style.display = 'none';
    formCard.appendChild(noticeEl);

    // Task intent textarea
    const intentGroup = document.createElement('div');
    intentGroup.className = 'grill-form-group';
    intentGroup.innerHTML = `
      <label class="grill-label" for="grill-intent">
        ${t('Task Scenario Intent', '任务场景意图')} <span class="required">*</span>
      </label>
      <div class="grill-hint">${t('Describe what the robot needs to do, the environment, and any operational goals.', '描述机器人需要执行的任务、工作环境以及具体操作目标。')}</div>
    `;
    const intentInput = document.createElement('textarea');
    intentInput.id = 'grill-intent';
    intentInput.className = 'grill-textarea';
    intentInput.rows = 4;
    intentInput.placeholder = t(
      'e.g. Carry 5kg parts across an active warehouse floor with obstacles to packing station B. Must handle human cross-traffic and low light.',
      '例如：使用四足机器人搬运 5kg 零件穿越有障碍物的车间，前往 B 包装工位。需应对人员穿行和弱光照环境。'
    );
    intentGroup.appendChild(intentInput);
    formCard.appendChild(intentGroup);

    // Referenced robot
    const robotGroup = document.createElement('div');
    robotGroup.className = 'grill-form-group';
    robotGroup.innerHTML = `
      <label class="grill-label" for="grill-robot">${t('Target Robot Model', '目标机器人型号')}</label>
      <div class="grill-hint">${t(
        'Please choose from the 4 supported robot models below (or leave blank if undecided):',
        '请从以下4款支持的机器人型号中进行选择（若尚未确定可留空）：'
      )}</div>
    `;

    const robotSelect = document.createElement('select');
    robotSelect.id = 'grill-robot';
    robotSelect.className = 'grill-select';

    const defaultOption = document.createElement('option');
    defaultOption.value = '';
    defaultOption.textContent = t('-- Undecided / Leave blank --', '-- 尚未确定 / 留空 --');
    robotSelect.appendChild(defaultOption);

    for (const r of SUPPORTED_ROBOTS) {
      const opt = document.createElement('option');
      opt.value = r.id;
      opt.textContent = t(r.en, r.zh);
      robotSelect.appendChild(opt);
    }

    robotGroup.appendChild(robotSelect);
    formCard.appendChild(robotGroup);

    // File attachments
    const fileGroup = document.createElement('div');
    fileGroup.className = 'grill-form-group';
    fileGroup.innerHTML = `
      <label class="grill-label">${t('Supporting Documents & Diagrams (Optional)', '支持文档与图纸（可选）')}</label>
      <div class="grill-hint">${t('Attach floor plans, payload specs, sensor datasheets, or markdown notes (PDF, PNG, JPG, WebP, MD, TXT).', '可上传场地平面图、负载规范、传感器数据表或 Markdown 备忘（支持 PDF、PNG、JPG、WebP、MD、TXT）。')}</div>
    `;

    const dropZone = document.createElement('div');
    dropZone.className = 'grill-dropzone';
    dropZone.innerHTML = `
      <div class="dropzone-icon">📁</div>
      <div class="dropzone-text">${t('Click to select files or drag & drop here', '点击选择文件或拖拽至此处')}</div>
      <div class="dropzone-types">${t('PDF, PNG, JPG, WebP, Markdown, TXT (Max 32 MB total)', 'PDF、PNG、JPG、WebP、Markdown、TXT（总大小上限 32 MB）')}</div>
    `;

    const hiddenFileInput = document.createElement('input');
    hiddenFileInput.type = 'file';
    hiddenFileInput.multiple = true;
    hiddenFileInput.accept = ALLOWED_EXTENSIONS.join(',');
    hiddenFileInput.style.display = 'none';

    dropZone.addEventListener('click', () => hiddenFileInput.click());
    dropZone.addEventListener('dragover', (e) => {
      e.preventDefault();
      dropZone.classList.add('dragover');
    });
    dropZone.addEventListener('dragleave', () => dropZone.classList.remove('dragover'));
    dropZone.addEventListener('drop', (e) => {
      e.preventDefault();
      dropZone.classList.remove('dragover');
      if (e.dataTransfer?.files) {
        this.addFiles(Array.from(e.dataTransfer.files), fileListEl, noticeEl);
      }
    });

    hiddenFileInput.addEventListener('change', () => {
      if (hiddenFileInput.files) {
        this.addFiles(Array.from(hiddenFileInput.files), fileListEl, noticeEl);
      }
    });

    fileGroup.appendChild(dropZone);
    fileGroup.appendChild(hiddenFileInput);

    const fileListEl = document.createElement('div');
    fileListEl.className = 'grill-file-list';
    fileGroup.appendChild(fileListEl);
    formCard.appendChild(fileGroup);

    // Submit actions
    const actionsGroup = document.createElement('div');
    actionsGroup.className = 'grill-actions';

    const submitBtn = document.createElement('button');
    submitBtn.type = 'button';
    submitBtn.className = 'grill-btn grill-btn-primary';
    submitBtn.textContent = t('Start Scenario Interview  →', '开始场景访谈  →');
    submitBtn.addEventListener('click', () => {
      const intent = intentInput.value.trim();
      const robot = robotSelect.value || null;
      if (!intent) {
        this.showNotice(noticeEl, t('Please enter a task scenario intent before starting.', '请在开始前填写任务场景意图。'), true);
        intentInput.focus();
        return;
      }
      this.startInterview(intent, robot, submitBtn, noticeEl);
    });

    actionsGroup.appendChild(submitBtn);
    formCard.appendChild(actionsGroup);

    wrapper.appendChild(formCard);
    this.container.appendChild(wrapper);
  }

  private addFiles(newFiles: File[], listEl: HTMLElement, noticeEl: HTMLElement): void {
    for (const f of newFiles) {
      const ext = '.' + f.name.split('.').pop()?.toLowerCase();
      if (!ALLOWED_EXTENSIONS.includes(ext)) {
        this.showNotice(noticeEl, t(`File "${f.name}" has unsupported format. Only ${ALLOWED_EXTENSIONS.join(', ')} are allowed.`, `文件 "${f.name}" 格式不支持。仅支持：${ALLOWED_EXTENSIONS.join(', ')}`), true);
        continue;
      }
      if (!this.selectedFiles.some(existing => existing.name === f.name && existing.size === f.size)) {
        this.selectedFiles.push(f);
      }
    }
    this.renderFileList(listEl);
  }

  private renderFileList(listEl: HTMLElement): void {
    listEl.replaceChildren();
    for (let i = 0; i < this.selectedFiles.length; i++) {
      const file = this.selectedFiles[i];
      const item = document.createElement('div');
      item.className = 'grill-file-item';

      const info = document.createElement('span');
      info.className = 'grill-file-info';
      info.textContent = `${file.name} (${this.formatSize(file.size)})`;

      const removeBtn = document.createElement('button');
      removeBtn.type = 'button';
      removeBtn.className = 'grill-file-remove';
      removeBtn.innerHTML = '&times;';
      removeBtn.title = t('Remove file', '移除文件');
      removeBtn.addEventListener('click', () => {
        this.selectedFiles.splice(i, 1);
        this.renderFileList(listEl);
      });

      item.appendChild(info);
      item.appendChild(removeBtn);
      listEl.appendChild(item);
    }
  }

  private formatSize(bytes: number): string {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }

  private showNotice(el: HTMLElement, message: string, isError = false): void {
    el.textContent = message;
    el.className = `grill-notice ${isError ? 'grill-notice-error' : 'grill-notice-info'}`;
    el.style.display = 'block';
  }

  private async startInterview(intent: string, robot: string | null, submitBtn: HTMLButtonElement, noticeEl: HTMLElement): Promise<void> {
    if (this.isSubmitting) return;
    this.isSubmitting = true;
    submitBtn.disabled = true;
    submitBtn.textContent = t('Creating private session...', '正在创建私密会话...');

    const hasFiles = this.selectedFiles.length > 0;

    try {
      this.showNotice(noticeEl, t('Initializing secure scenario session...', '正在初始化场景私密会话...'), false);
      const res = await fetch('/api/grill/sessions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ task_intent: intent, referenced_robot: robot, defer_turn: hasFiles }),
      });

      if (!res.ok) {
        const errorData = await res.json().catch(() => ({}));
        throw new Error(errorData.detail || `Session creation failed: HTTP ${res.status}`);
      }

      const data: SessionCreateResponse = await res.json();
      const token = data.token;
      const sessionId = data.session.id;

      // Upload attached files if any
      if (hasFiles) {
        for (let i = 0; i < this.selectedFiles.length; i++) {
          const file = this.selectedFiles[i];
          submitBtn.textContent = t(`Uploading file ${i + 1} of ${this.selectedFiles.length}...`, `正在上传第 ${i + 1} / ${this.selectedFiles.length} 个文件...`);
          this.showNotice(noticeEl, t(`Uploading "${file.name}"...`, `正在上传 "${file.name}"...`), false);

          const uploadRes = await fetch(`/api/grill/sessions/${sessionId}/files?name=${encodeURIComponent(file.name)}`, {
            method: 'PUT',
            headers: {
              'Authorization': `Bearer ${token}`,
              'Content-Type': 'application/octet-stream',
            },
            body: file,
          });

          if (!uploadRes.ok) {
            console.warn(`File upload warning for ${file.name}: HTTP ${uploadRes.status}`);
          }
        }

        // Now start the turn after all files are safely uploaded and indexed!
        submitBtn.textContent = t('Starting interview turn...', '正在启动访谈轮次...');
        await fetch(`/api/grill/sessions/${sessionId}/start`, {
          method: 'POST',
          headers: {
            'Authorization': `Bearer ${token}`,
          },
        });
      }

      this.showNotice(noticeEl, t('Session ready! Launching interview...', '会话已就绪！正在启动访谈...'), false);
      // Navigate to the private session link
      this.onNavigate(`/grill/s/${token}`);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      this.showNotice(noticeEl, `${t('Error', '错误')}: ${msg}`, true);
      submitBtn.disabled = false;
      submitBtn.textContent = t('Start Scenario Interview  →', '开始场景访谈  →');
      this.isSubmitting = false;
    }
  }
}
