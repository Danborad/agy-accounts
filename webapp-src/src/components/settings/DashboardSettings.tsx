import { useState } from 'react';
import { DndContext, KeyboardSensor, PointerSensor, closestCenter, useSensor, useSensors } from '@dnd-kit/core';
import { SortableContext, arrayMove, rectSortingStrategy, sortableKeyboardCoordinates, useSortable } from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import { GripVertical, LayoutDashboard, RotateCcw, Save } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useConfigStore } from '../../stores/useConfigStore';
import { request } from '../../utils/request';
import { showToast } from '../common/ToastContainer';
import { dashboardCardOrder, dashboardCards, type DashboardCardId, type DashboardPreferences } from '../../types/config';

function CardRow({ id, order, selected, disabled, onToggle }: { id: DashboardCardId; order: number; selected: boolean; disabled: boolean; onToggle: () => void }) {
    const { t } = useTranslation();
    const label = t(`dashboard_settings.cards.${id}`);
    const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id, disabled });
    return <div ref={setNodeRef} data-card-option={id} style={{ transform: CSS.Transform.toString(transform), transition }} className={`flex min-w-0 items-center gap-2 rounded-lg border px-2.5 py-2 text-sm ${selected ? 'border-blue-100 bg-blue-50/40 dark:border-slate-600 dark:bg-slate-800/50' : 'border-slate-200 dark:border-slate-700'} ${isDragging ? 'relative z-10 shadow-md' : ''}`}>
        <button type="button" {...attributes} {...listeners} disabled={disabled} aria-label={t('dashboard_settings.reorder', { card: label })} className="shrink-0 touch-none rounded p-1 text-slate-500 hover:bg-slate-100 focus-visible:ring-2 focus-visible:ring-blue-500 disabled:opacity-20 dark:hover:bg-slate-700"><GripVertical size={16} /></button>
        <label className="flex min-w-0 flex-1 cursor-pointer items-center gap-2.5">
            <input type="checkbox" disabled={disabled} checked={selected} onChange={onToggle} className="h-4 w-4 shrink-0 accent-blue-600" />
            <span className="text-sm font-medium text-slate-700 dark:text-slate-200">{label}</span>
        </label>
        <span aria-hidden="true" className="w-5 shrink-0 text-right text-xs tabular-nums text-slate-400">{selected ? String(order + 1).padStart(2, '0') : '—'}</span>
    </div>;
}

export default function DashboardSettings() {
    const { t } = useTranslation();
    const config = useConfigStore(state => state.config);
    const [draft, setDraft] = useState<DashboardPreferences | null>(null);
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState('');
    const preferences = draft ?? config?.dashboard;
    const selected = dashboardCards(preferences?.cards);
    const ordered = dashboardCardOrder(preferences?.cards, preferences?.order);
    const dirty = draft !== null;

    const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 6 } }), useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }));

    const toggle = (id: DashboardCardId) => {
        if (saving || !config) return;
        const cards = ordered.filter(card => card === id ? !selected.includes(card) : selected.includes(card));
        setDraft({ cards, order: ordered });
        setError('');
    };

    const reorder = (activeId: DashboardCardId, overId: DashboardCardId) => {
        if (saving || !config) return;
        const from = ordered.indexOf(activeId);
        const to = ordered.indexOf(overId);
        if (from < 0 || to < 0 || from === to) return;
        const order = arrayMove(ordered, from, to);
        setDraft({ cards: selected.slice(), order });
        setError('');
    };

    const save = async () => {
        if (saving || !config || !dirty) return;
        if (selected.length === 0) {
            const message = '至少保留一张首页卡片';
            setError(message);
            showToast(message, 'error');
            return;
        }
        setSaving(true);
        setError('');
        try {
            const next = await request<DashboardPreferences>('set_dashboard_cards', {
                cards: selected,
                order: ordered,
                manual: true,
            });
            useConfigStore.setState(state => ({
                config: state.config ? { ...state.config, dashboard: next } : null,
            }));
            setDraft(null);
            showToast('首页卡片设置已保存', 'success');
        } catch (saveError) {
            const message = saveError instanceof Error ? saveError.message : String(saveError);
            setError(message);
            showToast(`保存失败：${message}`, 'error');
        } finally {
            setSaving(false);
        }
    };

    return <section aria-labelledby="dashboard-settings-title" className="rounded-2xl border border-gray-200/80 bg-white p-5 sm:p-6 shadow-xs dark:border-slate-800 dark:bg-slate-900/80">
        <div className="mb-4 flex items-start gap-3">
            <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-blue-50 text-blue-600 dark:bg-blue-400/10 dark:text-blue-300"><LayoutDashboard className="h-5 w-5" /></span>
            <div className="min-w-0 flex-1">
                <h3 id="dashboard-settings-title" className="text-base font-semibold text-gray-900 dark:text-gray-100">{t('dashboard_settings.title')}</h3>
                <p className="mt-1 text-sm leading-6 text-gray-500 dark:text-gray-400">勾选或拖动调整首页卡片，修改后点「保存设置」生效；不保存不会影响当前配置。</p>
            </div>
        </div>
        <p className="mb-3 text-sm text-gray-500 dark:text-gray-400" role="status">
            {saving ? t('dashboard_settings.saving') : t('dashboard_settings.selected_count', { count: selected.length })}
            {dirty && !saving ? <span className="ml-2 font-medium text-amber-600 dark:text-amber-400">有未保存修改</span> : null}
        </p>
        <DndContext sensors={sensors} collisionDetection={closestCenter} accessibility={{ screenReaderInstructions: { draggable: t('dashboard_settings.drag_instructions') }, announcements: {
            onDragStart: ({ active }) => t('dashboard_settings.drag_start', { card: active.id }),
            onDragOver: ({ over }) => over ? t('dashboard_settings.drag_over', { card: over.id }) : '',
            onDragEnd: () => t('dashboard_settings.drag_end'),
            onDragCancel: () => t('dashboard_settings.drag_cancel'),
        } }} onDragEnd={({ active, over }) => {
            if (!over || active.id === over.id) return;
            reorder(active.id as DashboardCardId, over.id as DashboardCardId);
        }}>
            <SortableContext items={ordered} strategy={rectSortingStrategy}>
                <div className="grid gap-2 sm:grid-cols-2">{ordered.map(id => <CardRow key={id} id={id} order={selected.indexOf(id)} selected={selected.includes(id)} disabled={saving} onToggle={() => toggle(id)} />)}</div>
            </SortableContext>
        </DndContext>
        <div className="mt-4 flex flex-wrap items-center gap-2">
            <button
                type="button"
                onClick={() => { void save(); }}
                disabled={!dirty || saving || !config}
                className="inline-flex items-center gap-1.5 rounded-lg bg-blue-500 px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-blue-600 disabled:cursor-not-allowed disabled:opacity-50"
            >
                <Save className="h-4 w-4" />
                {saving ? '保存中…' : '保存设置'}
            </button>
            <button
                type="button"
                onClick={() => { setDraft(null); setError(''); }}
                disabled={!dirty || saving}
                className="inline-flex items-center gap-1.5 rounded-lg border border-gray-200 bg-white px-3 py-2 text-sm font-medium text-gray-600 transition-colors hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-slate-700 dark:bg-slate-800 dark:text-gray-300 dark:hover:bg-slate-700"
            >
                <RotateCcw className="h-4 w-4" />
                撤销修改
            </button>
        </div>
        {error && <p role="alert" className="mt-3 text-xs text-red-600">{error}</p>}
    </section>;
}
