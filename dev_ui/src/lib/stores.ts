import { writable } from 'svelte/store';

export type WorkerStatusPayload = {
	robot_status?: Record<string, unknown> | null;
	running?: boolean;
	current_command?: Record<string, unknown> | null;
	queue_length?: number;
	error_queue?: Array<Record<string, unknown>>;
	queued_operations?: Array<{ command?: Record<string, unknown>; timestamp?: string }>;
	needs_home?: boolean;
	needs_home_reason?: string | null;
};

export const isApiConnected = writable(true);
export const robotStatus = writable<WorkerStatusPayload | null>(null);
export const robotStatusError = writable('');
export const robotStatusLastUpdate = writable('');
