/**
 * HIVV.org Shared Utilities
 * Common functions for all pages - easy to maintain and extend
 * Version: 2.0 - Supports feature flags and versioning
 */

const HIVV = {
  API_BASE: '',
  STORAGE_KEY: 'hivv_session_token',
  CSRF_KEY: 'hivv_csrf_token',
  config: null,
  features: {},
  csrfToken: null,

  // ===== Config & Features =====
  async loadConfig() {
    try {
      const res = await fetch('/api/config');
      if (res.ok) {
        this.config = await res.json();
        this.features = this.config.features || {};
      }
    } catch (e) {
      console.warn('Failed to load config:', e);
    }
    return this.config;
  },

  isFeatureEnabled(feature) {
    return this.features[feature] !== false;
  },

  async fetchCsrfToken() {
    try {
      const res = await fetch('/api/auth/csrf-token');
      if (res.ok) {
        const data = await res.json();
        this.csrfToken = data.csrfToken;
        localStorage.setItem(this.CSRF_KEY, this.csrfToken);
        return this.csrfToken;
      }
    } catch (e) {
      console.warn('Failed to fetch CSRF token:', e);
    }
    return null;
  },

  // ===== HTTP Helpers =====
  async request(path, options = {}) {
    const token = localStorage.getItem(this.STORAGE_KEY);
    const headers = { 'Content-Type': 'application/json', ...options.headers };
    if (token) headers['X-Session-Token'] = token;
    if (this.csrfToken) headers['X-CSRF-Token'] = this.csrfToken;

    const res = await fetch(this.API_BASE + path, { ...options, headers });
    const data = await res.json();
    if (!res.ok && res.status === 401) {
      const onBee = window.location.pathname === '/bee' || window.location.pathname === '/bee.html';
      if (!onBee) {
        this.logout();
        window.location.href = '/bee';
      }
    }
    return { ok: res.ok, status: res.status, data };
  },

  get(path) { return this.request(path); },
  post(path, body) { return this.request(path, { method: 'POST', body: JSON.stringify(body) }); },
  put(path, body) { return this.request(path, { method: 'PUT', body: JSON.stringify(body) }); },
  del(path) { return this.request(path, { method: 'DELETE' }); },

  // ===== Auth =====
  async login(username, password) {
    const { data } = await this.post('/api/auth/login', { username, password });
    if (data.success) {
      localStorage.setItem(this.STORAGE_KEY, data.session_token);
      await this.fetchMe();
    }
    return data;
  },

  async register(username, password, displayName = '') {
    const { data } = await this.post('/api/auth/register', { username, password, display_name: displayName || username });
    if (data.success) {
      localStorage.setItem(this.STORAGE_KEY, data.session_token);
      await this.fetchMe();
    }
    return data;
  },

  async fetchMe() {
    const token = localStorage.getItem(this.STORAGE_KEY);
    if (!token) return null;
    const { data } = await this.get('/api/auth/me');
    if (data.logged_in) {
      localStorage.setItem('hivv_visitor_id', data.visitor_id);
      return data;
    }
    return null;
  },

  logout() {
    localStorage.removeItem(this.STORAGE_KEY);
    localStorage.removeItem('hivv_visitor_id');
  },

  async requireAuth() {
    const user = await this.fetchMe();
    if (!user?.logged_in) {
      const onBee = window.location.pathname === '/bee' || window.location.pathname === '/bee.html';
      if (!onBee) {
        window.location.href = '/bee';
      }
      return null;
    }
    return user;
  },

  // ===== Device Info =====
  getDeviceInfo() {
    return {
      browser: navigator.userAgent.split(' ').pop() || 'Unknown',
      device: `${navigator.hardwareConcurrency || '?'} cores, ${navigator.deviceMemory || '?'} GB RAM`,
      platform: navigator.platform,
      screen: `${screen.width}×${screen.height}`,
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      connection: navigator.connection?.effectiveType || 'Unknown',
      cores: navigator.hardwareConcurrency,
      memory: navigator.deviceMemory
    };
  },

  async fetchIPInfo() {
    try {
      const res = await fetch('/api/ip-info', { signal: AbortSignal.timeout(5000) });
      if (res.ok) return await res.json();
    } catch (e) {}
    return { ip: 'Unavailable', city: '', region: '', postal: '', country_name: '' };
  },

  // ===== Local AI Detection =====
  COMMON_PORTS: [
    { port: 1234, name: 'LM Studio', icon: '💻' },
    { port: 11434, name: 'Ollama', icon: '🦙' },
    { port: 8080, name: 'llama.cpp / LocalAI / SGLang', icon: '🔧' },
    { port: 8000, name: 'vLLM / Aphrodite', icon: '🚀' },
    { port: 5000, name: 'Text Gen / Oobabooga / KoboldCPP', icon: '📝' },
    { port: 8001, name: 'vLLM Alt', icon: '🚀' }
  ],

  async scanEndpoint(host, port, timeout = 2000) {
    const testUrl = `http://${host}:${port}/v1/models`;
    try {
      const res = await fetch(testUrl, { signal: AbortSignal.timeout(timeout) });
      if (res.ok) {
        const data = await res.json();
        const models = data.data?.map(m => m.id) ||
                       data.models?.map(m => m.name) ||
                       [];
        return { success: true, models, url: `http://${host}:${port}` };
      }
    } catch (e) {}
    return { success: false };
  },

  async scanHost(host) {
    const results = [];
    for (const { port, name } of this.COMMON_PORTS) {
      const result = await this.scanEndpoint(host, port);
      if (result.success) {
        results.push({ port, name, ...result });
      }
    }
    return results;
  },

  // ===== UI Helpers =====
  $: (sel) => document.querySelector(sel),
  $$: (sel) => document.querySelectorAll(sel),

  show(sel) { const el = this.$(sel); if (el) el.style.display = 'block'; },
  hide(sel) { const el = this.$(sel); if (el) el.style.display = 'none'; },
  toggle(sel, show) {
    const el = this.$(sel);
    if (el) el.style.display = show ? 'block' : 'none';
  },

  html(sel, html) {
    const el = this.$(sel);
    if (el) el.innerHTML = html;
  },

  text(sel, text) {
    const el = this.$(sel);
    if (el) el.textContent = text;
  },

  escapeHtml(str) {
    if (!str) return '';
    const d = document.createElement('div');
    d.textContent = str;
    return d.innerHTML;
  },

  formatDate(date) {
    if (!date) return 'Never';
    return new Date(date).toLocaleString();
  },

  formatNumber(num) {
    if (!num && num !== 0) return '0';
    return num.toLocaleString();
  },

  // ===== Tabs =====
  initTabs(containerId = '') {
    document.querySelectorAll('.tab').forEach(btn => {
      btn.addEventListener('click', () => {
        const tabId = btn.dataset.tab;
        if (!tabId) return;

        document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
        document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));

        btn.classList.add('active');
        const content = document.getElementById(`tab-${tabId}`);
        if (content) content.classList.add('active');
      });
    });
  },

  // ===== Toast Notifications =====
  toast(msg, duration = 3000) {
    const existing = document.querySelector('.hivv-toast');
    if (existing) existing.remove();

    const toast = document.createElement('div');
    toast.className = 'hivv-toast';
    toast.textContent = msg;
    toast.style.cssText = `
      position: fixed; bottom: 20px; right: 20px;
      padding: 16px 24px;
      background: #1a1a2e; border: 1px solid #00ff88;
      border-radius: 12px; color: #fff; font-weight: 500;
      z-index: 1000; animation: slideIn 0.3s ease;
    `;
    document.body.appendChild(toast);
    setTimeout(() => { toast.style.opacity = '0'; setTimeout(() => toast.remove(), 300); }, duration);
  },

  // ===== Markdown Renderer =====
  renderMarkdown(md) {
    if (!md) return '';
    let text = String(md)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');

    // Fenced code blocks with language and copy button
    text = text.replace(/```([a-zA-Z0-9_\-\+]*)\n([\s\S]*?)```/g, function(match, lang, code) {
      const langLabel = lang ? lang.toLowerCase() : 'code';
      return `\n<div class="code-block" data-lang="${langLabel}">
        <div class="code-header">
          <span class="code-lang">${langLabel}</span>
          <button class="code-copy-btn" onclick="HIVV.copyCode(this)" title="Copy code snippet">📋 Copy</button>
        </div>
        <pre><code>${code.trimEnd()}</code></pre>
      </div>\n`;
    });

    // Inline code
    text = text.replace(/`([^`\n]+)`/g, '<code class="inline-code">$1</code>');

    // Headers
    text = text.replace(/^#### (.*$)/gim, '<h4 class="md-h4">$1</h4>');
    text = text.replace(/^### (.*$)/gim, '<h3 class="md-h3">$1</h3>');
    text = text.replace(/^## (.*$)/gim, '<h2 class="md-h2">$1</h2>');
    text = text.replace(/^# (.*$)/gim, '<h1 class="md-h1">$1</h1>');

    // Blockquotes
    text = text.replace(/^\> (.*$)/gim, '<blockquote class="md-quote">$1</blockquote>');

    // Bold & Italic
    text = text.replace(/\*\*\*([^*]+)\*\*\*/g, '<strong><em>$1</em></strong>');
    text = text.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    text = text.replace(/\*([^*]+)\*/g, '<em>$1</em>');
    text = text.replace(/___([^_]+)___/g, '<strong><em>$1</em></strong>');
    text = text.replace(/__([^_]+)__/g, '<strong>$1</strong>');
    text = text.replace(/_([^_]+)_/g, '<em>$1</em>');

    // Links (safe https only)
    text = text.replace(/\[([^\]]+)\]\((https?:\/\/[^\s\)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer" class="md-link">$1</a>');

    // Lists
    text = text.replace(/^[\*\-] (.*$)/gim, '<li class="md-li">$1</li>');
    text = text.replace(/((?:<li class="md-li">.*?<\/li>\s*)+)/gis, '<ul class="md-ul">$1</ul>');

    text = text.replace(/^\d+\. (.*$)/gim, '<li class="md-oli">$1</li>');
    text = text.replace(/((?:<li class="md-oli">.*?<\/li>\s*)+)/gis, '<ol class="md-ol">$1</ol>');

    // Horizontal rule
    text = text.replace(/^---+$/gim, '<hr class="md-hr">');

    // Paragraphs
    const blocks = text.split(/\n\n+/);
    text = blocks.map(b => {
      b = b.trim();
      if (!b) return '';
      if (b.startsWith('<h') || b.startsWith('<div') || b.startsWith('<ul') || b.startsWith('<ol') || b.startsWith('<blockquote') || b.startsWith('<hr')) {
        return b;
      }
      return `<p class="md-p">${b.replace(/\n/g, '<br>')}</p>`;
    }).join('\n');

    return text;
  },

  copyCode(btn) {
    const codeBlock = btn.closest('.code-block');
    if (!codeBlock) return;
    const pre = codeBlock.querySelector('pre');
    if (!pre) return;
    const text = pre.innerText || pre.textContent;
    navigator.clipboard.writeText(text).then(() => {
      btn.textContent = '✓ Copied!';
      btn.classList.add('copied');
      setTimeout(() => {
        btn.textContent = '📋 Copy';
        btn.classList.remove('copied');
      }, 2000);
    }).catch(() => {
      btn.textContent = 'Error';
    });
  }
};

// ===== Provider Constants =====
HIVV.CLOUD_PROVIDERS = ['openai', 'anthropic', 'google', 'minimax', 'mimo', 'mistral', 'groq', 'openrouter', 'deepseek', 'together'];
HIVV.LOCAL_PROVIDERS = ['lmstudio', 'ollama', 'llamacpp', 'vllm', 'textgen', 'oobabooga', 'koboldcpp', 'localai', 'sglang', 'aphrodite'];

HIVV.DEFAULT_PORTS = {
  lmstudio: 1234, ollama: 11434, llamacpp: 8080, vllm: 8000,
  textgen: 5000, oobabooga: 5000, koboldcpp: 5000,
  localai: 8080, sglang: 8080, aphrodite: 8000
};

HIVV.PROVIDER_NAMES = {
  openai: 'OpenAI (ChatGPT)', anthropic: 'Anthropic (Claude)', google: 'Google (Gemini)',
  minimax: 'MiniMax', mimo: 'Mimo', mistral: 'Mistral AI', groq: 'Groq',
  openrouter: 'OpenRouter', deepseek: 'DeepSeek', together: 'Together AI',
  lmstudio: 'LM Studio', ollama: 'Ollama', llamacpp: 'llama.cpp',
  vllm: 'vLLM', textgen: 'Text Generation', oobabooga: 'Oobabooga',
  koboldcpp: 'KoboldCPP', localai: 'LocalAI', sglang: 'SGLang', aphrodite: 'Aphrodite',
  local: '🌐 Scan Local Network', custom: 'Custom Endpoint'
};

// Make available globally
window.HIVV = HIVV;
