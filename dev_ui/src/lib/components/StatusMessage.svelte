<script lang="ts">
	import { postToast } from '$lib/toasts';

	type Props = {
		message: string;
	};

	let { message }: Props = $props();

	$effect(() => {
		if (message.trim() === '') {
			return;
		}

		postToast('status', message);
	});
</script>
