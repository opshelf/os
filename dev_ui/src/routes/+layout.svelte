<script lang="ts">
	import { page } from '$app/stores';
	import { browser } from '$app/environment';
	import { onMount } from 'svelte';
	import {
		createReconnectingEventSource,
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
	import '../app.css';

	const version = import.meta.env.PUBLIC_VERSION;

	const links = [
		{ href: '/', label: 'status' },
		{ href: '/calibration', label: 'storage' },
		{ href: '/camera', label: 'cameras' },
		{ href: '/scale', label: 'scale' },
		{ href: '/sizer', label: 'sizer' },
		{ href: '/scanner', label: 'scanner' },
		{ href: '/printer', label: 'printer' },
		{ href: '/motors', label: 'motors' },
		{ href: '/wifi', label: 'wifi' }
	];

	let { children } = $props();
	let authReady = $state(false);
	let statusStream: ReturnType<typeof createReconnectingEventSource> | null = null;
	let didReceiveRobotUnavailable = false;

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
			isApiConnected.set(true);
		} catch {
			robotStatusError.set('failed to parse robot status payload');
		}
	}

	onMount(() => {
		if (browser === false) {
			return;
		}

		async function startStatusStream(): Promise<void> {
			if (isAuthenticated() === false) {
				try {
					await loginAsDeveloper();
				} catch (error) {
					console.error('failed to authenticate as developer:', error);
				}
			}

			authReady = true;

			const stream = createReconnectingEventSource({
				path: '/robot/status',
				refreshAuthenticationOnError: () => didReceiveRobotUnavailable === false,
				onOpen: () => {
					isApiConnected.set(true);
					robotStatusError.set('');
				},
				onMessage: (event) => {
					didReceiveRobotStatus(event.data);
				},
				onError: () => {
					if (didReceiveRobotUnavailable === true) {
						didReceiveRobotUnavailable = false;
						isApiConnected.set(true);
						return;
					}

					isApiConnected.set(false);
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
							isApiConnected.set(true);
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
						isApiConnected.set(true);
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
	<title>openshelf ┈ dev ui</title>
</svelte:head>

<nav>
	<ul>
		{#each links as link}
			<li>
				<a href={devicePath(link.href)} class:active={isActiveLink(link.href, $page.url.pathname)}>
					{link.label}
				</a>
			</li>
		{/each}

		<li class="spacer-flexible"></li>

		<li>
			<a href={`http://${document.location.hostname}:8888/`}>Ray</a>
		</li>
	</ul>
</nav>

<main>
	{@render children?.()}
</main>

{#if authReady === false || $isApiConnected === false}
	<div class="connection-overlay">
		<div class="connection-message">
			{authReady === false ? 'AUTHENTICATING, PLEASE WAIT' : 'CONNECTING, PLEASE WAIT'}
		</div>
	</div>
{/if}

<div class="logo-container">
	<table class="logo-table">
		<tbody>
			<tr class="row-1"><td></td></tr>
			<tr class="row-2"><td></td></tr>
			<tr class="row-3"><td></td></tr>
		</tbody>
	</table>
</div>

<style>
	.logo-container {
		position: fixed;
		bottom: 1.5em;
		right: 1.5em;
	}

	.logo-table {
		width: 4em;
		height: 4em;
		border-collapse: collapse;
	}

	.logo-table td {
		border: 3px solid #00a210;
		padding: 0;
	}

	.logo-table .row-1 td {
		height: 21.74%;
	}

	.logo-table .row-2 td {
		height: 34.78%;
	}

	.logo-table .row-3 td {
		height: 43.48%;
	}
</style>
