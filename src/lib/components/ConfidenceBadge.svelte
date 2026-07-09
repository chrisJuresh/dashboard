<script lang="ts">
	import { confidenceStatus } from '$lib/format';
	import type { Confidence } from '$lib/api';
	import Badge from './Badge.svelte';

	let { confidence }: { confidence: Confidence } = $props();

	const glyphs: Record<Confidence, string> = {
		high: '●●●', // ●●●
		medium: '●●○', // ●●○
		low: '●○○' // ●○○
	};

	const label = $derived(confidence.charAt(0).toUpperCase() + confidence.slice(1));
</script>

<Badge status={confidenceStatus(confidence)} icon={glyphs[confidence]} {label} />
