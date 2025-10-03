<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { createStatusStream } from '../lib/api';

	let messages: Array<{ timestamp: number; data: string }> = [];
	let eventSource: EventSource | null = null;
	let logContainer: HTMLDivElement;
	let isPaused: boolean = false;

	onMount(() => {
		didConnect();
	});

	onDestroy(() => {
		didDisconnect();
	});

	function didConnect() {
		if (eventSource !== null) {
			return;
		}

		eventSource = createStatusStream();

		eventSource.onmessage = (event) => {
			if (isPaused === true) {
				return;
			}

			messages = [
				...messages,
				{
					timestamp: Date.now(),
					data: event.data,
				},
			];

			if (logContainer !== undefined) {
				setTimeout(() => {
					logContainer.scrollTop = logContainer.scrollHeight;
				}, 0);
			}
		};

		eventSource.onerror = () => {
			console.error('SSE connection error');
		};
	}

	function didDisconnect() {
		if (eventSource !== null) {
			eventSource.close();
			eventSource = null;
		}
	}

	function didClearLog() {
		messages = [];
	}

	function didTogglePause() {
		isPaused = isPaused === false;
	}

	function formatTimestamp(timestamp: number): string {
		const date = new Date(timestamp);
		return date.toLocaleTimeString();
	}
</script>

<div class="status-log">
	<div class="header">
		<h2>robot status</h2>
		<div class="controls">
			<button on:click={didTogglePause}>
				{isPaused === true ? 'resume' : 'pause'}
			</button>
			<button on:click={didClearLog}>clear</button>
		</div>
	</div>

	<div class="log-container" bind:this={logContainer}>
		{#if messages.length === 0}
			<p>no messages yet...</p>
		{:else}
			{#each messages as message}
				<div class="log-entry">
					<span class="timestamp">{formatTimestamp(message.timestamp)}</span>
					<span class="data">{message.data}</span>
				</div>
			{/each}
		{/if}
	</div>
</div>

<style>
	.status-log {
		padding: 0;
		display: flex;
		flex-direction: column;
		height: 100%;
	}

	.header {
		display: flex;
		justify-content: space-between;
		align-items: center;
		margin-bottom: 1rem;
	}

	h2 {
		margin: 0;
	}

	.controls {
		display: flex;
		gap: 0.5rem;
	}

	button {
		padding: 0.25rem 0.5rem;
		cursor: pointer;
	}

	.log-container {
		flex: 1;
		overflow-y: auto;
		border: none;
		padding: 0;
	}

	.log-entry {
		margin-bottom: 0.25rem;
		display: flex;
		gap: 1rem;
	}

	.timestamp {
		flex-shrink: 0;
	}

	.data {
		word-break: break-all;
	}

	p {
		margin: 0;
	}
</style>
