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

	// Four visually-distinct states, green→warm by power draw:
	//   asleep (standby/sleeping) → good (green): platters parked, ~0 W
	//   active (I/O this cycle)   → serious (orange): spinning + working
	//   spinning/idle (awake, no I/O) → warning (amber): spun up but quiet
	//   not measured (unknown/'') → hatched: state couldn't be read (e.g. a
	//     `protected` drive a3watch never probes, or a transient hdparm miss)
	function cellFill(p: Cell): string {
		const st = p.power_state;
		if (st === 'standby' || st === 'sleeping') return 'var(--good)';
		if (st === 'unknown' || st === '' || st == null) return 'url(#as-hatch)';
		if (p.active === 1) return 'var(--serious)';
		return 'var(--warning)'; // idle / active-idle: awake but no I/O this cycle
	}
	function stateLabel(p: Cell): string {
		const st = p.power_state;
		if (st === 'standby' || st === 'sleeping') return 'asleep';
		if (st === 'unknown' || st === '' || st == null) return 'not measured';
		if (p.active === 1) return 'active (I/O)';
		return 'spinning (idle)';
	}

	const cellW = $derived(points.length ? W / points.length : 0);
	const rectW = $derived(Math.max(1, cellW - GAP));
</script>

<div class="strip" bind:clientWidth={cw}>
	{#if !points.length}
		<div class="empty muted" style="height:{height}px">no data</div>
	{:else}
		<svg viewBox="0 0 {W} {height}" width={W} {height} role="img">
			<defs>
				<pattern
					id="as-hatch"
					width="6"
					height="6"
					patternUnits="userSpaceOnUse"
					patternTransform="rotate(45)"
				>
					<rect width="6" height="6" fill="var(--surface-2)" />
					<line x1="0" y1="0" x2="0" y2="6" stroke="var(--text-muted)" stroke-width="1.5" />
				</pattern>
			</defs>
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
		<span class="leg-item"><span class="sw" style="background:var(--good)"></span>asleep</span>
		<span class="leg-item"><span class="sw" style="background:var(--warning)"></span>spinning (idle)</span>
		<span class="leg-item"><span class="sw" style="background:var(--serious)"></span>active (I/O)</span>
		<span class="leg-item"><span class="sw sw-hatch"></span>not measured</span>
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
	.sw-hatch {
		background: repeating-linear-gradient(
			45deg,
			var(--surface-2),
			var(--surface-2) 2px,
			var(--text-muted) 2px,
			var(--text-muted) 3px
		);
	}
</style>
