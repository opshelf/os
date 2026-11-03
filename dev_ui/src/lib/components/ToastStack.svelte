<script lang="ts">
	import { dismissToast, splitToastMessage, toasts } from '$lib/toasts';

	// A drag that selects text ends in a click; keep the toast so the selection can be copied.
	function didClickToast(event: MouseEvent, id: number): void {
		const selection = window.getSelection();
		const node = event.currentTarget as Node;
		if (selection !== null && selection.isCollapsed === false && node.contains(selection.anchorNode)) {
			return;
		}
		dismissToast(id);
	}

	function didKeyToast(event: KeyboardEvent, id: number): void {
		if (event.key === 'Enter' || event.key === ' ') {
			event.preventDefault();
			dismissToast(id);
		}
	}
</script>

{#if $toasts.length > 0}
	<div class="toast-stack">
		{#each $toasts as toast (toast.id)}
			{@const { headline, detail } = splitToastMessage(toast.message)}
			{#if toast.actions !== undefined && toast.actions.length > 0}
				<div class="toast {toast.kind} with-actions" role="status">
					<div
						class="toast-body"
						role="button"
						tabindex="0"
						onclick={(event) => didClickToast(event, toast.id)}
						onkeydown={(event) => didKeyToast(event, toast.id)}
					>
						{headline}{#if detail !== ''}<span class="toast-detail">{detail}</span>{/if}
					</div>
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
				<div
					class="toast {toast.kind}"
					role="button"
					tabindex="0"
					onclick={(event) => didClickToast(event, toast.id)}
					onkeydown={(event) => didKeyToast(event, toast.id)}
				>
					{headline}{#if detail !== ''}<span class="toast-detail">{detail}</span>{/if}
				</div>
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
		box-shadow: 4px 4px 0 rgba(0, 0, 0, 0.25);
		cursor: pointer;
		/* buttons are unselectable app-wide; a toast's text is meant to be copied */
		-webkit-user-select: text;
		user-select: text;
	}

	/* exception text and ids are case-sensitive, so the detail is never uppercased */
	.toast-detail {
		display: block;
		margin-top: 0.75em;
		font-size: 0.8em;
		text-transform: none;
		white-space: pre-wrap;
		opacity: 0.9;
	}

	.toast-actions {
		display: flex;
		flex-wrap: wrap;
		gap: 0.75em;
		margin-top: 1em;
	}

	.toast.with-actions {
		white-space: normal;
		cursor: default;
	}

	.toast-body {
		cursor: pointer;
		white-space: pre-wrap;
		overflow-wrap: anywhere;
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
