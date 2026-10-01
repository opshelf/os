<script lang="ts">
	import { postToast } from '$lib/toasts';

	type Props = {
		message: string;
	};

	let { message }: Props = $props();

	const formattedMessage = $derived.by(() => {
		const trimmed = message.trim();

		if (trimmed.startsWith('{') || trimmed.startsWith('[')) {
			try {
				const parsed = JSON.parse(trimmed);
				return JSON.stringify(parsed, null, 2);
			} catch {
				return message;
			}
		}

		return message;
	});

	$effect(() => {
		if (formattedMessage.trim() === '') {
			return;
		}

		postToast('error', formattedMessage);
	});
</script>
