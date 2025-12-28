<script lang="ts">
	type Props = {
		message: string;
	};

	let { message }: Props = $props();

	const formattedMessage = $derived(() => {
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
</script>

<div class="message error">{formattedMessage()}</div>
