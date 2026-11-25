export interface UpdateStatusInfo {
    running?: string;
    latest?: string;
    staged?: string;
    available?: boolean;
}

const UPDATE_STATUS_EVENT = 'agy:update-status';

/** 让导航栏与设置页共享同一次检查结果，避免一处已是最新、另一处仍显示旧徽标。 */
export function publishUpdateStatus(info: UpdateStatusInfo): void {
    window.dispatchEvent(new CustomEvent<UpdateStatusInfo>(UPDATE_STATUS_EVENT, { detail: info }));
}

export function subscribeUpdateStatus(handler: (info: UpdateStatusInfo) => void): () => void {
    const listener = (event: Event) => {
        handler((event as CustomEvent<UpdateStatusInfo>).detail);
    };
    window.addEventListener(UPDATE_STATUS_EVENT, listener);
    return () => window.removeEventListener(UPDATE_STATUS_EVENT, listener);
}
