<script lang="ts">
	import { developerPinRequest, submitDeveloperPin } from '$lib/auth';

	// Shown when the device rejects the default developer PIN, i.e. on production
	// and demo devices, which take a per-device PIN from provisioning.
	let pin = $state<string>('');

	function didSubmit(event: SubmitEvent): void {
		event.preventDefault();
		if (pin === '') {
			return;
		}
		const entered = pin;
		pin = '';
		submitDeveloperPin(entered);
	}
</script>

{#if $developerPinRequest !== null}
	<div class="developer-pin-scrim"></div>
	<form class="developer-pin-prompt" onsubmit={didSubmit}>
		<p class="developer-pin-title">developer pin</p>
		<p class="developer-pin-subtitle">this device does not accept the default pin</p>
		<!-- svelte-ignore a11y_autofocus -->
		<input
			type="password"
			inputmode="numeric"
			autocomplete="current-password"
			aria-label="developer pin"
			autofocus
			bind:value={pin}
		/>
		<div class="developer-pin-actions">
			<div class="developer-pin-spacer"></div>
			<button type="submit" disabled={pin === ''}>sign in</button>
		</div>
		{#if $developerPinRequest.error !== ''}
			<p class="developer-pin-error">{$developerPinRequest.error}</p>
		{/if}
	</form>
{/if}

<style>
	.developer-pin-scrim {
		position: fixed;
		inset: 0;
		background: rgba(0, 0, 0, 0.4);
		z-index: 199;
	}

	.developer-pin-prompt {
		position: fixed;
		top: 40%;
		left: 50%;
		transform: translate(-50%, -50%);
		z-index: 201;
		min-width: 24em;
		max-width: min(36em, 90vw);
		padding: 1.5em;
		background: white;
		border: 2px solid black;
		box-shadow: 4px 4px 0 rgba(0, 0, 0, 0.25);
		text-transform: uppercase;
	}

	.developer-pin-title {
		margin: 0;
	}

	.developer-pin-subtitle {
		margin: 0.5em 0 1em;
	}

	.developer-pin-prompt input {
		width: 100%;
		box-sizing: border-box;
	}

	.developer-pin-actions {
		display: flex;
		gap: 1em;
		align-items: center;
		margin-top: 1.5em;
	}

	.developer-pin-spacer {
		flex: 1;
	}

	.developer-pin-actions button {
		margin-top: 0;
	}

	.developer-pin-error {
		margin-top: 1em;
		color: #c40000;
	}
</style>
