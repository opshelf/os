<script lang="ts">
	import { page } from '$app/stores';
	import '../app.css';

	const version = import.meta.env.PUBLIC_VERSION;

	const links = [
		{ href: '/', label: 'status' },
		{ href: '/command', label: 'command' },
		{ href: '/camera', label: 'camera' }
	];

	let { children } = $props();

	function isActiveLink(href: string, currentPath: string): boolean {
		if (href === '/') {
			return currentPath === '/';
		}
		return currentPath.startsWith(href);
	}
</script>

<svelte:head>
	<link rel="icon" href="/favicon.svg" />
</svelte:head>

<nav>
	<ul>
		{#each links as link}
			<li>
				<a href={link.href} class:active={isActiveLink(link.href, $page.url.pathname)}>
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

<div class="version-label">os-1 dev ui {version}</div>

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
