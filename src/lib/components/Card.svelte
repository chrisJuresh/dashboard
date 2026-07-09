<script lang="ts">
	import type { Snippet } from 'svelte';

	let { title, children }: { title?: string; children?: Snippet } = $props();
</script>

<section class="card">
	{#if title}<h2 class="card-title">{title}</h2>{/if}
	{@render children?.()}
</section>

<style>
	.card-title {
		font-size: 13px;
		font-weight: 650;
		letter-spacing: 0.02em;
		text-transform: uppercase;
		color: var(--text-secondary);
		margin: 0 0 12px;
	}
</style>
