<script lang="ts">
	import type { Snippet } from 'svelte';

	let {
		title,
		actions,
		children
	}: { title?: string; actions?: Snippet; children?: Snippet } = $props();
</script>

<section class="card">
	{#if title || actions}
		<div class="card-head">
			{#if title}<h2 class="card-title">{title}</h2>{/if}
			{#if actions}<div class="card-actions">{@render actions()}</div>{/if}
		</div>
	{/if}
	{@render children?.()}
</section>

<style>
	.card-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 10px;
		flex-wrap: wrap;
		margin: 0 0 12px;
	}
	.card-title {
		font-size: 13px;
		font-weight: 650;
		letter-spacing: 0.02em;
		text-transform: uppercase;
		color: var(--text-secondary);
		margin: 0;
	}
	.card-actions {
		display: flex;
		align-items: center;
		gap: 6px;
	}
</style>
