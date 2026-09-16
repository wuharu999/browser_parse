import { SessionCreateResponse } from './types';

export const ALLOWED_EXTENSIONS = ['.pdf', '.png', '.jpg', '.jpeg', '.webp', '.gif', '.md', '.txt'];

export function isAllowedGrillExtension(filename: string): boolean {
  const ext = '.' + filename.split('.').pop()?.toLowerCase();
  return ALLOWED_EXTENSIONS.includes(ext);
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
      <div class="grill-badge">🤖 Robot Scenario Grill Bot</div>
      <h1 class="grill-title">Turn-based Scenario Modeling & Assessment</h1>
      <p class="grill-subtitle">
        Enter your target robotic task. Our orchestrator will interview you through targeted, multi-choice
        questions to formulate an explicit Behavior Tree, then deploy 3 specialist subagents to evaluate
        hardware capabilities, integration architecture, and operational risk.
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
        Task Scenario Intent <span class="required">*</span>
      </label>
      <div class="grill-hint">Describe what the robot needs to do, the environment, and any operational goals.</div>
    `;
    const intentInput = document.createElement('textarea');
    intentInput.id = 'grill-intent';
    intentInput.className = 'grill-textarea';
    intentInput.rows = 4;
    intentInput.placeholder = 'e.g. Carry 5kg parts across an active warehouse floor with obstacles to packing station B. Must handle human cross-traffic and low light.';
    intentGroup.appendChild(intentInput);
    formCard.appendChild(intentGroup);

    // Referenced robot
    const robotGroup = document.createElement('div');
    robotGroup.className = 'grill-form-group';
    robotGroup.innerHTML = `
      <label class="grill-label" for="grill-robot">Target Robot Hardware (Optional)</label>
      <div class="grill-hint">Specify the robot model if known (e.g. Unitree B2, Boston Dynamics Spot, UR5e). Leave blank if undecided.</div>
    `;
    const robotInput = document.createElement('input');
    robotInput.id = 'grill-robot';
    robotInput.type = 'text';
    robotInput.className = 'grill-input';
    robotInput.placeholder = 'e.g. Unitree B2 Quadruped';
    robotGroup.appendChild(robotInput);
    formCard.appendChild(robotGroup);

    // File attachments
    const fileGroup = document.createElement('div');
    fileGroup.className = 'grill-form-group';
    fileGroup.innerHTML = `
      <label class="grill-label">Supporting Documents & Diagrams (Optional)</label>
      <div class="grill-hint">Attach floor plans, payload specs, sensor datasheets, or markdown notes (PDF, PNG, JPG, WebP, MD, TXT).</div>
    `;

    const dropZone = document.createElement('div');
    dropZone.className = 'grill-dropzone';
    dropZone.innerHTML = `
      <div class="dropzone-icon">📁</div>
      <div class="dropzone-text">Click to select files or drag & drop here</div>
      <div class="dropzone-types">PDF, PNG, JPG, WebP, Markdown, TXT (Max 32 MB total)</div>
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
    submitBtn.textContent = 'Start Scenario Interview  →';
    submitBtn.addEventListener('click', () => {
      const intent = intentInput.value.trim();
      const robot = robotInput.value.trim() || null;
      if (!intent) {
        this.showNotice(noticeEl, 'Please enter a task scenario intent before starting.', true);
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
        this.showNotice(noticeEl, `File "${f.name}" has unsupported format. Only ${ALLOWED_EXTENSIONS.join(', ')} are allowed.`, true);
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
      removeBtn.title = 'Remove file';
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
    submitBtn.textContent = 'Creating private session...';

    try {
      this.showNotice(noticeEl, 'Initializing secure scenario session...', false);
      const res = await fetch('/api/grill/sessions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ task_intent: intent, referenced_robot: robot }),
      });

      if (!res.ok) {
        const errorData = await res.json().catch(() => ({}));
        throw new Error(errorData.detail || `Session creation failed: HTTP ${res.status}`);
      }

      const data: SessionCreateResponse = await res.json();
      const token = data.token;
      const sessionId = data.session.id;

      // Upload attached files if any
      if (this.selectedFiles.length > 0) {
        for (let i = 0; i < this.selectedFiles.length; i++) {
          const file = this.selectedFiles[i];
          submitBtn.textContent = `Uploading file ${i + 1} of ${this.selectedFiles.length}...`;
          this.showNotice(noticeEl, `Uploading "${file.name}"...`, false);

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
      }

      this.showNotice(noticeEl, 'Session ready! Launching interview...', false);
      // Navigate to the private session link
      this.onNavigate(`/grill/s/${token}`);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      this.showNotice(noticeEl, `Error: ${msg}`, true);
      submitBtn.disabled = false;
      submitBtn.textContent = 'Start Scenario Interview  →';
      this.isSubmitting = false;
    }
  }
}
