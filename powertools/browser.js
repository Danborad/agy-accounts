/* Same-origin transport for Powertools on agy-server; no CDP port is exposed. */
(() => {
  if (window !== window.top || window.__agyPowertoolsWeb) return;
  const endpoint = '/__agy-powertools/';
  window.__agyPowertoolsWeb = { transport: 'same-origin' };
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
  const updateControls = () => {
    for (const key of restricted) {
      const control = document.querySelector(`[data-testid="agy-setting-${key}"]`);
      if (control && !control.disabled) {
        control.disabled = true;
        control.title = '网页版暂不支持；账号切换请继续使用现有账号面板。';
      }
    }
    const page = document.querySelector('[data-testid="agy-page-tabs"]');
    if (page && !document.querySelector('[data-agy-web-notice]')) {
      const note = document.createElement('p');
      note.dataset.agyWebNotice = '';
      note.textContent = '网页版测试：界面与统计已接入。切号仍使用原账号面板；费用仅为 API 参考估算，不是订阅账单。';
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
    document.head.append(script);
    clearInterval(timer);
  };
  const timer = setInterval(load, 250);
  load();
  setTimeout(() => clearInterval(timer), 30000);
})();
