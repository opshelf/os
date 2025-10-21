<script lang="ts">
	import { page } from '$app/stores';
	import favicon from '$lib/assets/favicon.svg';
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
	<link rel="icon" href={favicon} />
</svelte:head>

<nav>
	<ul>
		{#each links as link}
			<li><a href={link.href} class:active={isActiveLink(link.href, $page.url.pathname)}>{link.label}</a></li>
		{/each}
	</ul>
</nav>

<main>
	{@render children?.()}
</main>

<div class="version-label">os-1 dev ui {version}</div>
