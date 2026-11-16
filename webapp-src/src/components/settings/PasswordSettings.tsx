import { useState } from 'react';
import { KeyRound } from 'lucide-react';
import { request } from '../../utils/request';
import { showToast } from '../common/ToastContainer';

export default function PasswordSettings() {
    const [current, setCurrent] = useState('');
    const [next, setNext] = useState('');
    const [confirm, setConfirm] = useState('');
    const [busy, setBusy] = useState(false);

    const submit = async (event: React.FormEvent) => {
        event.preventDefault();
        if (busy) return;
        if (next.length < 8) {
            showToast('新密码至少 8 个字符', 'error');
            return;
        }
        if (next !== confirm) {
            showToast('两次输入的新密码不一致', 'error');
            return;
        }
        setBusy(true);
        try {
            await request('change_password', { currentPassword: current, newPassword: next });
            setCurrent('');
            setNext('');
            setConfirm('');
            showToast('面板密码已更新', 'success');
        } catch (error) {
            showToast(String(error), 'error');
        } finally {
            setBusy(false);
        }
    };

    return (
        <section className="rounded-2xl border border-gray-200/80 bg-white p-5 sm:p-6 shadow-xs dark:border-slate-800 dark:bg-slate-900/80">
            <div className="mb-4 flex items-start gap-3">
                <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-violet-50 text-violet-600 dark:bg-violet-400/10 dark:text-violet-300">
                    <KeyRound className="h-5 w-5" />
                </span>
                <div>
                    <h3 className="text-base font-semibold text-gray-900 dark:text-gray-100">面板密码</h3>
                    <p className="mt-0.5 text-xs leading-relaxed text-gray-500 dark:text-gray-400">
                        修改登录面板使用的共享密码。修改后其它已登录设备不会被强制退出。
                    </p>
                </div>
            </div>
            <form onSubmit={submit} className="grid gap-3 sm:grid-cols-3">
                <label className="flex flex-col gap-1 text-xs font-medium text-gray-600 dark:text-gray-300">
                    当前密码
                    <input
                        type="password"
                        value={current}
                        autoComplete="current-password"
                        onChange={event => setCurrent(event.target.value)}
                        className="rounded-lg border border-gray-200 bg-white px-3 py-2 text-sm text-gray-800 outline-none focus:border-blue-400 dark:border-slate-700 dark:bg-slate-800 dark:text-gray-100"
                        required
                    />
                </label>
                <label className="flex flex-col gap-1 text-xs font-medium text-gray-600 dark:text-gray-300">
                    新密码
                    <input
                        type="password"
                        value={next}
                        autoComplete="new-password"
                        onChange={event => setNext(event.target.value)}
                        className="rounded-lg border border-gray-200 bg-white px-3 py-2 text-sm text-gray-800 outline-none focus:border-blue-400 dark:border-slate-700 dark:bg-slate-800 dark:text-gray-100"
                        required
                    />
                </label>
                <label className="flex flex-col gap-1 text-xs font-medium text-gray-600 dark:text-gray-300">
                    确认新密码
                    <input
                        type="password"
                        value={confirm}
                        autoComplete="new-password"
                        onChange={event => setConfirm(event.target.value)}
                        className="rounded-lg border border-gray-200 bg-white px-3 py-2 text-sm text-gray-800 outline-none focus:border-blue-400 dark:border-slate-700 dark:bg-slate-800 dark:text-gray-100"
                        required
                    />
                </label>
                <div className="sm:col-span-3">
                    <button
                        type="submit"
                        disabled={busy}
                        className="rounded-lg bg-blue-500 px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-blue-600 disabled:cursor-not-allowed disabled:opacity-60"
                    >
                        {busy ? '保存中…' : '更新密码'}
                    </button>
                </div>
            </form>
        </section>
    );
}
