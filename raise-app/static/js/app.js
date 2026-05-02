/* Raise App — JavaScript */
const Raise = {
  /* ─── Toast notifications ─── */
  toast(message, type = 'info') {
    let container = document.getElementById('toast-container');
    if (!container) {
      container = document.createElement('div');
      container.id = 'toast-container';
      container.className = 'toast-container';
      document.body.appendChild(container);
    }
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    const icons = { success: '✓', error: '✕', info: 'ℹ' };
    toast.innerHTML = `<span>${icons[type] || 'ℹ'}</span><span>${message}</span>`;
    container.appendChild(toast);
    setTimeout(() => { toast.style.opacity = '0'; toast.style.transform = 'translateX(100px)'; setTimeout(() => toast.remove(), 300); }, 4000);
  },

  /* ─── Loading overlay ─── */
  showLoading(msg = 'Processing...') {
    if (document.getElementById('loading-overlay')) return;
    const el = document.createElement('div');
    el.id = 'loading-overlay';
    el.className = 'loading-overlay';
    el.innerHTML = `<div class="spinner"></div><p>${msg}</p>`;
    document.body.appendChild(el);
  },
  hideLoading() {
    const el = document.getElementById('loading-overlay');
    if (el) el.remove();
  },

  /* ─── Preview email ─── */
  _previewTimeout: null,
  async previewEmail() {
    const subject = document.getElementById('email-subject')?.value || '';
    const body = document.getElementById('email-body')?.value || '';
    
    if (!subject && !body) return;

    if (this._previewTimeout) clearTimeout(this._previewTimeout);
    
    this._previewTimeout = setTimeout(async () => {
      // Pick first recipient for preview
      const chips = document.querySelectorAll('.recipient-chip.selected');
      let recipient = {};
      if (chips.length > 0) {
        recipient = JSON.parse(chips[0].dataset.contact || '{}');
      }

      try {
        const resp = await fetch('/api/preview', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ subject, body, recipient }),
        });
        const data = await resp.json();
        const panel = document.getElementById('preview-panel');
        if (panel) {
          panel.innerHTML = `
            <div class="preview-from">From: ${document.getElementById('from-email')?.textContent || 'raise@swiftagents.org'}</div>
            <div class="preview-subject">${this.escapeHtml(data.subject)}</div>
            <div class="preview-body">${data.body.replace(/\n/g, '<br>')}</div>
          `;
        }
      } catch (e) {
        console.error('Preview failed:', e);
      }
    }, 400); // 400ms debounce
  },

  /* ─── Send emails ─── */
  async sendEmails() {
    const subject = document.getElementById('email-subject')?.value;
    const body = document.getElementById('email-body')?.value;
    const sheet = document.getElementById('sheet-select')?.value;

    if (!subject || !body || !sheet) {
      this.toast('Please fill in subject, body, and select a sheet', 'error');
      return;
    }

    const selectedChips = document.querySelectorAll('.recipient-chip.selected');
    let selectedEmails = [];
    selectedChips.forEach(chip => {
      const contact = JSON.parse(chip.dataset.contact || '{}');
      if (contact.Email) selectedEmails.push(contact.Email);
    });

    if (selectedEmails.length === 0) {
      // If none selected, send to all
      const allChips = document.querySelectorAll('.recipient-chip');
      allChips.forEach(chip => {
        const contact = JSON.parse(chip.dataset.contact || '{}');
        if (contact.Email) selectedEmails.push(contact.Email);
      });
    }

    if (selectedEmails.length === 0) {
      this.toast('No recipients found', 'error');
      return;
    }

    if (!confirm(`Send email to ${selectedEmails.length} recipient(s)?`)) return;

    const formData = new FormData();
    formData.append('subject', subject);
    formData.append('body', body);
    formData.append('sheet', sheet);
    formData.append('selected_emails', selectedEmails.join(','));

    // Attachments
    const fileInput = document.getElementById('attachment-input');
    if (fileInput?.files) {
      for (let i = 0; i < fileInput.files.length; i++) {
        formData.append(`attachments_${i}`, fileInput.files[i]);
      }
    }

    this.showLoading(`Sending to ${selectedEmails.length} recipients...`);

    try {
      const resp = await fetch('/api/send', { method: 'POST', body: formData });
      const data = await resp.json();
      this.hideLoading();

      if (data.success) {
        this.toast(`Sent: ${data.sent} | Failed: ${data.failed}`, data.failed > 0 ? 'error' : 'success');
      } else {
        this.toast(data.error || 'Send failed', 'error');
      }
    } catch (e) {
      this.hideLoading();
      this.toast('Send failed: ' + e.message, 'error');
    }
  },

  /* ─── Send test email ─── */
  async sendTestEmail() {
    const subject = document.getElementById('email-subject')?.value || '';
    const body = document.getElementById('email-body')?.value || '';
    const testEmail = prompt('Enter test email address:', 'abdulabiola21@gmail.com');
    if (!testEmail) return;

    const chips = document.querySelectorAll('.recipient-chip.selected');
    let recipient = {};
    if (chips.length > 0) recipient = JSON.parse(chips[0].dataset.contact || '{}');

    this.showLoading('Sending test email...');
    try {
      const resp = await fetch('/api/send-test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ subject, body, test_email: testEmail, recipient }),
      });
      const data = await resp.json();
      this.hideLoading();
      this.toast(data.message || data.error, data.success ? 'success' : 'error');
    } catch (e) {
      this.hideLoading();
      this.toast('Test send failed: ' + e.message, 'error');
    }
  },

  /* ─── Load recipients for selected sheet ─── */
  async loadRecipients(sheet) {
    const container = document.getElementById('recipients-container');
    if (!container) return;
    container.innerHTML = '<div class="spinner"></div>';

    try {
      const resp = await fetch(`/api/recipients?sheet=${encodeURIComponent(sheet)}`);
      const data = await resp.json();

      if (data.recipients.length === 0) {
        container.innerHTML = '<div class="empty-state"><p>No contacts with email in this sheet</p></div>';
        return;
      }

      let html = `<div class="checkbox-wrap" style="margin-bottom:8px"><input type="checkbox" id="select-all" onchange="Raise.toggleSelectAll(this.checked)"><label for="select-all" style="font-size:13px;color:var(--text-secondary)">Select all (${data.recipients.length})</label></div><div class="recipients-preview">`;
      data.recipients.forEach(r => {
        const contactJson = JSON.stringify(r).replace(/"/g, '&quot;');
        const name = r['Full Name'] || r['Email'];
        html += `<span class="recipient-chip selected" data-contact="${contactJson}" onclick="this.classList.toggle('selected')">${name} · ${r['Email']}</span>`;
      });
      html += '</div>';
      container.innerHTML = html;
      this.previewEmail();
    } catch (e) {
      container.innerHTML = '<p style="color:var(--danger)">Failed to load recipients</p>';
    }
  },

  toggleSelectAll(checked) {
    document.querySelectorAll('.recipient-chip').forEach(chip => {
      if (checked) chip.classList.add('selected');
      else chip.classList.remove('selected');
    });
    this.previewEmail();
  },

  /* ─── File upload handling ─── */
  initFileUpload() {
    const area = document.getElementById('file-upload-area');
    const input = document.getElementById('attachment-input');
    const list = document.getElementById('file-list');
    if (!area || !input) return;

    area.addEventListener('click', () => input.click());
    area.addEventListener('dragover', e => { e.preventDefault(); area.style.borderColor = 'var(--accent)'; });
    area.addEventListener('dragleave', () => { area.style.borderColor = ''; });
    area.addEventListener('drop', e => {
      e.preventDefault();
      area.style.borderColor = '';
      input.files = e.dataTransfer.files;
      this.updateFileList();
    });
    input.addEventListener('change', () => this.updateFileList());
  },

  updateFileList() {
    const input = document.getElementById('attachment-input');
    const list = document.getElementById('file-list');
    if (!input || !list) return;

    list.innerHTML = '';
    Array.from(input.files).forEach((f, i) => {
      const size = f.size > 1048576 ? (f.size / 1048576).toFixed(1) + ' MB' : (f.size / 1024).toFixed(1) + ' KB';
      list.innerHTML += `<div class="file-item"><span>📎 ${this.escapeHtml(f.name)} (${size})</span></div>`;
    });
  },

  /* ─── Formatting toolbar ─── */
  insertFormat(tag) {
    const textarea = document.getElementById('email-body');
    if (!textarea) return;
    const start = textarea.selectionStart;
    const end = textarea.selectionEnd;
    const text = textarea.value;
    const selected = text.substring(start, end);

    const formats = {
      'bold': { prefix: '<b>', suffix: '</b>' },
      'italic': { prefix: '<i>', suffix: '</i>' },
      'underline': { prefix: '<u>', suffix: '</u>' },
      'link': { prefix: '<a href="URL">', suffix: '</a>' },
      'h1': { prefix: '<h1>', suffix: '</h1>' },
      'h2': { prefix: '<h2>', suffix: '</h2>' },
      'br': { prefix: '<br>\n', suffix: '' },
      'ul': { prefix: '<ul>\n<li>', suffix: '</li>\n</ul>' },
    };

    const fmt = formats[tag];
    if (!fmt) return;

    textarea.value = text.substring(0, start) + fmt.prefix + selected + fmt.suffix + text.substring(end);
    textarea.focus();
    textarea.selectionStart = start + fmt.prefix.length;
    textarea.selectionEnd = start + fmt.prefix.length + selected.length;
    this.previewEmail();
  },

  insertPlaceholder(placeholder) {
    const textarea = document.getElementById('email-body');
    if (!textarea) return;
    const pos = textarea.selectionStart;
    textarea.value = textarea.value.substring(0, pos) + placeholder + textarea.value.substring(pos);
    textarea.focus();
    textarea.selectionStart = textarea.selectionEnd = pos + placeholder.length;
    this.previewEmail();
  },

  escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
  },

  /* ─── Mobile sidebar toggle ─── */
  toggleSidebar() {
    document.querySelector('.sidebar')?.classList.toggle('open');
  }
};

document.addEventListener('DOMContentLoaded', () => {
  Raise.initFileUpload();
});
