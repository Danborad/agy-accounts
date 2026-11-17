import { useEffect, useState } from 'react';
import { Download, RefreshCw } from 'lucide-react';
import { request } from '../../utils/request';
import { showToast } from '../common/ToastContainer';

interface UpdateInfo {
    running?: string;
    latest?: string;
    staged?: string;
    available?: boolean;
}

export default function UpdateSettings() {
    const [info, setInfo] = useState<UpdateInfo | null>(null);
    const [busy, setBusy] = useState<'check' | 'apply' | ''>('');

    const load = () => {
        request<UpdateInfo>('get_update')
            .then(setInfo)
            .catch(() => {});
    };

    useEffect(() => {
        load();
    }, []);

    const check = async () => {
        if (busy) return;
        setBusy('check');
        try {
            const result = await request<UpdateInfo>('check_update');
            setInfo(result);
            showToast(result.available
                ? `发现 Antigravity ${result.latest}`
                : 'Antigravity 已是最新版本', 'success');
        } catch (error) {
            showToast(`检查更新失败：${error instanceof Error ? error.message : String(error)}`, 'error');
        } finally {
            setBusy('');
        }
    };

    const apply = async () => {
        if (busy) return;
        const confirmed = window.confirm(
            `更新到 Antigravity ${info?.latest || '最新版本'}？\n` +
            '更新会重启 agy-server，Antigravity 页面会断开几秒后自动重连。'
        );
        if (!confirmed) return;
        setBusy('apply');
        try {
            const result = await request<UpdateInfo & { message?: string }>('apply_update');
            setInfo(result);
            showToast(`${result.message || '已更新'}：${result.running || result.latest || ''}`, 'success');
        } catch (error) {
            showToast(`更新失败：${error instanceof Error ? error.message : String(error)}`, 'error');
        } finally {
            setBusy('');
        }
    };

    return (
        <section className="rounded-2xl border border-gray-200/80 bg-white p-5 sm:p-6 shadow-xs dark:border-slate-800 dark:bg-slate-900/80">
            <div className="mb-4 flex items-start gap-3">
                <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-sky-50 text-sky-600 dark:bg-sky-400/10 dark:text-sky-300">
                    <Download className="h-5 w-5" />
                </span>
                <div className="min-w-0 flex-1">
                    <h3 className="text-base font-semibold text-gray-900 dark:text-gray-100">Antigravity 更新</h3>
                    <p className="mt-0.5 text-xs leading-relaxed text-gray-500 dark:text-gray-400">
                        官方 language_server 由 agy-server 自动检查并下载。这里可以手动检查，或在发现新版本后立即更新并重启。
                        汉化是独立注入层，不需要等汉化更新。
                    </p>
                </div>
            </div>
            <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-gray-200/80 bg-gray-50/60 px-4 py-3 dark:border-slate-700 dark:bg-slate-800/40">
                <div className="min-w-0 text-sm">
                    <p className="text-gray-700 dark:text-gray-200">
                        当前版本：<span className="font-medium">{info?.running || '读取中…'}</span>
                        {info?.latest && info.latest !== info.running
                            ? <span className="ml-2 text-amber-600 dark:text-amber-400">最新 {info.latest}</span>
                            : null}
                    </p>
                    <p className="mt-0.5 text-xs text-gray-500 dark:text-gray-400">
                        {info?.available ? '发现可用更新，可立即应用。' : '暂未发现待应用的更新。'}
                    </p>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                    <button
                        type="button"
                        disabled={Boolean(busy)}
                        onClick={() => void check()}
                        className="inline-flex items-center gap-1.5 rounded-lg border border-gray-200 bg-white px-3 py-2 text-sm font-medium text-gray-600 transition-colors hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-60 dark:border-slate-700 dark:bg-slate-800 dark:text-gray-300 dark:hover:bg-slate-700"
                    >
                        <RefreshCw className={`h-4 w-4 ${busy === 'check' ? 'animate-spin' : ''}`} />
                        {busy === 'check' ? '检查中…' : '检查更新'}
                    </button>
                    {info?.available ? (
                        <button
                            type="button"
                            disabled={Boolean(busy)}
                            onClick={() => void apply()}
                            className="inline-flex items-center gap-1.5 rounded-lg bg-blue-500 px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-blue-600 disabled:cursor-not-allowed disabled:opacity-60"
                        >
                            <Download className={`h-4 w-4 ${busy === 'apply' ? 'animate-bounce' : ''}`} />
                            {busy === 'apply' ? '更新中…' : '更新并重启'}
                        </button>
                    ) : null}
                </div>
            </div>
        </section>
    );
}
