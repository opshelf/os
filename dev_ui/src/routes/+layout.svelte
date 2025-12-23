<script lang="ts">
	import { page } from '$app/stores';
	import { browser } from '$app/environment';
	import { onMount } from 'svelte';
	import { loginAsDeveloper, isAuthenticated, isApiConnected } from '$lib';
	import '../app.css';

	const version = import.meta.env.PUBLIC_VERSION;

	const links = [
		{ href: '/', label: 'status' },
		{ href: '/command', label: 'command' },
		{ href: '/calibration', label: 'calibration' },
		{ href: '/camera', label: 'cameras' },
		{ href: '/scale', label: 'scale' },
		{ href: '/scanner', label: 'scanner' },
		{ href: '/motors', label: 'motors' }
	];

	let { children } = $props();
	let authReady = $state(false);

	function isActiveLink(href: string, currentPath: string): boolean {
		if (href === '/') {
			return currentPath === '/';
		}
		return currentPath.startsWith(href);
	}

	onMount(async () => {
		if (browser === false) {
			return;
		}

		if (isAuthenticated() === false) {
			try {
				await loginAsDeveloper();
			} catch (error) {
				console.error('failed to authenticate as developer:', error);
			}
		}

		authReady = true;
	});
</script>

<svelte:head>
	<link rel="icon" href="/favicon.svg" />
</svelte:head>

<nav>
	<ul>
		{#if $isApiConnected === true}
			{#each links as link}
				<li>
					<a href={link.href} class:active={isActiveLink(link.href, $page.url.pathname)}>
						{link.label}
					</a>
				</li>
			{/each}
		{/if}

		<li class="spacer-flexible"></li>

		<li>
			<a href={`http://${document.location.hostname}:8888/`}>Ray</a>
		</li>
	</ul>
</nav>

<main>
	{#if $isApiConnected === false}
		<p class="offline-message">API is offline, re-connecting...</p>
	{:else if authReady === true}
		{@render children?.()}
	{:else}
		<p>authenticating...</p>
	{/if}
</main>

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
	.version-label {
		padding: 0.5em;
	}

	.offline-message {
		padding: 2em;
		text-align: center;
		color: #888;
		font-size: 1.2em;
	}

	.logo-container {
		position: fixed;
		bottom: 2em;
		right: 2em;
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
