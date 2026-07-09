<script lang="ts">
	import { fmtDateTime } from '$lib/format';

	interface Cell {
		ts: number;
		active: number;
		power_state: string;
	}
	interface Props {
		points: Cell[];
		height?: number;
	}
	let { points, height = 34 }: Props = $props();

	let cw = $state(0);
	const W = $derived(Math.max(200, cw || 800));
	const GAP = 2;

	function cellFill(p: Cell): string {
		const st = p.power_state;
		if (st === 'standby' || st === 'sleeping') return 'var(--good)';
		if (p.active === 1 || st === 'active') return 'var(--warning)';
		return 'var(--surface-2)';
	}
	function stateLabel(p: Cell): string {
		const st = p.power_state;
		if (st === 'standby' || st === 'sleeping') return st;
		if (p.active === 1 || st === 'active') return 'active';
		return st || 'unknown';
	}

	const cellW = $derived(points.length ? W / points.length : 0);
	const rectW = $derived(Math.max(1, cellW - GAP));
</script>

<div class="strip" bind:clientWidth={cw}>
	{#if !points.length}
		<div class="empty muted" style="height:{height}px">no data</div>
	{:else}
		<svg viewBox="0 0 {W} {height}" width={W} {height} role="img">
			{#each points as p, i (p.ts)}
				<rect
					x={i * cellW}
					y="0"
					width={rectW}
					height={height}
					rx="2"
					style="fill:{cellFill(p)}"
				>
					<title>{fmtDateTime(p.ts)} · {stateLabel(p)} · active={p.active}</title>
				</rect>
			{/each}
		</svg>
	{/if}

	<div class="legend">
		<span class="leg-item"><span class="sw" style="background:var(--good)"></span>standby / sleeping</span>
		<span class="leg-item"><span class="sw" style="background:var(--warning)"></span>active</span>
		<span class="leg-item"><span class="sw" style="background:var(--surface-2)"></span>idle / unknown</span>
	</div>
</div>

<style>
	.strip {
		width: 100%;
		max-width: 100%;
		overflow: hidden;
	}
	svg {
		display: block;
		width: 100%;
		height: auto;
	}
	.empty {
		display: flex;
		align-items: center;
		justify-content: center;
		font-size: 13px;
	}
	.legend {
		display: flex;
		flex-wrap: wrap;
		gap: 6px 14px;
		margin-top: 6px;
		font-size: 12px;
		color: var(--text-muted);
	}
	.leg-item {
		display: inline-flex;
		align-items: center;
		gap: 6px;
	}
	.sw {
		width: 10px;
		height: 10px;
		border-radius: 2px;
		border: 1px solid var(--border);
		flex: none;
	}
</style>
