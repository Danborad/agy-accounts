import { BrainCircuit, Clock, Sparkles } from "lucide-react";
import type { Account, QuotaData } from "../../types/account";
import { cn } from "../../utils/cn";

type Window = "5h" | "weekly";
type Family = "claude" | "gemini" | "argon" | "carbon" | "hydrogen";

interface Row {
    key: string;
    family: Family;
    window: Window;
    remaining: number | null;
    resetTime: string;
    label?: string;
}

function familyOf(value: string): Family | null {
    const text = (value || "").toLowerCase();
    if (text.includes("argon")) return "argon";
    if (text.includes("carbon")) return "carbon";
    if (text.includes("hydrogen")) return "hydrogen";
    if (text.includes("claude") || text.includes("gpt") || text.startsWith("3p")) return "claude";
    if (text.includes("gemini")) return "gemini";
    return null;
}

function windowOf(value: string): Window | null {
    const text = (value || "").trim().toLowerCase();
    if (text === "5h" || text.endsWith("-5h")) return "5h";
    if (text === "weekly" || text.endsWith("-weekly")) return "weekly";
    return null;
}

function percentage(value: unknown, known?: boolean): number | null {
    if (known === false || typeof value !== "number" || !Number.isFinite(value)) return null;
    const scaled = value * 100;
    return scaled >= 0 && scaled <= 100 ? scaled : null;
}

function buildRows(quota?: QuotaData): Row[] {
    const rows = new Map<string, Row>();
    for (const group of quota?.quota_groups || []) {
        const family = familyOf(group.display_name);
        if (!family) continue;
        for (const bucket of group.buckets || []) {
            const window = windowOf(bucket.window) || windowOf(bucket.bucket_id);
            if (!window) continue;
            let key = `${family}-${window}`;
            let suffix = 2;
            while (rows.has(key)) {
                key = `${family}-${window}-${suffix}`;
                suffix += 1;
            }
            const value = percentage(bucket.remaining_fraction, bucket.remaining_fraction_known);
            const isPrimary = family === "claude" || family === "gemini";
            rows.set(key, {
                key,
                family,
                window,
                remaining: value,
                resetTime: bucket.reset_time || "",
                label: isPrimary ? undefined : (group.display_name || family),
            });
        }
    }

    // Gemini 5h has no grouped summary on some plans; derive it from the model list.
    for (const family of ["claude", "gemini"] as const) {
        if (rows.has(`${family}-5h`)) continue;
        const models = (quota?.models || []).filter((model) => {
            const name = (model.name || "").toLowerCase();
            return family === "claude" ? name.includes("claude") : name.startsWith("gemini");
        });
        const known = models
            .map((model) => ({
                value: model.observed_remaining_fraction !== undefined
                    ? percentage(model.observed_remaining_fraction, model.percentage_known)
                    : percentage((model.percentage ?? 0) / 100, model.percentage_known),
                reset: model.reset_time || "",
            }))
            .filter((item) => item.value !== null);
        if (known.length) {
            const lowest = known.reduce((a, b) => (a.value! <= b.value! ? a : b));
            rows.set(`${family}-5h`, {
                key: `${family}-5h`,
                family,
                window: "5h",
                remaining: lowest.value,
                resetTime: lowest.reset || "",
            });
        }
    }

    const mainOrder: Array<[Family, Window]> = [
        ["claude", "5h"],
        ["gemini", "5h"],
        ["claude", "weekly"],
        ["gemini", "weekly"],
    ];
    const mainKeys = new Set(mainOrder.map(([family, window]) => `${family}-${window}`));
    const main = mainOrder.map(([family, window]) => rows.get(`${family}-${window}`) || {
        key: `${family}-${window}`,
        family,
        window,
        remaining: null,
        resetTime: "",
    });
    const extras = Array.from(rows.values()).filter((row) => !mainKeys.has(row.key));
    return [...main, ...extras];
}

