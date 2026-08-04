// ---------------------------------------------------------------------------
// NeuroRouter frontend — shared client. Loaded by every page.
// Talks to the FastAPI backend that serves this very frontend (same-origin
// by default), so no CORS config is needed out of the box. The base URL is
// overridable from Settings for the (uncommon) case of running the frontend
// separately from the API.
// ---------------------------------------------------------------------------

const NR = {
  baseUrl() {
    return localStorage.getItem('nr_base_url') || '';
  },
  apiKey() {
    return localStorage.getItem('nr_api_key') || 'dev-key-123';
  },
  adminToken() {
    return localStorage.getItem('nr_admin_token') || 'admin-dev-token';
  },
  setBaseUrl(v) { localStorage.setItem('nr_base_url', v || ''); },
  setApiKey(v) { localStorage.setItem('nr_api_key', v || ''); },
  setAdminToken(v) { localStorage.setItem('nr_admin_token', v || ''); },

  async _fetch(path, opts = {}) {
    const headers = Object.assign(
      { 'Content-Type': 'application/json' },
      opts.adminAuth ? { 'X-Admin-Token': this.adminToken() } : {},
      opts.apiAuth !== false ? { 'X-API-Key': this.apiKey() } : {},
      opts.headers || {}
    );
    let resp;
    try {
      resp = await fetch(this.baseUrl() + path, { ...opts, headers });
    } catch (e) {
      throw new Error(`Could not reach NeuroRouter API at "${this.baseUrl() || window.location.origin}" — is the backend running?`);
    }
    if (!resp.ok) {
      let detail = resp.statusText;
      try { detail = (await resp.json()).detail || detail; } catch (_) {}
      const err = new Error(detail);
      err.status = resp.status;
      throw err;
    }
    if (resp.status === 204) return null;
    return resp.json();
  },

  // ---- public API ----
  chatCompletion(body) {
    return this._fetch('/v1/chat/completions', { method: 'POST', body: JSON.stringify(body) });
  },
  getStats() { return this._fetch('/v1/stats', { apiAuth: false }); },
  getModels() { return this._fetch('/v1/models', { apiAuth: false }); },
  getHealth() { return this._fetch('/health', { apiAuth: false }); },
  getLogs(params = {}) {
    const q = new URLSearchParams(params).toString();
    return this._fetch(`/v1/logs${q ? '?' + q : ''}`, { apiAuth: false });
  },
  getTrace(id) { return this._fetch(`/v1/logs/${encodeURIComponent(id)}`, { apiAuth: false }); },

  // ---- admin API ----
  listKeys() { return this._fetch('/v1/admin/keys', { adminAuth: true, apiAuth: false }); },
  createKey(name) {
    return this._fetch('/v1/admin/keys', { method: 'POST', adminAuth: true, apiAuth: false, body: JSON.stringify({ name }) });
  },
  revokeKey(key) {
    return this._fetch(`/v1/admin/keys/${encodeURIComponent(key)}`, { method: 'DELETE', adminAuth: true, apiAuth: false });
  },
  getConfig() { return this._fetch('/v1/admin/config', { adminAuth: true, apiAuth: false }); },
  updateConfig(values) {
    return this._fetch('/v1/admin/config', { method: 'POST', adminAuth: true, apiAuth: false, body: JSON.stringify(values) });
  },
  resetConfig() { return this._fetch('/v1/admin/config/reset', { method: 'POST', adminAuth: true, apiAuth: false }); },
  disableProvider(name) {
    return this._fetch(`/v1/admin/providers/${encodeURIComponent(name)}/disable`, { method: 'POST', adminAuth: true, apiAuth: false });
  },
  enableProvider(name) {
    return this._fetch(`/v1/admin/providers/${encodeURIComponent(name)}/enable`, { method: 'POST', adminAuth: true, apiAuth: false });
  },
  resetCircuit(name) {
    return this._fetch(`/v1/admin/providers/${encodeURIComponent(name)}/reset-circuit`, { method: 'POST', adminAuth: true, apiAuth: false });
  },
};

// ---------------------------------------------------------------------------
// Toasts
// ---------------------------------------------------------------------------
function nrToast(message, type = 'info') {
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.innerHTML = `<span class="material-symbols-outlined text-sm">${type === 'error' ? 'error' : type === 'success' ? 'check_circle' : 'info'}</span><span>${message}</span>`;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 4200);
}

function nrHandleError(e, fallback = 'Something went wrong') {
  console.error(e);
  nrToast(e.message || fallback, 'error');
}

// ---------------------------------------------------------------------------
// Sidebar nav — same markup on every page, active link highlighted
// ---------------------------------------------------------------------------
const NR_NAV_ITEMS = [
  { id: 'playground', href: 'index.html', icon: 'terminal', label: 'Playground' },
  { id: 'dashboard', href: 'dashboard.html', icon: 'dashboard', label: 'Dashboard' },
  { id: 'providers', href: 'providers.html', icon: 'hub', label: 'Providers & Routing' },
  { id: 'monitor', href: 'monitor.html', icon: 'monitoring', label: 'Live Monitor' },
  { id: 'logs', href: 'logs.html', icon: 'receipt_long', label: 'System Logs' },
  { id: 'keys', href: 'keys.html', icon: 'key', label: 'API Keys' },
  { id: 'settings', href: 'settings.html', icon: 'tune', label: 'Settings' },
];

function nrRenderNav(activeId) {
  const nav = document.getElementById('nr-nav');
  if (!nav) return;
  nav.innerHTML = NR_NAV_ITEMS.map(item => `
    <a class="nav-link ${item.id === activeId ? 'active' : ''}" href="${item.href}">
      <span class="material-symbols-outlined">${item.icon}</span>
      <span>${item.label}</span>
    </a>
  `).join('');
}

// Connection status pill in the topbar, present on every page
async function nrRenderConnectionStatus() {
  const el = document.getElementById('nr-conn-status');
  if (!el) return;
  try {
    const health = await NR.getHealth();
    el.innerHTML = `<span class="pulse-dot bg-secondary" style="background:#4edea3"></span> <span style="color:#4edea3">Connected</span>`;
  } catch (e) {
    el.innerHTML = `<span class="w-2 h-2 rounded-full" style="background:#ffb4ab;display:inline-block"></span> <span style="color:#ffb4ab">Offline</span>`;
  }
}

document.addEventListener('DOMContentLoaded', () => {
  nrRenderConnectionStatus();
  setInterval(nrRenderConnectionStatus, 10000);
});
