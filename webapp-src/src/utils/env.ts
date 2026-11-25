/**
 * Detect if the app is running in a Tauri environment
 */
export const isTauri = () => {
    return typeof window !== 'undefined' &&
        (!!(window as any).__TAURI_INTERNALS__ || !!(window as any).__TAURI__);
};

/** 网页桥接也伪造了 __TAURI_INTERNALS__，桌面专属能力必须再排除桥接标记。 */
export const isDesktopApp = () => isTauri() && !(typeof window !== 'undefined' && !!(window as any).__AGY_WEB_BRIDGE__);

/**
 * Detect if running on Linux
 */
export const isLinux = () => {
    return navigator.userAgent.toLowerCase().includes('linux');
};
