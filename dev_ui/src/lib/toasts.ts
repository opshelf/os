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
	key: string;
	message: string;
	actions?: ToastAction[];
};

export type ToastReport = {
	key?: string;
	actions?: ToastAction[];
};

export type ToastSource = {
	report: (message: string, report?: ToastReport) => void;
	release: () => void;
};

const STATUS_TIMEOUT_MS = 10000;

// The stack is position:fixed, so a faulting subsystem must not bury the page. When
// the queue is full the oldest entry leaves first.
const MAX_VISIBLE = 5;

// Conditions and one-off events are keyed in separate namespaces, so an event worded
// like an ongoing condition does not take over its queue slot.
const CONDITION_PREFIX = 'condition:';
const EVENT_PREFIX = 'event:';

// A released condition stays remembered, acknowledgement included, so a source that
// remounts and reports the same wording does not look like a new fault.
const MAX_REMEMBERED = 64;

// A dismissal silences a live condition, it does not resolve it: a fault that is still
// being reported comes back after this long, so tapping it away between two jobs does
// not lose it for the rest of the shift.
const ACK_TIMEOUT_MS = 60000;

export const toasts = writable<Toast[]>([]);

// What a source last said its state is, kept after the source goes away.
type Condition = {
	kind: ToastKind;
	message: string;
	actions?: ToastAction[];
	sources: Set<number>;
	toastId: number | null;
	acknowledged: boolean;
	reshowTimer: ReturnType<typeof setTimeout> | null;
};

const conditions = new Map<string, Condition>();
const activeKeyOf = new Map<number, string>();
const activeTimers = new Map<number, ReturnType<typeof setTimeout>>();

let nextToastId = 1;
let nextSourceId = 1;

function clearReshow(condition: Condition): void {
	if (condition.reshowTimer !== null) {
		clearTimeout(condition.reshowTimer);
		condition.reshowTimer = null;
	}
}

// Take a condition out of view until it changes or resolves. Only a condition some
// source still reports comes back on its own; a stale one stays put until then.
function acknowledgeCondition(key: string, condition: Condition): void {
	condition.acknowledged = true;
	condition.toastId = null;
	clearReshow(condition);

	condition.reshowTimer = setTimeout(() => {
		condition.reshowTimer = null;

		if (condition.sources.size === 0 || condition.acknowledged === false) {
			return;
		}

		condition.acknowledged = false;
		condition.toastId = upsertToast(condition.kind, key, condition.message, condition.actions);
	}, ACK_TIMEOUT_MS);
}

function clearExpiry(id: number): void {
	const timer = activeTimers.get(id);

	if (timer !== undefined) {
		clearTimeout(timer);
		activeTimers.delete(id);
	}
}

function scheduleExpiry(id: number): void {
	clearExpiry(id);
	activeTimers.set(
		id,
		setTimeout(() => {
			activeTimers.delete(id);
			dismissToast(id);
		}, STATUS_TIMEOUT_MS)
	);
}

function upsertToast(
	kind: ToastKind,
	key: string,
	message: string,
	actions?: ToastAction[]
): number {
	const existing = get(toasts).find((toast) => toast.key === key);

	if (existing !== undefined) {
		toasts.update((current) =>
			current.map((toast) => (toast.id === existing.id ? { ...toast, message, actions } : toast))
		);

		if (kind === 'status') {
			scheduleExpiry(existing.id);
		}

		return existing.id;
	}

	const id = nextToastId++;

	toasts.update((current) => [...current, { id, kind, key, message, actions }]);

	if (kind === 'status') {
		scheduleExpiry(id);
	}

	trimToCap();

	return id;
}

function dropToast(key: string): void {
	toasts.update((current) =>
		current.filter((toast) => {
			if (toast.key !== key) {
				return true;
			}

			clearExpiry(toast.id);
			return false;
		})
	);
}

