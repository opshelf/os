import { get, writable } from 'svelte/store';

export type ToastKind = 'error' | 'status';

export type Toast = {
	id: number;
	kind: ToastKind;
	message: string;
};

const TOAST_TIMEOUT_MS = 10000;

export const toasts = writable<Toast[]>([]);

let nextId = 1;

const activeTimers = new Map<number, ReturnType<typeof setTimeout>>();

function scheduleExpiry(id: number): void {
	const existing = activeTimers.get(id);

	if (existing !== undefined) {
		clearTimeout(existing);
	}

	activeTimers.set(id, setTimeout(() => dismissToast(id), TOAST_TIMEOUT_MS));
}

// Anything can post here. Toasts dismiss on tap; status toasts also expire on
// their own, while errors stay until dismissed. Repeating a visible status
// toast restarts its timer instead of stacking a copy.
export function postToast(kind: ToastKind, message: string): void {
	const visible = get(toasts);
	const existing = visible.find((toast) => toast.kind === kind && toast.message === message);

	if (existing !== undefined) {
		if (kind === 'status') {
			scheduleExpiry(existing.id);
		}

		return;
	}

	const id = nextId++;

	toasts.update((current) => [...current, { id, kind, message }]);

	if (kind === 'status') {
		scheduleExpiry(id);
	}
}

export function dismissToast(id: number): void {
	const timer = activeTimers.get(id);

	if (timer !== undefined) {
		clearTimeout(timer);
		activeTimers.delete(id);
	}

	toasts.update((current) => current.filter((toast) => toast.id !== id));
}
