import { useEffect, useState } from 'react';
import { RefreshCw, ShieldCheck } from 'lucide-react';
import { request } from '../../utils/request';
import { showToast } from '../common/ToastContainer';

interface FailoverStatus {
    state: string;
    message: string;
    at: number;
    from?: string;
    to?: string;
}

interface FailoverPayload {
    enabled: boolean;
    status?: FailoverStatus;
}

function formatTime(stamp?: number) {
    if (!stamp) return '';
    return new Date(stamp * 1000).toLocaleTimeString('zh-CN', { hour12: false });
}

export default function FailoverSettings() {
    const [enabled, setEnabled] = useState(true);
    const [status, setStatus] = useState<FailoverStatus | null>(null);
    const [loading, setLoading] = useState(true);
    const [busy, setBusy] = useState(false);

    useEffect(() => {
        request<FailoverPayload>('get_failover')
            .then(payload => {
                setEnabled(Boolean(payload?.enabled));
                setStatus(payload?.status || null);
            })
            .catch(() => {})
            .finally(() => setLoading(false));
    }, []);

    const toggle = async () => {
        if (busy || loading) return;
        const next = !enabled;
        setBusy(true);
        try {
            const payload = await request<FailoverPayload>('set_failover', { enabled: next });
            setEnabled(Boolean(payload?.enabled));
            setStatus(payload?.status || null);
            showToast(next ? '已开启账号失效自动切换' : '已关闭账号失效自动切换', 'success');
        } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            showToast(`保存失败：${message}`, 'error');
        } finally {
            setBusy(false);
        }
    };

    const statusText = status?.message || '每 90 秒检查一次当前账号';
    const lastChecked = formatTime(status?.at);

    return (
        <section className="rounded-2xl border border-gray-200/80 bg-white p-5 sm:p-6 shadow-xs dark:border-slate-800 dark:bg-slate-900/80">
            <div className="mb-4 flex items-start gap-3">
                <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-emerald-50 text-emerald-600 dark:bg-emerald-400/10 dark:text-emerald-300">
                    <ShieldCheck className="h-5 w-5" />
                </span>
                <div className="min-w-0 flex-1">
                    <h3 className="text-base font-semibold text-gray-900 dark:text-gray-100">账号失效自动切换</h3>
                    <p className="mt-0.5 text-xs leading-relaxed text-gray-500 dark:text-gray-400">
                        当 Google 明确判定当前账号不可用或需要验证时，自动尝试其它已保存账号；验证失败会回滚。
                        如果只是连续无法读取登录状态，则仅尝试重启服务，不换账号。
                    </p>
                </div>
            </div>
            <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-gray-200/80 bg-gray-50/60 px-4 py-3 dark:border-slate-700 dark:bg-slate-800/40">
                <div className="min-w-0">
                    <p className="text-sm font-medium text-gray-800 dark:text-gray-100">
                        {loading ? '读取中…' : enabled ? '已开启' : '已关闭'}
                    </p>
                    <p className="mt-0.5 flex flex-wrap items-center gap-1.5 text-xs text-gray-500 dark:text-gray-400">
                        <span>{statusText}</span>
                        {lastChecked ? <span className="text-gray-400">· {lastChecked}</span> : null}
                    </p>
                </div>
                <button
                    type="button"
                    role="switch"
                    aria-checked={enabled}
                    aria-label="账号失效自动切换"
                    disabled={busy || loading}
                    onClick={() => void toggle()}
                    className={`relative inline-flex h-7 w-12 shrink-0 items-center rounded-full transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${enabled ? 'bg-emerald-500' : 'bg-slate-300 dark:bg-slate-600'}`}
                >
                    <span className={`inline-block h-5 w-5 transform rounded-full bg-white shadow transition-transform ${enabled ? 'translate-x-6' : 'translate-x-1'}`} />
                </button>
            </div>
            {busy ? (
                <p className="mt-3 inline-flex items-center gap-1.5 text-xs text-gray-500 dark:text-gray-400">
                    <RefreshCw className="h-3.5 w-3.5 animate-spin" /> 保存中…
                </p>
            ) : null}
        </section>
    );
}
