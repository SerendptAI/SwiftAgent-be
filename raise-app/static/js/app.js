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
    el.innerHTML = `<div class="spinner"></div><p id="loading-msg" style="text-align:center;">${msg}</p>`;
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
  async sendEmails(isTestMode = false) {
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
    formData.append('is_test_mode', isTestMode ? 'true' : 'false');

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
      
      const reader = resp.body.getReader();
      const decoder = new TextDecoder("utf-8");
      
      let buffer = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        
        buffer += decoder.decode(value, { stream: true });
        let lines = buffer.split('\n\n');
        buffer = lines.pop(); // Keep the incomplete part
        
        for (let line of lines) {
          if (line.startsWith('data: ')) {
            const data = JSON.parse(line.substring(6));
            
            if (data.type === 'progress') {
              const el = document.getElementById('loading-msg');
              if (el) {
                el.innerHTML = `Sending...<br><span style="font-size:12px;opacity:0.8;">${data.index} / ${data.total} | Sent: ${data.sent} | Failed: ${data.failed}</span><br><span style="font-size:10px;opacity:0.6;margin-top:4px;display:block;">${data.email}</span>`;
              }
            } else if (data.type === 'complete') {
              this.hideLoading();
              this.toast('Campaign send complete!', 'success');
            }
          }
        }
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

      let html = `<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
        <div class="checkbox-wrap"><input type="checkbox" id="select-all" checked onchange="Raise.toggleSelectAll(this.checked)"><label for="select-all" style="font-size:13px;color:var(--text-secondary)">Select all (${data.recipients.length})</label></div>
        <button type="button" class="btn btn-secondary" style="padding:4px 8px; font-size:12px;" onclick="Raise.excludeSent()">Exclude Already Sent</button>
      </div><div class="recipients-preview">`;
      data.recipients.forEach(r => {
        const contactJson = JSON.stringify(r).replace(/"/g, '&quot;');
        const name = r['Full Name'] || r['Email'];
        // Show status visually
        let statusBadge = '';
        if (r.last_status === 'sent') statusBadge = ' <span style="color:var(--success);font-size:10px;">(Sent)</span>';
        
        html += `<span class="recipient-chip selected" data-contact="${contactJson}" onclick="this.classList.toggle('selected')">${name} · ${r['Email']}${statusBadge}</span>`;
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

  excludeSent() {
    let excluded = 0;
    document.querySelectorAll('.recipient-chip').forEach(chip => {
      try {
        const contact = JSON.parse(chip.dataset.contact || '{}');
        if (contact.last_status === 'sent') {
          chip.classList.remove('selected');
          excluded++;
        }
      } catch (e) {}
    });
    this.previewEmail();
    this.toast(`Excluded ${excluded} previously sent contacts.`, 'info');
  },

  toggleLoadAll() {
    const btn = document.getElementById('btn-load-all');
    const select = document.getElementById('sheet-select');
    
    if (btn.classList.contains('active')) {
      // Toggle off
      btn.classList.remove('active');
      btn.textContent = 'Load All';
      btn.classList.replace('btn-primary', 'btn-secondary');
      
      select.disabled = false;
      select.value = '';
      
      // Remove the __ALL__ option if it exists
      const allOpt = Array.from(select.options).find(o => o.value === '__ALL__');
      if (allOpt) allOpt.remove();
      
      document.getElementById('recipients-container').innerHTML = '<div style="color:var(--text-muted);font-size:13px;padding:12px;">Select a sheet above to load recipients</div>';
      this.previewEmail();
    } else {
      // Toggle on
      btn.classList.add('active');
      btn.textContent = 'Clear All';
      btn.classList.replace('btn-secondary', 'btn-primary');
      
      let allOpt = Array.from(select.options).find(o => o.value === '__ALL__');
      if (!allOpt) {
        allOpt = document.createElement('option');
        allOpt.value = '__ALL__';
        allOpt.textContent = 'All Sheets (Combined)';
        select.appendChild(allOpt);
      }
      select.value = '__ALL__';
      select.disabled = true;
      
      this.loadRecipients('__ALL__');
    }
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