function resetLabel(iso: string): string {
    if (!iso) return "";
    const delta = Date.parse(iso) - Date.now();
    if (!Number.isFinite(delta)) return "";
    if (delta <= 0) return "即将重置";
    const minutes = Math.round(delta / 60000);
    if (minutes < 60) return `${minutes} 分钟后`;
    const hours = Math.round(minutes / 60);
    if (hours < 48) return `${hours} 小时后`;
    return `${Math.round(hours / 24)} 天后`;
}

function tone(value: number | null): string {
    if (value === null) return "text-gray-400 dark:text-gray-500";
    if (value < 20) return "text-rose-600 dark:text-rose-400";
    if (value < 60) return "text-amber-600 dark:text-amber-400";
    return "text-emerald-600 dark:text-emerald-400";
}

function barTone(value: number | null): string {
    if (value === null) return "bg-gray-300 dark:bg-white/10";
    if (value < 20) return "bg-rose-500";
    if (value < 60) return "bg-amber-500";
    return "bg-emerald-500";
}

export function GroupQuota({ account, columns = 2 }: { account: Account; columns?: 1 | 2 }) {
    const quota = account.quota;
    const blocked = account.disabled || quota?.is_forbidden || account.validation_blocked;
    if (blocked) {
        return (
            <div className="w-full py-2 text-center text-[11px] font-semibold text-gray-400 dark:text-gray-500">
                {account.validation_blocked ? "需要验证" : account.disabled ? "账号已禁用" : "账号已受限"}
            </div>
        );
    }

    const rows = buildRows(quota);
    return (
        <div className={cn("grid grid-cols-1 gap-1.5 w-full", columns === 2 && "md:grid-cols-2")}>
            {rows.map((row) => {
                const Icon = row.family === "claude" ? BrainCircuit : Sparkles;
                const label = row.label
                    ? `${row.label} · ${row.window === "5h" ? "5 小时" : "周额度"}`
                    : `${row.family === "claude" ? "Claude" : "Gemini"} ${row.window === "5h" ? "5h" : "周额度"}`;
                const rounded = row.remaining === null ? null : Math.round(row.remaining);
                const reset = resetLabel(row.resetTime);
                return (
                    <div
                        key={row.key}
                        className={cn(
                            "rounded-lg border border-gray-200/70 bg-gray-50/80 px-2.5 py-1 dark:border-white/[0.08] dark:bg-white/[0.04]",
                            row.label && columns === 2 && "md:col-span-2"
                        )}
                        title={row.resetTime
                            ? `${label} · ${new Date(row.resetTime).toLocaleString()}`
                            : `${label} · Antigravity API 未返回该窗口的数据`}
                    >
                        <div className="flex items-center justify-between gap-2 text-[11px] font-semibold leading-tight">
                            <span className="flex min-w-0 items-center gap-1.5 truncate text-gray-800 dark:text-gray-100">
                                <Icon size={12} className="shrink-0 text-gray-500 dark:text-gray-400" />
                                <span className="truncate">{label}</span>
                            </span>
                            <span className={`shrink-0 font-mono ${tone(row.remaining)}`}>
                                {rounded === null ? "—" : `剩余 ${rounded}%`}
                            </span>
                        </div>
                        <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-gray-200 dark:bg-white/10">
                            <div
                                className={`h-full rounded-full transition-all duration-500 ${barTone(row.remaining)}`}
                                style={{ width: `${rounded ?? 0}%` }}
                            />
                        </div>
                        <div className="mt-0.5 flex items-center justify-between gap-2 text-[10px] leading-none text-gray-400 dark:text-gray-500">
                            <span>{rounded === null ? "接口未提供" : ""}</span>
                            {reset ? (
                                <span className="flex items-center gap-1 font-mono">
                                    <Clock size={10} />
                                    {reset}
                                </span>
                            ) : null}
                        </div>
                    </div>
                );
            })}
        </div>
    );
}

export default GroupQuota;
