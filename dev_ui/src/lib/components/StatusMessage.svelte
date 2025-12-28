<script lang="ts">
	type Props = {
		message: string;
	};

	let { message }: Props = $props();
</script>

<div class="message status">{message}</div>
