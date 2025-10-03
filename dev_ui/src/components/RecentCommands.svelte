<script lang="ts">
	import { getRecentCommands, clearRecentCommands } from '../lib/storage';
	import type { RecentCommand } from '../lib/storage';

	export let onCommandSelect: (command: any) => void;

	let recentCommands: RecentCommand[] = [];

	function loadCommands() {
		recentCommands = getRecentCommands();
	}

	function didClickCommand(command: any) {
		onCommandSelect(command);
	}

	function didClearHistory() {
		clearRecentCommands();
		loadCommands();
	}

	function formatTimestamp(timestamp: number): string {
		const date = new Date(timestamp);
		return date.toLocaleTimeString();
	}

	$: {
		loadCommands();
	}
</script>

<div class="recent-commands">
	<h2>recent commands</h2>

	{#if recentCommands.length === 0}
		<p>no recent commands</p>
	{:else}
		<ul>
			{#each recentCommands as item}
				<li>
					<div class="command-info">
						<span class="command-name">{item.command.command}</span>
						<span class="timestamp">{formatTimestamp(item.timestamp)}</span>
					</div>
					<button on:click={() => didClickCommand(item.command)}>resend</button>
				</li>
			{/each}
		</ul>

		<button on:click={didClearHistory}>clear history</button>
	{/if}
</div>

<style>
	.recent-commands {
		padding: 0;
	}

	h2 {
		margin: 0 0 1rem 0;
	}

	ul {
		list-style: none;
		padding: 0;
		margin: 0 0 1rem 0;
	}

	li {
		display: flex;
		justify-content: space-between;
		align-items: center;
		padding: 0.5rem 0;
		margin-bottom: 0.5rem;
		border: none;
	}

	.command-info {
		display: flex;
		flex-direction: column;
	}

	.command-name {
		font-weight: normal;
	}

	button {
		padding: 0.25rem 0.5rem;
		cursor: pointer;
	}

	p {
		margin: 0;
	}
</style>
