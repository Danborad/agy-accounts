/* Minimal account UI: credentials and switching remain in agy-accounts. */
(() => {
  const key = 'agy-web-account-switch-job';
  window.__agyWebAccountsPanel = function AccountsPanel() {
    const R = window.React;
    const h = R.createElement;
    const [data, setData] = R.useState(null);
    const [error, setError] = R.useState('');
    const [target, setTarget] = R.useState(null);
    const [busy, setBusy] = R.useState(false);
    const [message, setMessage] = R.useState('');
    const [jobId, setJobId] = R.useState(() => sessionStorage.getItem(key) || '');
    const pending = R.useRef(false);
    const lastJob = R.useRef(jobId);
    R.useEffect(() => {
      let live = true;
      const load = async () => {
        if (pending.current) return;
        pending.current = true;
        try {
          const next = await window.__agyPowertoolsWeb.rpc('serverAccounts.list');
          if (!live) return;
          setData(next);
          setError('');
          if (lastJob.current && next.switchJob?.id === lastJob.current) {
            setMessage(next.switchJob.message);
            if (!next.switchJob.active) {
              sessionStorage.removeItem(key);
              setJobId('');
              if (next.switchJob.state !== 'success') setError(next.switchJob.message);
            }
          } else if (jobId && !next.switchJob?.active) {
            setMessage('服务已重新启动，切换结果无法确认，请核对当前账号后再操作。');
            sessionStorage.removeItem(key);
            setJobId('');
          }
        } catch (failure) {
          if (live) setError(jobId ? '正在等待服务恢复连接，切换状态会自动更新，请勿重复切号。' : failure.message);
        } finally { pending.current = false; }
      };
      void load();
      const timer = setInterval(load, jobId || data?.refreshing ? 2000 : 10000);
      return () => { live = false; clearInterval(timer); };
    }, [jobId, Boolean(data?.refreshing)]);

    const requestSwitch = async () => {
      if (busy || jobId || !target) return;
      setBusy(true);
      setError('');
      try {
        const result = await window.__agyPowertoolsWeb.rpc('serverAccounts.switch', {
          accountId: target.id, expectedCurrentId: data.currentId, confirmed: true,
        });
        sessionStorage.setItem(key, result.jobId);
        lastJob.current = result.jobId;
        setJobId(result.jobId);
        setTarget(null);
        setMessage('正在切换并验证登录，页面可能短暂断开，请稍候。');
      } catch (failure) { setError(failure.message); }
      finally { setBusy(false); }
    };
    const refresh = async () => {
      setBusy(true);
      try { setData(await window.__agyPowertoolsWeb.rpc('serverAccounts.refresh')); setError(''); }
      catch (failure) { setError(failure.message); }
      finally { setBusy(false); }
    };
    const switching = busy || Boolean(jobId) || data?.switchJob?.active || data?.switching?.active;
    const button = (text, onClick, disabled, props = {}) => h('button', {
      type: 'button', className: 'agy-field agy-server-button', onClick, disabled, ...props,
    }, text);
    const identity = account => h('div', { className: 'agy-server-identity' },
      h('strong', { className: 'agy-server-email' }, account.email || account.name),
      h('div', { className: 'agy-server-meta' },
        account.name && account.name !== account.email
          && h('span', { className: 'agy-server-alias', title: '账号备注' }, account.name),
        h('span', { className: `agy-server-login${account.has_token ? '' : ' is-missing'}` },
          account.has_token ? '已保存登录' : '缺少登录'),
        account.quota?.tier && h('span', { className: 'agy-server-tier' }, account.quota.tier)));
    const quotaRow = (label, bucket, rowKey) => {
      if (!bucket) return null;
      const percent = Math.max(0, Math.min(100, Math.round(bucket.remaining * 100)));
      const tone = percent < 20 ? 'low' : percent < 50 ? 'mid' : 'high';
      const reset = bucket.reset ? new Date(bucket.reset) : null;
      return h('div', { key: rowKey, className: 'agy-server-quota-row' },
        h('span', { className: 'agy-server-quota-label' }, label),
        h('div', { className: 'agy-server-bar', role: 'progressbar', 'aria-valuemin': 0,
          'aria-valuemax': 100, 'aria-valuenow': percent },
          h('div', { className: `agy-server-fill ${tone}`, style: { width: `${percent}%` } })),
        h('strong', { className: 'agy-server-percent' }, bucket.disabled ? '不限额' : `${percent}%`),
        reset && Number.isFinite(reset.getTime()) && h('span', { className: 'pt-muted agy-server-reset' },
          `${reset.toLocaleString(undefined, { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })} 重置`));
    };
    const quota = account => {
      const q = account.quota;
      if (!q?.groups?.length) return h('p', { className: 'pt-muted' },
        q?.status === 'error' ? `额度查询失败：${q.error || '请刷新重试'}` : '正在读取账号额度…');
      return h('div', { className: 'agy-server-quota' }, ...q.groups.map((group, index) => h('div', { key: `${group.key}-${index}` },
        h('div', { className: 'pt-muted' }, group.label),
        quotaRow('5 小时', group.windows['5h'], '5h'),
        quotaRow('周额度', group.windows.weekly, 'weekly'))));
    };
    return h('section', { className: 'pt-stack', 'data-testid': 'agy-server-accounts' },
      h('div', { className: 'pt-row' }, h('h3', null, '账号额度与切换'),
        button(data?.refreshing ? '刷新中…' : '刷新额度', refresh, switching || data?.refreshing,
          { 'data-testid': 'agy-server-accounts-refresh' })),
      h('p', { className: 'pt-muted' }, '使用已有账号快照；切换后验证登录，失败时回滚。无需另开账号面板。'),
      error && h('p', { role: 'alert', className: 'pt-alert' }, error),
      message && h('p', { role: 'status' }, message),
      !data && h('p', null, '正在读取账号…'),
      data?.accounts?.length === 0 && h('p', null, '暂无已保存账号。请先通过原面板保存账号快照。'),
      ...((data?.accounts || []).map(account => h('article', {
        key: account.id, className: 'pt-card', 'data-server-account': account.id,
      }, h('div', { className: 'pt-row', style: { alignItems: 'flex-start' } },
        identity(account),
        account.is_current ? h('span', { className: 'agy-server-current', 'data-testid': 'agy-server-current' }, '当前账号')
          : button('切换', () => { setTarget(account); setError(''); }, switching || !account.has_token,
            { 'data-testid': 'agy-server-switch' })), quota(account),
        account.quota?.fetched_at && h('p', { className: 'pt-muted', style: { fontSize: 11, marginTop: 10 } },
          `${data.refreshing ? '正在更新；缓存时间' : '更新时间'}：${new Date(account.quota.fetched_at * 1000).toLocaleString()}`),
        account.quota?.error && h('p', { className: 'pt-muted' }, `当前为缓存额度：${account.quota.error}`)))),
      target && h('div', { role: 'dialog', 'aria-modal': 'true', 'aria-label': '确认切换账号', className: 'agy-server-confirm' },
        h('div', { className: 'pt-card' }, h('h3', null, `切换到「${target.email || target.name}」？`),
          target.name && target.name !== target.email && h('p', { className: 'pt-muted' }, `备注：${target.name}`),
          h('p', null, '切换会重启 Antigravity，正在生成的回复可能中断。请确认当前任务已结束；失败时会尝试恢复原凭据。'),
          h('div', { className: 'pt-row' }, button('取消', () => setTarget(null), busy),
            button(busy ? '提交中…' : '确认切换', requestSwitch, busy || Boolean(jobId),
              { 'data-testid': 'agy-server-switch-confirm' })))));
  };
})();
