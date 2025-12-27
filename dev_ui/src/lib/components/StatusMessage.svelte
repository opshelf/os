<script lang="ts">
	type Props = {
		message: string;
	};

	let { message }: Props = $props();
</script>

<div class="status">{message}</div>

<style>
	.status {
		margin-top: 2em;
		text-transform: uppercase;
	}
</style>
