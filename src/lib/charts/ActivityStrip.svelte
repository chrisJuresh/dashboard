<script lang="ts">
	import { fmtDateTime } from '$lib/format';

	interface Point {
		ts: number;
		active: number;
		power_state: string;
	}
	interface Props {
		points: Point[];
		height?: number;
	}
	let { points, height = 34 }: Props = $props();

	let cw = $state(0);
	const W = $derived(Math.max(200, cw || 800));
	const GAP = 2;
	// cap the cell count so a wide range (e.g. 7 d ≈ 10 000 samples) renders as a
	// readable strip rather than thousands of sub-pixel rects. Points are bucketed
	// down to this many cells; each cell shows the "most awake" state in its span.
	const MAX_CELLS = 360;

	type State = 'standby' | 'idle' | 'active' | 'unknown';
	// Rank for bucketing: the most-awake sample in a bucket wins, so a brief wake in
	// an otherwise-asleep span is never hidden, and "unknown" only shows when a whole
	// bucket was unmeasured (never masking a known state).
	const RANK: Record<State, number> = { active: 3, idle: 2, standby: 1, unknown: 0 };

	function pointState(p: Point): State {
		const st = p.power_state;
		if (st === 'standby' || st === 'sleeping') return 'standby';
		// recorded I/O proves the drive was spinning — this is how a cell that was
		// logged "unknown" (e.g. during a window when probing was off) gets filled in.
		if (p.active === 1) return 'active';
		if (st === 'idle' || st === 'active') return 'idle';
		return 'unknown';
	}

	interface Cell {
		ts: number;
		to: number;
		state: State;
		hadIO: boolean;
		n: number;
	}
	const cells = $derived.by<Cell[]>(() => {
		if (!points.length) return [];
		const size = Math.max(1, Math.ceil(points.length / MAX_CELLS));
		const out: Cell[] = [];
		for (let i = 0; i < points.length; i += size) {
			const group = points.slice(i, i + size);
			let best: State = 'unknown';
			let hadIO = false;
			for (const p of group) {
				const s = pointState(p);
				if (p.active === 1) hadIO = true;
				if (RANK[s] > RANK[best]) best = s;
			}
			out.push({
				ts: group[0].ts,
				to: group[group.length - 1].ts,
				state: best,
				hadIO,
				n: group.length
			});
		}
		return out;
	});

	function cellFill(c: Cell): string {
		if (c.state === 'standby') return 'var(--good)';
		if (c.state === 'unknown') return 'url(#as-hatch)';
		if (c.state === 'active') return 'var(--serious)';
		return 'var(--warning)'; // idle: spinning, no I/O
	}
	function cellLabel(c: Cell): string {
		if (c.state === 'standby') return 'asleep';
		if (c.state === 'unknown') return 'not measured';
		if (c.state === 'active') return 'active (I/O)';
		return 'spinning (idle)';
	}

	const cellW = $derived(cells.length ? W / cells.length : 0);
	const rectW = $derived(Math.max(1, cellW - (cellW > 3 ? GAP : 0)));
</script>

<div class="strip" bind:clientWidth={cw}>
	{#if !cells.length}
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
			{#each cells as c, i (c.ts)}
				<rect x={i * cellW} y="0" width={rectW} height={height} rx="2" style="fill:{cellFill(c)}">
					<title
						>{fmtDateTime(c.ts)}{c.n > 1 ? ` – ${fmtDateTime(c.to)}` : ''} · {cellLabel(c)}{c.n > 1
							? ` · ${c.n} samples`
							: ''}</title
					>
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
