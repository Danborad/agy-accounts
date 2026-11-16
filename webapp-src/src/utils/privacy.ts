import { useEffect, useState } from "react";

const STORAGE_KEY = "agy-accounts-privacy-mode";
const listeners = new Set<() => void>();

let enabled = false;
try {
    enabled = localStorage.getItem(STORAGE_KEY) === "true";
} catch {
    enabled = false;
}

export function maskEmail(email?: string | null): string {
    if (!email) return "";
    const at = email.indexOf("@");
    return at > 0 ? "***" + email.slice(at) : "***";
}

export function maskText(value?: string | null): string {
    return value ? "***" : "";
}

export function privacyEnabled(): boolean {
    return enabled;
}

export function setPrivacyMode(next: boolean): void {
    enabled = next;
    try {
        localStorage.setItem(STORAGE_KEY, String(next));
    } catch {
        // ignore storage failures
    }
    listeners.forEach((listener) => listener());
}

export function togglePrivacyMode(): void {
    setPrivacyMode(!enabled);
}

export function usePrivacyMode(): boolean {
    const [value, setValue] = useState(enabled);
    useEffect(() => {
        const listener = () => setValue(enabled);
        listeners.add(listener);
        listener();
        return () => {
            listeners.delete(listener);
        };
    }, []);
    return value;
}
