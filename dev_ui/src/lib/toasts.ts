import { writable } from 'svelte/store';

export type ToastKind = 'error' | 'status';

export type Toast = {
	id: number;
	kind: ToastKind;
	message: string;
};

const TOAST_TIMEOUT_MS = 10000;

export const toasts = writable<Toast[]>([]);

let nextId = 1;

// Anything can post here. Toasts dismiss on tap or after the timeout.
export function postToast(kind: ToastKind, message: string): void {
	const id = nextId++;

	toasts.update((current) => [...current, { id, kind, message }]);
	setTimeout(() => dismissToast(id), TOAST_TIMEOUT_MS);
}

export function dismissToast(id: number): void {
	toasts.update((current) => current.filter((toast) => toast.id !== id));
}
