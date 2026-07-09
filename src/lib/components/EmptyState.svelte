<script lang="ts">
	import type { Snippet } from 'svelte';

	let {
		title,
		message,
		children
	}: { title: string; message: string; children?: Snippet } = $props();
</script>

<div class="empty">
	<h3 class="title">{title}</h3>
	<p class="message muted">{message}</p>
	{#if children}<div class="action">{@render children()}</div>{/if}
</div>

<style>
	.empty {
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		text-align: center;
		gap: 8px;
		padding: 48px 24px;
		color: var(--text-secondary);
	}
	.title {
		font-size: 15px;
		font-weight: 650;
		color: var(--text-primary);
	}
	.message {
		margin: 0;
		max-width: 42ch;
		font-size: 13px;
		line-height: 1.5;
	}
	.action {
		margin-top: 8px;
	}
</style>
