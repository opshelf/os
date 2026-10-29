import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./api', () => ({ buildAuthenticatedUrl: (path: string) => `http://api${path}` }));
vi.mock('./auth', () => ({
	developerPin: () => null,
	ensureValidToken: async () => {},
	isAuthenticated: () => false,
	loginAsDeveloper: async () => {},
	logout: () => {}
}));

class FakeEventSource {
	static readonly CONNECTING = 0;
	static readonly OPEN = 1;
	static readonly CLOSED = 2;
	static instances: FakeEventSource[] = [];

	readyState = FakeEventSource.CONNECTING;
	onopen: (() => void) | null = null;
	onmessage: ((event: MessageEvent<string>) => void) | null = null;
	onerror: (() => void) | null = null;

	constructor(readonly url: string) {
		FakeEventSource.instances.push(this);
	}

	addEventListener(): void {}
	removeEventListener(): void {}

	close(): void {
		this.readyState = FakeEventSource.CLOSED;
	}

	open(): void {
		this.readyState = FakeEventSource.OPEN;
		this.onopen?.();
	}

	fail(): void {
		this.readyState = FakeEventSource.CLOSED;
		this.onerror?.();
	}
}

let visibilityState: DocumentVisibilityState = 'visible';
let documentTarget = new EventTarget();
let windowTarget = new EventTarget();

function setVisibility(state: DocumentVisibilityState): void {
	visibilityState = state;
	documentTarget.dispatchEvent(new Event('visibilitychange'));
}

const { createReconnectingEventSource } = await import('./sse');

async function flush(): Promise<void> {
	await vi.advanceTimersByTimeAsync(0);
}

function latest(): FakeEventSource {
	return FakeEventSource.instances[FakeEventSource.instances.length - 1];
}

describe('createReconnectingEventSource page lifecycle', () => {
	beforeEach(() => {
		vi.useFakeTimers();
		FakeEventSource.instances = [];
		visibilityState = 'visible';
		documentTarget = new EventTarget();
		windowTarget = new EventTarget();
		vi.stubGlobal('EventSource', FakeEventSource);
		vi.stubGlobal('PageTransitionEvent', class extends Event {});
		vi.stubGlobal('fetch', vi.fn(async () => new Response(null, { status: 200 })));
		vi.stubGlobal('document', {
			get visibilityState() {
				return visibilityState;
			},
			addEventListener: documentTarget.addEventListener.bind(documentTarget),
			removeEventListener: documentTarget.removeEventListener.bind(documentTarget)
		});
		vi.stubGlobal('window', {
			addEventListener: windowTarget.addEventListener.bind(windowTarget),
			removeEventListener: windowTarget.removeEventListener.bind(windowTarget)
		});
	});

	afterEach(() => {
		vi.useRealTimers();
		vi.unstubAllGlobals();
	});

	it('reports a hidden error but reconnects only when the page is shown again', async () => {
		const onError = vi.fn();
		const stream = createReconnectingEventSource({ path: '/robot/status', onError });
		stream.connect();
		await flush();
		latest().open();

		setVisibility('hidden');
		latest().fail();
		expect(onError).toHaveBeenCalledTimes(1);

		await vi.advanceTimersByTimeAsync(10_000);
		expect(FakeEventSource.instances).toHaveLength(1);

		setVisibility('visible');
		await flush();
		expect(FakeEventSource.instances).toHaveLength(2);
		stream.close();
	});

	it('keeps a healthy connection after a throttled hide', async () => {
		const stream = createReconnectingEventSource({ path: '/robot/status' });
		stream.connect();
		await flush();
		latest().open();

		setVisibility('hidden');
		await vi.advanceTimersByTimeAsync(30_000);
		setVisibility('visible');
		await flush();

		expect(FakeEventSource.instances).toHaveLength(1);
		expect(latest().readyState).toBe(FakeEventSource.OPEN);
		stream.close();
	});

	it('replaces an OPEN connection after the page was suspended', async () => {
		const stream = createReconnectingEventSource({ path: '/robot/status' });
		stream.connect();
		await flush();
		const suspended = latest();
		suspended.open();

		setVisibility('hidden');
		// a suspended page runs no timers while the clock moves on
		vi.setSystemTime(Date.now() + 60_000);
		setVisibility('visible');
		await flush();

		expect(FakeEventSource.instances).toHaveLength(2);
		expect(suspended.readyState).toBe(FakeEventSource.CLOSED);
		stream.close();
	});

	it('does not start a second connection while one is being set up', async () => {
		const stream = createReconnectingEventSource({ path: '/robot/status' });
		stream.connect();
		windowTarget.dispatchEvent(new Event('online'));
		windowTarget.dispatchEvent(new Event('online'));
		await flush();

		expect(FakeEventSource.instances).toHaveLength(1);
		stream.close();
	});

	it('stays closed after close() even when the page is shown', async () => {
		const stream = createReconnectingEventSource({ path: '/robot/status' });
		stream.connect();
		await flush();
		stream.close();

		setVisibility('hidden');
		setVisibility('visible');
		windowTarget.dispatchEvent(new Event('online'));
		await flush();

		expect(FakeEventSource.instances).toHaveLength(1);
	});
});
