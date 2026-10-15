<script lang="ts">
	import { page } from '$app/stores';
	import { browser } from '$app/environment';
	import { onMount } from 'svelte';
	import {
		apiFetch,
		buildAuthenticatedUrl,
		createReconnectingEventSource,
		developerPin,
		getSizerStatus,
		isApiConnected,
		isAuthenticated,
		loginAsDeveloper,
		robotInit,
		robotStatus,
		robotStatusError,
		robotStatusLastUpdate,
		type RobotInitState,
		type WorkerStatusPayload
	} from '$lib';
	import { devicePath, stripDeviceId } from '$lib/nav';
	import NeedsHomeToast from '$lib/components/NeedsHomeToast.svelte';
	import SafetyAckAlert from '$lib/components/SafetyAckAlert.svelte';
	import ToastStack from '$lib/components/ToastStack.svelte';
	import '../app.css';

	const version = import.meta.env.PUBLIC_VERSION;

	type NavLink = {
		href: string;
		label: string;
		requiresSizer?: boolean;
	};

	const links: NavLink[] = [
		{ href: '/', label: 'status' },
		{ href: '/storage', label: 'storage' },
		{ href: '/camera', label: 'cameras' },
		{ href: '/scale', label: 'scale' },
		{ href: '/sizer', label: 'sizer', requiresSizer: true },
		{ href: '/scanner', label: 'scanner' },
		{ href: '/printer', label: 'printer' },
		{ href: '/motors', label: 'motors' },
		{ href: '/wifi', label: 'wifi' }
	];

	let { children } = $props();
	let authReady = $state(false);
	let apiState = $state<'unknown' | 'down' | 'up'>('unknown');
	let deviceLabel = $state('');
	let isSizerEnabled = $state(false);
	let statusStream: ReturnType<typeof createReconnectingEventSource> | null = null;
	let didReceiveRobotUnavailable = false;

	async function loadNavigationState(): Promise<void> {
		const [systemInfo, sizerStatus] = await Promise.allSettled([
			apiFetch<{ device_id: string; device_alias: string | null }>('/system/info'),
			getSizerStatus()
		]);

		if (systemInfo.status === 'fulfilled') {
			const alias = systemInfo.value.device_alias?.trim();
			deviceLabel = (alias || systemInfo.value.device_id).toUpperCase();
		}

		isSizerEnabled = sizerStatus.status === 'fulfilled' && sizerStatus.value.enabled === true;
	}

	function setApiState(state: 'unknown' | 'down' | 'up'): void {
		apiState = state;
		isApiConnected.set(state === 'up');
	}

	async function probeApiState(): Promise<void> {
		try {
			const response = await fetch(buildAuthenticatedUrl('/robot/status'), {
				headers: { Accept: 'text/event-stream' }
			});
			await response.body?.cancel();
			setApiState(response.status >= 500 ? 'down' : 'up');
		} catch {
			setApiState('down');
		}
	}

	function isActiveLink(href: string, currentPath: string): boolean {
		const path = stripDeviceId(currentPath);

		if (href === '/') {
			return path === '/';
		}
		return path.startsWith(href);
	}

	function didReceiveRobotStatus(raw: string): void {
		try {
			didReceiveRobotUnavailable = false;
			robotStatus.set(JSON.parse(raw) as WorkerStatusPayload);
			robotStatusError.set('');
			robotStatusLastUpdate.set(new Date().toLocaleString());
			setApiState('up');
		} catch {
			robotStatusError.set('failed to parse robot status payload');
		}
	}

	onMount(() => {
		if (browser === false) {
			return;
		}

		async function startStatusStream(): Promise<void> {
			void probeApiState();

			if (isAuthenticated() === false && developerPin() !== null) {
				try {
					await loginAsDeveloper();
				} catch (error) {
					console.error('failed to sign in as developer:', error);
				}
			}

			authReady = true;
			void loadNavigationState();

			const stream = createReconnectingEventSource({
				path: '/robot/status',
				refreshAuthenticationOnError: () => didReceiveRobotUnavailable === false,
				onOpen: () => {
					setApiState('up');
					robotStatusError.set('');
				},
				onMessage: (event) => {
					didReceiveRobotStatus(event.data);
				},
				onError: () => {
					if (didReceiveRobotUnavailable === true) {
						didReceiveRobotUnavailable = false;
						setApiState('up');
						return;
					}

					setApiState('down');
					robotStatusError.set('lost connection to robot status stream');
				},
				events: {
					init_progress: (event) => {
						if (event instanceof MessageEvent === false) {
							return;
						}

						try {
							const init = JSON.parse(event.data) as RobotInitState;
							robotInit.set(init);
							if (init.phase !== 'ready' && init.phase !== 'disabled') {
								robotStatus.set(null);
								robotStatusLastUpdate.set('');
							}
							setApiState('up');
						} catch {
							// ignore malformed init progress payloads
						}
					},
					robot_unavailable: (event) => {
						if (event instanceof MessageEvent === false) {
							return;
						}

						didReceiveRobotUnavailable = true;
						robotStatus.set(null);
						robotStatusError.set(event.data);
						setApiState('up');
					}
				}
			});
			statusStream = stream;
			stream.connect();
		}

		void startStatusStream();

		return () => {
			statusStream?.close();
			statusStream = null;
		};
	});
</script>

<svelte:head>
	<link rel="icon" href="/favicon.svg" />
	<title>Dev UI • Openshelf</title>
</svelte:head>

<nav>
	<ul>
		{#each links as link}
			{#if link.requiresSizer !== true || isSizerEnabled === true}
				<li>
					<a
						href={devicePath(link.href)}
						class:active={isActiveLink(link.href, $page.url.pathname)}
					>
						{link.label}
					</a>
				</li>
			{/if}
		{/each}

		<li class="spacer-flexible"></li>

		{#if deviceLabel !== ''}
			<li class="device-label">{deviceLabel}</li>
		{/if}

		<li>
			<a href={`http://${document.location.hostname}:8888/`} class="close-btn">Close</a>
		</li>
	</ul>
</nav>

<main>
	{@render children?.()}
</main>

<NeedsHomeToast />
<SafetyAckAlert />

{#if apiState === 'down'}
	<div class="connection-overlay">
		<div class="connection-message">DEVICE OR API IS UNREACHABLE</div>
	</div>
{:else if authReady === false}
	<div class="connection-overlay">
		<div class="connection-message">AUTHENTICATING, PLEASE WAIT</div>
	</div>
{:else if apiState === 'unknown'}
	<div class="connection-overlay">
		<div class="connection-message">CONNECTING, PLEASE WAIT</div>
	</div>
{/if}

<ToastStack />
