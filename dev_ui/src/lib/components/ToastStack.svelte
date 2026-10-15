<script lang="ts">
	import { dismissToast, toasts } from '$lib/toasts';
</script>

{#if $toasts.length > 0}
	<div class="toast-stack">
		{#each $toasts as toast (toast.id)}
			{#if toast.actions !== undefined && toast.actions.length > 0}
				<div class="toast {toast.kind} with-actions" role="status">
					<div>{toast.message}</div>
					<div class="toast-actions">
						{#each toast.actions as action (action.label)}
							<button
								type="button"
								disabled={action.disabled === true}
								onclick={() => void action.onClick()}
							>
								{action.label}
							</button>
						{/each}
					</div>
				</div>
			{:else}
				<button type="button" class="toast {toast.kind}" onclick={() => dismissToast(toast.id)}>
					{toast.message}
				</button>
			{/if}
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
		text-transform: uppercase;
	}

	.toast-actions {
		display: flex;
		flex-wrap: wrap;
		gap: 0.75em;
		margin-top: 1em;
	}

	.toast.with-actions {
		white-space: normal;
	}

	.toast.with-actions > :first-child {
		white-space: pre-wrap;
	}

	.toast-actions button {
		margin-top: 0;
		border-color: white;
		background-color: transparent;
		box-shadow: 4px 4px 0 rgba(255, 255, 255, 0.35);
		color: white;
	}

	.toast.error {
		background-color: #c40000;
	}

	.toast.status {
		background-color: oklch(0.5318 0.2399 256.98999);
	}
</style>
