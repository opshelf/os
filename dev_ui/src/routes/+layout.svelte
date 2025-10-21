<script lang="ts">
	import favicon from '$lib/assets/favicon.svg';

	const links = [
		{ href: '/', label: 'Status' },
		{ href: '/command', label: 'Command' },
		{ href: '/camera', label: 'Camera' }
	];

	let { children } = $props();
</script>

<svelte:head>
	<link rel="icon" href={favicon} />
</svelte:head>

<nav>
	<ul>
		{#each links as link}
			<li><a href={link.href}>{link.label}</a></li>
		{/each}
	</ul>
</nav>

<main>
	{@render children?.()}
</main>
