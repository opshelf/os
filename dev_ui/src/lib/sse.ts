import { buildAuthenticatedUrl } from './api';
import { ensureValidToken, isAuthenticated, loginAsDeveloper } from './auth';

type EventHandler = (event: MessageEvent<string>) => void;

type ReconnectingEventSourceOptions = {
	path: string | (() => string);
	reconnectDelayMs?: number;
	onOpen?: () => void;
	onMessage?: EventHandler;
	onError?: () => void;
	events?: Record<string, EventListener>;
};

export type ReconnectingEventSource = {
	connect: () => void;
	close: () => void;
	getSource: () => EventSource | null;
};

export function createReconnectingEventSource(
	options: ReconnectingEventSourceOptions
): ReconnectingEventSource {
	let source: EventSource | null = null;
	let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
	let shouldReconnect = true;
	const reconnectDelayMs = options.reconnectDelayMs ?? 2000;

	function clearReconnectTimer(): void {
		if (reconnectTimer === null) {
			return;
		}

		clearTimeout(reconnectTimer);
		reconnectTimer = null;
	}

	function resolveUrl(): string {
		if (typeof options.path === 'function') {
			return options.path();
		}

		return buildAuthenticatedUrl(options.path);
	}

	function closeSource(): void {
		if (source === null) {
			return;
		}

		if (options.events !== undefined) {
			for (const [eventName, handler] of Object.entries(options.events)) {
				source.removeEventListener(eventName, handler);
			}
		}

		source.close();
		source = null;
	}

	function shouldStopReconnecting(): boolean {
		return shouldReconnect === false || source !== null;
	}

	async function reconnect(): Promise<void> {
		if (shouldStopReconnecting() === true) {
			return;
		}

		try {
			if (isAuthenticated() === true) {
				await ensureValidToken();
			} else {
				await loginAsDeveloper();
			}
		} catch {
			try {
				await loginAsDeveloper();
			} catch {
				return;
			}
		}

		if (shouldStopReconnecting() === true) {
			return;
		}

		const nextSource = new EventSource(resolveUrl());
		source = nextSource;

		if (options.events !== undefined) {
			for (const [eventName, handler] of Object.entries(options.events)) {
				nextSource.addEventListener(eventName, handler);
			}
		}

		nextSource.onopen = () => {
			options.onOpen?.();
		};

		if (options.onMessage !== undefined) {
			nextSource.onmessage = options.onMessage;
		}

		nextSource.onerror = () => {
			closeSource();
			options.onError?.();
			clearReconnectTimer();
			reconnectTimer = setTimeout(() => {
				reconnectTimer = null;
				void reconnect();
			}, reconnectDelayMs);
		};
	}

	return {
		connect: () => {
			shouldReconnect = true;
			void reconnect();
		},
		close: () => {
			shouldReconnect = false;
			clearReconnectTimer();
			closeSource();
		},
		getSource: () => source
	};
}
