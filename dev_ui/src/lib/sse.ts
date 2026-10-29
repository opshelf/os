import { buildAuthenticatedUrl } from './api';
import { developerPin, ensureValidToken, isAuthenticated, loginAsDeveloper, logout } from './auth';

// A hidden page whose timers stalled this long was suspended (iOS), not just throttled.
const SUSPENDED_TIMER_GAP_MS = 5000;
const HIDDEN_TICK_MS = 1000;

type EventHandler = (event: MessageEvent<string>) => void;

type ReconnectingEventSourceOptions = {
	path: string | (() => string);
	reconnectDelayMs?: number;
	refreshAuthenticationOnError?: boolean | (() => boolean);
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
	let shouldRefreshAuthentication = false;
	let isProbingLoginRedirect = false;
	let didTriggerReload = false;
	let hiddenTimer: ReturnType<typeof setInterval> | null = null;
	let hiddenLastTickAt = 0;
	let hiddenMaxTickGapMs = 0;
	let isConnecting = false;
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

	function scheduleReconnect(): void {
		if (shouldReconnect === false) {
			return;
		}

		clearReconnectTimer();
		reconnectTimer = setTimeout(() => {
			reconnectTimer = null;
			void reconnect();
		}, reconnectDelayMs);
	}

	async function reloadIfLoginRedirect(): Promise<void> {
		if (isProbingLoginRedirect === true || didTriggerReload === true) {
			return;
		}

		isProbingLoginRedirect = true;

		try {
			let probeUrl: string;
			try {
				probeUrl = resolveUrl();
			} catch {
				return;
			}

			let response: Response;
			try {
				response = await fetch(probeUrl, {
					headers: { Accept: 'text/event-stream' },
					redirect: 'manual'
				});
			} catch {
				return;
			}

			const isRedirectStatus =
				response.type === 'opaqueredirect' ||
				response.status === 0 ||
				(response.status >= 300 && response.status < 400);

			const contentType = response.headers.get('content-type') ?? '';
			const isEventStream = contentType.includes('text/event-stream') === true;

			if (isEventStream === true) {
				await response.body?.cancel();
				return;
			}

			if (isRedirectStatus === false) {
				await response.body?.cancel();
				return;
			}

			await response.body?.cancel();
			didTriggerReload = true;
			location.reload();
		} finally {
			isProbingLoginRedirect = false;
		}
	}

	function shouldRefreshAuthenticationAfterError(): boolean {
		if (typeof options.refreshAuthenticationOnError === 'function') {
			return options.refreshAuthenticationOnError();
		}

		return options.refreshAuthenticationOnError ?? false;
	}

	async function authenticateForConnection(): Promise<void> {
		// Without the relay's cookie (the device kiosk, direct LAN access) the API serves
		// the stream without a token, and signing in would throw before it connects.
		if (developerPin() === null) {
			shouldRefreshAuthentication = false;
			return;
		}

		try {
			if (shouldRefreshAuthentication === true) {
				logout();
				await loginAsDeveloper();
				shouldRefreshAuthentication = false;
				return;
			}

			if (isAuthenticated() === true) {
				await ensureValidToken();
				return;
			}

			await loginAsDeveloper();
		} catch {
			await loginAsDeveloper();
			shouldRefreshAuthentication = false;
		}
	}

	async function reconnect(): Promise<void> {
		if (shouldStopReconnecting() === true || isConnecting === true) {
			return;
		}

		isConnecting = true;
		try {
			await authenticateForConnection();
		} catch {
			options.onError?.();
			scheduleReconnect();
			return;
		} finally {
			isConnecting = false;
		}

		if (shouldStopReconnecting() === true) {
			return;
		}

		let nextSource: EventSource;
		try {
			nextSource = new EventSource(resolveUrl());
		} catch {
			options.onError?.();
			scheduleReconnect();
			return;
		}

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
			if (shouldRefreshAuthenticationAfterError() === true) {
				shouldRefreshAuthentication = true;
				logout();
			}
			closeSource();
			options.onError?.();

			// iOS drops the connections of a backgrounded tab and freezes its timers, so a
			// hidden page reconnects when it is shown again instead of on a timer.
			if (document.visibilityState === 'hidden') {
				return;
			}

			void reloadIfLoginRedirect();
			scheduleReconnect();
		};
	}

	function stopHiddenTimer(): number {
		if (hiddenTimer === null) {
			return 0;
		}

		clearInterval(hiddenTimer);
		hiddenTimer = null;
		return Math.max(hiddenMaxTickGapMs, Date.now() - hiddenLastTickAt);
	}

	// Safari can resume a suspended page with the old connection still reporting OPEN
	// while nothing arrives on it. Suspension shows as a stalled timer, so only a page
	// that was actually suspended drops a healthy-looking connection.
	function didChangeLifecycle(event: Event): void {
		if (document.visibilityState === 'hidden') {
			if (hiddenTimer === null) {
				hiddenLastTickAt = Date.now();
				hiddenMaxTickGapMs = 0;
				hiddenTimer = setInterval(() => {
					const now = Date.now();
					hiddenMaxTickGapMs = Math.max(hiddenMaxTickGapMs, now - hiddenLastTickAt);
					hiddenLastTickAt = now;
				}, HIDDEN_TICK_MS);
			}
			return;
		}

		const hiddenTickGapMs = stopHiddenTimer();

		const isStale =
			source === null ||
			source.readyState === EventSource.CLOSED ||
			hiddenTickGapMs >= SUSPENDED_TIMER_GAP_MS ||
			event.type === 'online' ||
			(event instanceof PageTransitionEvent && event.persisted === true);

		if (shouldReconnect === false || isStale === false || isConnecting === true) {
			return;
		}

		clearReconnectTimer();
		closeSource();
		void reconnect();
	}

	function setLifecycleListeners(isListening: boolean): void {
		const method = isListening ? 'addEventListener' : 'removeEventListener';
		document[method]('visibilitychange', didChangeLifecycle);
		window[method]('pageshow', didChangeLifecycle);
		window[method]('online', didChangeLifecycle);
	}

	return {
		connect: () => {
			shouldReconnect = true;
			setLifecycleListeners(true);
			void reconnect();
		},
		close: () => {
			shouldReconnect = false;
			setLifecycleListeners(false);
			stopHiddenTimer();
			clearReconnectTimer();
			closeSource();
		},
		getSource: () => source
	};
}
