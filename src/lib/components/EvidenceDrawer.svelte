<script lang="ts">
	import type { DiskEvent } from '$lib/api';

	let { event }: { event: DiskEvent } = $props();

	const maxWeight = $derived(
		event.evidence.reduce((m, e) => Math.max(m, e.weight), 0) || 1
	);
</script>

<details class="drawer">
	<summary>
		<span class="summary-label">Evidence</span>
		<span class="count muted tabular">{event.evidence.length}</span>
	</summary>

	{#if event.note}
		<p class="note">{event.note}</p>
	{/if}

	{#if event.evidence.length}
		<ul class="evidence">
			{#each event.evidence as e}
				<li class="row">
					<span class="signal tabular">{e.signal}</span>
					<span class="detail">{e.detail}</span>
					<span class="weight" title={`weight ${e.weight}`} aria-hidden="true">
						<span class="bar" style:width={`${(e.weight / maxWeight) * 100}%`}></span>
					</span>
				</li>
			{/each}
		</ul>
	{:else}
		<p class="muted">No evidence recorded.</p>
	{/if}
</details>

<style>
	.drawer {
		border-top: 1px solid var(--border);
		padding-top: 8px;
	}
	summary {
		display: flex;
		align-items: center;
		gap: 8px;
		cursor: pointer;
		list-style: none;
		font-size: 13px;
		font-weight: 600;
		color: var(--text-secondary);
		user-select: none;
	}
	summary::-webkit-details-marker {
		display: none;
	}
	summary::before {
		content: '\25B8'; /* ▸ */
		display: inline-block;
		transition: transform 0.12s ease;
		color: var(--text-muted);
	}
	.drawer[open] summary::before {
		transform: rotate(90deg);
	}
	.count {
		font-size: 12px;
		font-weight: 500;
	}
	.note {
		margin: 10px 0;
		color: var(--text-primary);
		font-size: 13px;
		line-height: 1.5;
	}
	.evidence {
		list-style: none;
		margin: 8px 0 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 6px;
	}
	.row {
		display: grid;
		grid-template-columns: minmax(90px, auto) 1fr 64px;
		align-items: center;
		gap: 10px;
		font-size: 12px;
	}
	.signal {
		font-family: ui-monospace, 'SFMono-Regular', Menlo, monospace;
		font-size: 11px;
		color: var(--text-secondary);
		background: var(--surface-2);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		padding: 1px 6px;
		justify-self: start;
		white-space: nowrap;
	}
	.detail {
		color: var(--text-secondary);
		min-width: 0;
	}
	.weight {
		height: 6px;
		border-radius: 999px;
		background: var(--surface-2);
		overflow: hidden;
	}
	.bar {
		display: block;
		height: 100%;
		background: var(--series-1);
		border-radius: 999px;
	}
	@media (max-width: 520px) {
		.row {
			grid-template-columns: 1fr;
			gap: 3px;
		}
		.weight {
			width: 100%;
		}
	}
</style>
