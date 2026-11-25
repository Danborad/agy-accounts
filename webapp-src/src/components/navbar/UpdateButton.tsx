import { useEffect, useState } from 'react';
import { Download, Loader2 } from 'lucide-react';
import { request } from '../../utils/request';
import { publishUpdateStatus, subscribeUpdateStatus, type UpdateStatusInfo } from '../../utils/updateStatus';
import { showToast } from '../common/ToastContainer';

type UpdateInfo = UpdateStatusInfo;

export default function UpdateButton() {
    const [info, setInfo] = useState<UpdateInfo | null>(null);
    const [busy, setBusy] = useState(false);

    useEffect(() => {
        let alive = true;
        const show = (data: UpdateInfo) => {
            if (alive) setInfo(data);
        };
        const load = () => {
            request<UpdateInfo>('get_update')
                .then(show)
                .catch(() => {});
        };
        load();
        const timer = window.setInterval(load, 60 * 1000);
        const refresh = () => load();
        const refreshVisible = () => {
            if (document.visibilityState === 'visible') load();
        };
        const unsubscribe = subscribeUpdateStatus(show);
        window.addEventListener('focus', refresh);
        document.addEventListener('visibilitychange', refreshVisible);
        return () => {
            alive = false;
            window.clearInterval(timer);
            unsubscribe();
            window.removeEventListener('focus', refresh);
            document.removeEventListener('visibilitychange', refreshVisible);
        };
    }, []);

    if (!info?.available || !info.latest) return null;

    const apply = async () => {
        if (busy) return;
        const confirmed = window.confirm(
            `发现 Antigravity ${info.latest}（当前 ${info.running || '未知'}）。\n` +
            '更新会重启 agy-server，正在使用的页面会断开几秒后自动重连。\n\n现在更新并重启吗？'
        );
        if (!confirmed) return;
        setBusy(true);
        try {
            const result = await request<UpdateInfo & { message?: string }>('apply_update');
            showToast(`已更新到 ${result.running || info.latest}，Antigravity 会自动重连`, 'success');
            const updated = { ...info, running: result.running, latest: result.latest, available: false };
            setInfo(updated);
            publishUpdateStatus(updated);
            window.setTimeout(() => window.location.reload(), 1800);
        } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            showToast(`更新失败：${message}`, 'error');
        } finally {
            setBusy(false);
        }
    };

    return (
        <button
            type="button"
            disabled={busy}
            onClick={() => void apply()}
            title={`发现 Antigravity ${info.latest}，点击更新并重启`}
            className="inline-flex h-10 items-center gap-1.5 rounded-full bg-blue-50 px-3 text-sm font-medium text-blue-600 transition-colors hover:bg-blue-100 disabled:cursor-not-allowed disabled:opacity-70 dark:bg-blue-500/15 dark:text-blue-300 dark:hover:bg-blue-500/25"
        >
            {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Download className="h-4 w-4" />}
            <span className="hidden sm:inline">{busy ? '更新中…' : `更新 ${info.latest}`}</span>
        </button>
    );
}