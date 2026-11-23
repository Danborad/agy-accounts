/* Same-origin transport for Powertools on agy-server; no CDP port is exposed. */
(() => {
  if (window !== window.top || window.__agyPowertoolsWeb) return;
  const endpoint = '/__agy-powertools/';
  window.__agyPowertoolsWeb = { transport: 'same-origin' };
  window.__agyPowertoolsWeb.rpc = async (method, payload = {}) => {
    const response = await fetch(`${endpoint}rpc`, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-Agy-Powertools': '1' },
      body: JSON.stringify({ ...payload, id: 1, method }), signal: AbortSignal.timeout(18000),
    });
    const data = await response.json();
    if (!response.ok || data.error) throw Error(data.error || '插件服务暂时不可用');
    return data;
  };
  window.__agyPowertoolsHost = async payload => {
    let request;
    try {
      request = JSON.parse(payload);
      const response = await fetch(`${endpoint}rpc`, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-Agy-Powertools': '1' },
        body: payload,
        signal: AbortSignal.timeout(18000),
      });
      const reply = await response.json().catch(() => ({ error: '插件服务暂时不可用，请刷新页面重试。' }));
      if (response.status === 401) reply.error = '网页登录已过期，请重新登录后刷新。';
      window.__agyPowertoolsReceive?.(request.id, reply);
    } catch {
      if (request) window.__agyPowertoolsReceive?.(request.id, { error: '插件连接失败，请稍后刷新页面。' });
    }
  };

  // Desktop-only controls must not suggest that server credentials or proxies can be changed.
  const restricted = ['multiAccount', 'autoSwitchAccount', 'proxyEnabled', 'fileContextMenu'];
  const style = document.createElement('style');
  style.textContent = restricted.map(key => `[data-testid="agy-setting-row-${key}"] { opacity: .55; }`).join('\n');
  document.head.append(style);
  const accountStyle = document.createElement('style');
  accountStyle.textContent = `
    .agy-server-identity{min-width:0}
    .agy-server-email{display:block;font-size:15px;line-height:1.4;overflow-wrap:anywhere}
    .agy-server-meta{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:5px}
    .agy-server-alias,.agy-server-login,.agy-server-tier{font-size:11px;line-height:1.5;padding:1px 7px;border:1px solid var(--border,#333);border-radius:999px;color:var(--muted-foreground,#9ca3af)}
    .agy-server-alias{color:var(--foreground,#eee);background:var(--secondary,#222)}
    .agy-server-login.is-missing{color:var(--error,#ef4444);border-color:var(--error,#ef4444)}
    .agy-server-current{font-size:11px;padding:2px 8px;border-radius:999px;background:var(--primary,#2563eb);color:#fff;white-space:nowrap}
    .agy-server-quota{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px;margin-top:14px}
    .agy-server-quota-row{display:flex;flex-wrap:wrap;align-items:center;gap:6px;font-size:12px;padding:3px 0}
    .agy-server-quota-label{width:48px;flex:none;color:var(--muted-foreground,#9ca3af)}
    .agy-server-bar{height:6px;border-radius:3px;background:var(--border,#333);overflow:hidden;flex:1 1 100px;min-width:80px}
    .agy-server-fill{height:100%;border-radius:3px;transition:width .2s}
    .agy-server-fill.high{background:#22c55e}
    .agy-server-fill.mid{background:#f59e0b}
    .agy-server-fill.low{background:#ef4444}
    .agy-server-percent{width:42px;text-align:right;font-variant-numeric:tabular-nums}
    .agy-server-reset{font-size:11px;margin-left:auto}
    [data-server-account]{padding:16px}
    [data-testid="agy-page-tabs"]{overflow-x:auto;max-width:100%;scrollbar-width:none;white-space:nowrap}
    [data-testid="agy-page-tabs"]>button{flex-shrink:0}
    .agy-server-button{cursor:pointer;padding:7px 12px;border:1px solid var(--border);border-radius:8px}
    .agy-server-button:disabled{opacity:.5;cursor:default}
    .agy-server-confirm{position:fixed;inset:0;background:#0009;z-index:10000;display:flex;align-items:center;justify-content:center;padding:16px}
    .agy-server-confirm>.pt-card{max-width:480px;background:var(--card);padding:20px}
  `;
  document.head.append(accountStyle);
  const updateControls = () => {
    for (const key of restricted) {
      const control = document.querySelector(`[data-testid="agy-setting-${key}"]`);
      if (control && !control.disabled) {
        control.disabled = true;
        control.title = '此桌面功能不适用于服务器；切号请使用内置的账号与额度页。';
      }
    }
    const accountRow = document.querySelector('[data-testid="agy-setting-row-multiAccount"]');
    if (accountRow) accountRow.style.display = 'none';
    const fileRow = document.querySelector('[data-testid="agy-setting-row-fileContextMenu"]');
    if (fileRow) fileRow.style.display = 'none';
    for (const title of ['账号与切换', '网络代理']) {
      const heading = [...document.querySelectorAll('h3')].find(node => node.textContent.trim() === title);
      const section = heading?.closest('.space-y-2');
      if (section) section.style.display = 'none';
    }
    const page = document.querySelector('[data-testid="agy-page-tabs"]');
    if (page && !document.querySelector('[data-agy-web-notice]')) {
      const note = document.createElement('p');
      note.dataset.agyWebNotice = '';
      note.textContent = '账号额度与切换已内置在「账号与额度」和设置页；费用仅为 API 参考估算，不是订阅账单。';
      note.style.cssText = 'font-size:12px;line-height:1.6;opacity:.65;margin:12px 0';
      page.parentElement.after(note);
    }
  };
  const observer = new MutationObserver(updateControls);
  observer.observe(document.documentElement, { childList: true, subtree: true });

  let loaded = false;
  const load = () => {
    if (loaded || !window.ReactDOM?.createRoot || !window.createFileRoute) return;
    loaded = true;
    const script = document.createElement('script');
    script.src = `${endpoint}ui.js`;
    script.onerror = () => console.warn('[Powertools] 插件加载失败，原 Antigravity 页面不受影响。');
    script.onload = () => {
      let attempts = 0;
      const verify = setInterval(() => {
        const healthy = window.__agyPowertools?.status?.healthy;
        const error = window.__agyCompatibilityError;
        if (healthy || error || ++attempts >= 60) {
          clearInterval(verify);
          if (!healthy) console.warn('[Powertools]', error || '插件未完成挂载，原页面仍可使用。');
        }
      }, 500);
    };
    const accounts = document.createElement('script');
    accounts.src = `${endpoint}accounts.js`;
    accounts.onload = () => document.head.append(script);
    accounts.onerror = script.onerror;
    document.head.append(accounts);
    clearInterval(timer);
  };
  const timer = setInterval(load, 250);
  load();
  setTimeout(() => clearInterval(timer), 30000);
})();
