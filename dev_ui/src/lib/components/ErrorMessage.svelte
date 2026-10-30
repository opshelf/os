<script lang="ts">
	import { createToastSource } from '$lib/toasts';

	type Props = {
		message: string;
	};

	let { message }: Props = $props();

	// This component states what it currently observes, it does not announce it: the
	// queue decides visibility, so remounting on navigation does not re-toast a fault
	// the technician already dismissed.
	const source = createToastSource('error');

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
		source.report(formattedMessage);
	});

	$effect(() => () => source.release());
</script>
