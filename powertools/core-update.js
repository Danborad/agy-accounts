/* Check the Antigravity core, separately from Powertools plugin releases. */
(() => {
  window.__agyWebCoreUpdate = function CoreUpdate() {
    const R = window.React;
    const h = R.createElement;
    const [info, setInfo] = R.useState(null);
    const [error, setError] = R.useState('');
    const [submitting, setSubmitting] = R.useState(false);
    const pending = R.useRef(false);
    const checking = info?.check?.state === 'checking';
    R.useEffect(() => {
      let live = true;
      const load = async () => {
        if (pending.current) return;
        pending.current = true;
        try {
          const next = await window.__agyPowertoolsWeb.rpc('coreUpdate.status');
          if (live) { setInfo(next); setError(''); }
        } catch (failure) { if (live) setError(failure.message); }
        finally { pending.current = false; }
      };
      void load();
      const timer = setInterval(load, checking ? 2000 : 30000);
      return () => { live = false; clearInterval(timer); };
    }, [checking]);
    const check = async () => {
      if (submitting || checking) return;
      setSubmitting(true);
      setError('');
      try { setInfo(await window.__agyPowertoolsWeb.rpc('coreUpdate.check')); }
      catch (failure) { setError(failure.message); }
      finally { setSubmitting(false); }
    };
    let status = '尚未检查官方最新版本。';
    if (checking) status = '正在查询官方版本，请稍候…';
    else if (info?.check?.state === 'failed') status = info.check.error;
    else if (info?.check?.state === 'success') {
      if (!info.running) status = '官方版本已查询，但无法确认当前运行版本。';
      else status = info.available ? `发现可用更新：${info.latest}` : 'Antigravity 核心已是官方最新版本。';
    } else if (info?.staged && info.available) status = `已下载待应用版本：${info.staged}`;
    return h('section', { className: 'pt-card pt-stack', style: { padding: 16 }, 'data-testid': 'agy-core-update' },
      h('div', { className: 'pt-row' }, h('h3', null, 'Antigravity 核心更新'),
        h('button', { type: 'button', className: 'agy-field agy-server-button', onClick: check,
          disabled: submitting || checking, 'data-testid': 'agy-core-update-check' },
        submitting || checking ? '检查中…' : '检查核心更新')),
      h('p', { className: 'pt-muted' }, '查询官方 language_server 版本，与下方 Powertools 插件更新分开。仅检查，不下载或重启服务。'),
      h('div', { className: 'pt-row' },
        h('span', { 'data-testid': 'agy-core-update-running' }, `当前版本：${info ? info.running || '无法确认' : '读取中…'}`),
        h('span', { 'data-testid': 'agy-core-update-latest' }, `官方最新：${info?.check?.latest || '未检查'}`)),
      h('p', { role: info?.check?.state === 'failed' ? 'alert' : 'status', 'data-testid': 'agy-core-update-status' }, status),
      info?.check?.checked_at > 0 && h('p', { className: 'pt-muted', style: { fontSize: 11 } },
        `上次成功检查：${new Date(info.check.checked_at * 1000).toLocaleString()}`),
      info?.available && h('p', { className: 'pt-muted' }, '可由 agy-server 自动下载；应用更新会重启核心，请在空闲时由主机管理。'),
      error && h('p', { role: 'alert' }, error));
  };
})();
