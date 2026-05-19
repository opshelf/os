<script lang="ts">
	import { page } from '$app/stores';
	import { browser } from '$app/environment';
	import { onMount } from 'svelte';
	import { loginAsDeveloper, isAuthenticated, isApiConnected } from '$lib';
	import '../app.css';

	const version = import.meta.env.PUBLIC_VERSION;

	const links = [
		{ href: '/', label: 'status' },
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
	<title>openshelf ┈ dev ui</title>
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
	{@render children?.()}
</main>

{#if authReady === false || $isApiConnected === false}
	<div class="connection-overlay">
		<div class="connection-message">
			{authReady === false ? 'AUTHENTICATING' : 'CONNECTING, PLEASE WAIT'}
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
	.connection-overlay {
		position: fixed;
		inset: 0;
		display: flex;
		align-items: center;
		justify-content: center;
		background-color: rgba(255, 255, 255, 0.85);
		z-index: 1000;
	}

	.connection-message {
		padding: 5px;
		border: 2px solid black;
		background-color: white;
		text-transform: uppercase;
		box-shadow: 4px 4px 0 rgba(0, 0, 0, 0.25);
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
