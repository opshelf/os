import { get, writable } from 'svelte/store';

export type ToastKind = 'error' | 'status';

export type ToastAction = {
	label: string;
	onClick: () => void | Promise<void>;
	disabled?: boolean;
};

export type Toast = {
	id: number;
	kind: ToastKind;
	message: string;
	actions?: ToastAction[];
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

	activeTimers.set(
		id,
		setTimeout(() => dismissToast(id), TOAST_TIMEOUT_MS)
	);
}

// Anything can post here. Toasts dismiss on tap; status toasts also expire on
// their own, while errors stay until dismissed. Repeating a visible status
// toast restarts its timer instead of stacking a copy.
export function postToast(kind: ToastKind, message: string, actions?: ToastAction[]): number {
	const visible = get(toasts);
	const existing = visible.find((toast) => toast.kind === kind && toast.message === message);

	if (existing !== undefined) {
		toasts.update((current) =>
			current.map((toast) => (toast.id === existing.id ? { ...toast, actions } : toast))
		);

		if (kind === 'status') {
			scheduleExpiry(existing.id);
		}

		return existing.id;
	}

	const id = nextId++;

	toasts.update((current) => [...current, { id, kind, message, actions }]);

	if (kind === 'status') {
		scheduleExpiry(id);
	}

	return id;
}

// Action state can change while a toast is visible. Updating by id is a no-op
// after expiry, so a later status snapshot cannot recreate a timed-out toast.
export function updateToastActions(id: number, actions: ToastAction[]): void {
	toasts.update((current) =>
		current.map((toast) => (toast.id === id ? { ...toast, actions } : toast))
	);
}

export function dismissToast(id: number): void {
	const timer = activeTimers.get(id);

	if (timer !== undefined) {
		clearTimeout(timer);
		activeTimers.delete(id);
	}

	toasts.update((current) => current.filter((toast) => toast.id !== id));
}
