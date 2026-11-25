/* Check the Antigravity core, separately from Powertools plugin releases. */
(() => {
  window.__agyWebCoreUpdate = function CoreUpdate() {
    const R = window.React;
    const h = R.createElement;
    const [info, setInfo] = R.useState(null);
    const [error, setError] = R.useState('');
    const [submitting, setSubmitting] = R.useState(false);
    const [confirming, setConfirming] = R.useState(false);
    const dialog = R.useRef(null);
    const pending = R.useRef(false);
    const checking = info?.check?.state === 'checking';
    const updating = ['pending', 'downloading', 'restarting', 'verifying'].includes(info?.update?.state);
    R.useLayoutEffect(() => {
      if (!confirming) return;
      const element = dialog.current;
      element.showModal();
      return () => element.close();
    }, [confirming]);
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
      const timer = setInterval(load, checking || updating ? 2000 : 30000);
      return () => { live = false; clearInterval(timer); };
    }, [checking, updating]);
    const check = async () => {
      if (submitting || checking || updating) return;
      setSubmitting(true);
      setError('');
      try { setInfo(await window.__agyPowertoolsWeb.rpc('coreUpdate.check')); }
      catch (failure) { setError(failure.message); }
      finally { setSubmitting(false); }
    };
    const apply = async () => {
      if (submitting || updating) return;
      setSubmitting(true);
      setError('');
      try {
        setInfo(await window.__agyPowertoolsWeb.rpc('coreUpdate.apply', { version: info.latest, confirmed: true }));
        setConfirming(false);
      } catch (failure) { setError(failure.message); }
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
      h('div', { className: 'pt-row' }, h('h3', null, 'Antigravity 核心更新'), h('div', { className: 'agy-server-actions', style: { flexWrap: 'wrap' } },
        h('button', { type: 'button', className: 'agy-field agy-server-button', onClick: check,
          disabled: submitting || checking || updating, 'data-testid': 'agy-core-update-check' },
        checking ? '检查中…' : '检查核心更新'),
        info?.available && info?.canApply && h('button', { type: 'button', className: 'agy-field agy-server-button',
          onClick: () => setConfirming(true), disabled: submitting || checking || updating, 'data-testid': 'agy-core-update-apply' },
          updating ? '更新中…' : '更新并重启'))),
      h('p', { className: 'pt-muted' }, '核心更新由独立限权服务执行，与插件更新分开。确认后下载并重启核心，只有验证目标版本运行后才报告成功。'),
      h('div', { className: 'pt-row' },
        h('span', { 'data-testid': 'agy-core-update-running' }, `当前版本：${info ? info.running || '无法确认' : '读取中…'}`),
        h('span', { 'data-testid': 'agy-core-update-latest' }, `官方最新：${info?.check?.latest || '未检查'}`)),
      h('p', { role: info?.check?.state === 'failed' ? 'alert' : 'status', 'data-testid': 'agy-core-update-status' }, status),
      info?.update?.message && h('p', { role: info.update.state === 'failed' ? 'alert' : 'status', 'data-testid': 'agy-core-update-progress' }, info.update.message),
      info?.check?.checked_at > 0 && h('p', { className: 'pt-muted', style: { fontSize: 11 } },
        `上次成功检查：${new Date(info.check.checked_at * 1000).toLocaleString()}`),
      info?.available && !info?.canApply && h('p', { className: 'pt-muted' }, '核心更新服务未安装，请管理员完成部署。'),
      error && h('p', { role: 'alert' }, error),
      confirming && h('dialog', { ref: dialog, className: 'agy-server-confirm', 'aria-label': '确认核心更新',
        onCancel: event => { event.preventDefault(); if (!submitting) setConfirming(false); } },
        h('div', { className: 'pt-card' }, h('h3', null, `更新到 Antigravity ${info.latest}？`),
          h('p', null, '将下载官方核心并重启 Antigravity。正在生成的回复可能中断，请等任务结束后再更新。账号凭据不会切换。'),
          h('div', { className: 'agy-server-modal-actions' },
            h('button', { type: 'button', className: 'agy-field agy-server-button', disabled: submitting,
              onClick: () => setConfirming(false) }, '取消'),
            h('button', { type: 'button', className: 'agy-field agy-server-button', disabled: submitting,
              onClick: apply, 'data-testid': 'agy-core-update-confirm' }, submitting ? '提交中…' : '确认更新并重启')))));
  };
})();