function trimToCap(): void {
	toasts.update((current) => {
		let next = current;

		while (next.length > MAX_VISIBLE) {
			const [oldest, ...rest] = next as [Toast, ...Toast[]];

			clearExpiry(oldest.id);

			// Pushing a live condition out is a dismissal by the queue, not a heal.
			const condition = conditions.get(oldest.key);

			if (condition !== undefined && condition.toastId === oldest.id) {
				acknowledgeCondition(oldest.key, condition);
			}

			next = rest;
		}

		return next;
	});
}

function rememberWithinBudget(): void {
	for (const [key, condition] of conditions) {
		if (conditions.size <= MAX_REMEMBERED) {
			return;
		}

		if (condition.sources.size === 0) {
			conditions.delete(key);
		}
	}
}

// The source no longer reports this condition, but the queue keeps showing what it
// last said: a fault does not become false because another page was opened.
function releaseOwnership(key: string, sourceId: number): void {
	if (activeKeyOf.get(sourceId) === key) {
		activeKeyOf.delete(sourceId);
	}

	conditions.get(key)?.sources.delete(sourceId);
}

// The source said the condition is over. It leaves the queue, and so does its
// acknowledgement, so a fault that comes back is shown again.
function endCondition(key: string, sourceId: number): void {
	releaseOwnership(key, sourceId);

	const condition = conditions.get(key);

	if (condition === undefined || condition.sources.size > 0) {
		return;
	}

	clearReshow(condition);
	conditions.delete(key);
	dropToast(key);
}

function reportCondition(
	sourceId: number,
	kind: ToastKind,
	message: string,
	reportKey: string,
	actions?: ToastAction[]
): void {
	const key = reportKey === '' ? '' : `${CONDITION_PREFIX}${reportKey}`;
	const previousKey = activeKeyOf.get(sourceId);

	if (previousKey !== undefined && previousKey !== key) {
		// Reporting a different state ends the previous one for this source.
		endCondition(previousKey, sourceId);
	}

	if (key === '') {
		return;
	}

	activeKeyOf.set(sourceId, key);

	let condition = conditions.get(key);

	if (condition === undefined) {
		condition = {
			kind,
			message,
			actions,
			sources: new Set<number>(),
			toastId: null,
			acknowledged: false,
			reshowTimer: null
		};
		conditions.set(key, condition);
	}

	condition.sources.add(sourceId);
	rememberWithinBudget();

	if (condition.message !== message) {
		condition.message = message;
		condition.acknowledged = false;
		clearReshow(condition);
	}

	condition.actions = actions;

	// Acknowledged and unchanged: keep it out of the way even though sources keep
	// reporting it after a navigation, a remount, or another status snapshot.
	if (condition.acknowledged === true) {
		return;
	}

	condition.toastId = upsertToast(kind, key, message, actions);
}

// Anything can post here. Toasts dismiss on tap; status toasts also expire on their
// own, while errors stay until dismissed. Repeating a visible status toast restarts
// its timer instead of stacking a copy.
export function postToast(kind: ToastKind, message: string, actions?: ToastAction[]): number {
	return upsertToast(kind, `${EVENT_PREFIX}${kind}:${message}`, message, actions);
}

// A source reports the state it is currently in, and reports an empty message once
// that state is over. Re-reporting unchanged state does not raise a new toast, so a
// page that remounts while a fault is ongoing cannot make it look new again.
export function createToastSource(kind: ToastKind): ToastSource {
	const sourceId = nextSourceId++;

	return {
		report: (message: string, report?: ToastReport) => {
			const text = message.trim();

			reportCondition(sourceId, kind, text, (report?.key ?? text).trim(), report?.actions);
		},
		release: () => {
			const key = activeKeyOf.get(sourceId);

			if (key !== undefined) {
				releaseOwnership(key, sourceId);
			}
		}
	};
}

export function dismissToast(id: number): void {
	const toast = get(toasts).find((candidate) => candidate.id === id);

	clearExpiry(id);
	toasts.update((current) => current.filter((candidate) => candidate.id !== id));

	if (toast === undefined) {
		return;
	}

	const condition = conditions.get(toast.key);

	if (condition !== undefined && condition.toastId === toast.id) {
		acknowledgeCondition(toast.key, condition);
	}
}
