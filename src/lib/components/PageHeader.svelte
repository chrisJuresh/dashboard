<script lang="ts">
	import type { Snippet } from 'svelte';

	let {
		title,
		subtitle,
		actions
	}: { title: string; subtitle?: string; actions?: Snippet } = $props();
</script>

<header class="page-header">
	<div class="titles">
		<h1>{title}</h1>
		{#if subtitle}<p class="subtitle muted">{subtitle}</p>{/if}
	</div>
	{#if actions}
		<div class="actions">{@render actions()}</div>
	{/if}
</header>

<style>
	.page-header {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: var(--gap);
		flex-wrap: wrap;
		margin-bottom: var(--gap);
	}
	.titles {
		min-width: 0;
	}
	h1 {
		font-size: 22px;
		line-height: 1.2;
	}
	.subtitle {
		margin: 4px 0 0;
		font-size: 14px;
	}
	.actions {
		display: flex;
		align-items: center;
		gap: 8px;
		flex-shrink: 0;
		margin-left: auto;
	}
</style>
