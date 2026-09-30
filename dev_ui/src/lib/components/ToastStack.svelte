<script lang="ts">
	import { dismissToast, toasts } from '$lib/toasts';
</script>

{#if $toasts.length > 0}
	<div class="toast-stack">
		{#each $toasts as toast (toast.id)}
			<button
				type="button"
				class="toast {toast.kind}"
				onclick={() => dismissToast(toast.id)}
			>
				{toast.message}
			</button>
		{/each}
	</div>
{/if}

<style>
	.toast-stack {
		position: fixed;
		right: 1.5em;
		bottom: 1.5em;
		z-index: 1001;
		display: flex;
		flex-direction: column;
		gap: 1.5em;
	}

	.toast {
		width: 320px;
		padding: 1em;
		border: none;
		color: white;
		text-align: left;
		white-space: pre-wrap;
		overflow-wrap: anywhere;
	}

	.toast.error {
		background-color: #c40000;
	}

	.toast.status {
		background-color: oklch(0.5318 0.2399 256.98999);
	}
</style>
