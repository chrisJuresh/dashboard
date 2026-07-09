<script lang="ts">
	import { fmtClock, fmtDateTime, seriesVar } from '$lib/format';

	interface SeriesDef {
		key: string;
		label: string;
		slot: number;
	}
	interface Props {
		points: Array<Record<string, number>>;
		series: SeriesDef[];
		height?: number;
		yLabel?: string;
		yMax?: number;
		format?: (n: number) => string;
	}
	let { points, series, height = 220, yLabel = '', yMax, format }: Props = $props();

	// container width (ResizeObserver-backed) drives a 1:1 viewBox so text stays crisp
	let cw = $state(0);
	const W = $derived(Math.max(320, cw || 800));
	const M = { top: 12, right: 14, bottom: 26, left: 48 };
	const innerW = $derived(W - M.left - M.right);
	const innerH = $derived(height - M.top - M.bottom);

	function extentTs(arr: Array<Record<string, number>>): [number, number] {
		let mn = Infinity;
		let mx = -Infinity;
		for (const p of arr) {
			const v = p.ts;
			if (v < mn) mn = v;
			if (v > mx) mx = v;
		}
		if (!isFinite(mn)) return [0, 1];
		return [mn, mx === mn ? mn + 1 : mx];
	}
	const tsExtent = $derived(extentTs(points));
	const tsMin = $derived(tsExtent[0]);
	const tsMax = $derived(tsExtent[1]);

	function niceMax(v: number): number {
		if (!isFinite(v) || v <= 0) return 1;
		const pow = Math.pow(10, Math.floor(Math.log10(v)));
		const n = v / pow;
		const step = n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10;
		return step * pow;
	}
	const dataMax = $derived.by(() => {
		let m = 0;
		for (const p of points) {
			for (const s of series) {
				const v = p[s.key];
				if (Number.isFinite(v) && v > m) m = v;
			}
		}
		return m;
	});
	const yMaxVal = $derived(yMax ?? niceMax(dataMax));

	function xScale(ts: number): number {
		if (tsMax === tsMin) return M.left + innerW / 2;
		return M.left + ((ts - tsMin) / (tsMax - tsMin)) * innerW;
	}
	function yScale(v: number): number {
		const mx = yMaxVal || 1;
		return M.top + innerH - (v / mx) * innerH;
	}

	const yTicks = $derived.by(() => {
		const mx = yMaxVal || 1;
		const n = 4;
		const out: number[] = [];
		for (let i = 0; i <= n; i++) out.push((mx / n) * i);
		return out;
	});
	const xTicks = $derived.by(() => {
		if (!points.length) return [] as number[];
		const n = Math.max(2, Math.min(6, Math.floor(W / 120)));
		const out: number[] = [];
		for (let i = 0; i <= n; i++) out.push(tsMin + ((tsMax - tsMin) / n) * i);
		return out;
	});

	function valAt(p: Record<string, number>, key: string): number | null {
		const v = p[key];
		return Number.isFinite(v) ? v : null;
	}
	function linePath(key: string): string {
		let d = '';
		let pen = false;
		for (const p of points) {
			const v = valAt(p, key);
			if (v == null) {
				pen = false;
				continue;
			}
			const x = xScale(p.ts).toFixed(1);
			const y = yScale(v).toFixed(1);
			d += (pen ? 'L' : 'M') + x + ' ' + y + ' ';
			pen = true;
		}
		return d.trim();
	}

	const defFmt = (n: number) =>
		Number.isFinite(n) ? (Math.abs(n) >= 100 ? n.toFixed(0) : n.toFixed(1)) : '—';
	function fmtVal(n: number): string {
		return format ? format(n) : defFmt(n);
	}

	// crosshair + tooltip
	let svgEl = $state<SVGSVGElement | undefined>(undefined);
	let hoverIdx = $state<number | null>(null);
	function onMove(e: PointerEvent) {
		if (!svgEl || !points.length) return;
		const rect = svgEl.getBoundingClientRect();
		const mx = (e.clientX - rect.left) * (W / (rect.width || W));
		let best = -1;
		let bd = Infinity;
		for (let i = 0; i < points.length; i++) {
			const d = Math.abs(xScale(points[i].ts) - mx);
			if (d < bd) {
				bd = d;
				best = i;
			}
		}
		hoverIdx = best >= 0 ? best : null;
	}
	function onLeave() {
		hoverIdx = null;
	}
	const hp = $derived(hoverIdx != null ? points[hoverIdx] : null);
	const hx = $derived(hp ? xScale(hp.ts) : 0);
	const ttLeft = $derived(hp ? Math.min(Math.max(hx + 8, 4), Math.max(4, W - 170)) : 0);
</script>

<div class="chart" bind:clientWidth={cw}>
	{#if series.length}
		<div class="legend">
			{#each series as s (s.key)}
				<span class="leg-item">
					<span class="sw" style="background:{seriesVar(s.slot)}"></span>
					<span class="secondary">{s.label}</span>
				</span>
			{/each}
		</div>
	{/if}

	{#if !points.length}
		<div class="empty muted" style="height:{height}px">no data</div>
	{:else}
		<svg
			bind:this={svgEl}
			viewBox="0 0 {W} {height}"
			width={W}
			height={height}
			role="img"
			onpointermove={onMove}
			onpointerleave={onLeave}
		>
			{#each yTicks as t, i (i)}
				<line
					x1={M.left}
					x2={W - M.right}
					y1={yScale(t)}
					y2={yScale(t)}
					style="stroke:var(--grid)"
					stroke-width="1"
				/>
				<text
					x={M.left - 6}
					y={yScale(t) + 3}
					text-anchor="end"
					font-size="11"
					class="tabular"
					style="fill:var(--text-muted)">{fmtVal(t)}</text
				>
			{/each}

			<line
				x1={M.left}
				x2={M.left}
				y1={M.top}
				y2={M.top + innerH}
				style="stroke:var(--axis)"
				stroke-width="1"
			/>
			<line
				x1={M.left}
				x2={W - M.right}
				y1={M.top + innerH}
				y2={M.top + innerH}
				style="stroke:var(--axis)"
				stroke-width="1"
			/>

			{#each xTicks as t, i (i)}
				<text
					x={xScale(t)}
					y={height - 8}
					text-anchor="middle"
					font-size="11"
					class="tabular"
					style="fill:var(--text-muted)">{fmtClock(t)}</text
				>
			{/each}

			{#if yLabel}
				<text
					transform="rotate(-90)"
					x={-(M.top + innerH / 2)}
					y={12}
					text-anchor="middle"
					font-size="11"
					style="fill:var(--text-muted)">{yLabel}</text
				>
			{/if}

			{#each series as s (s.key)}
				<path
					d={linePath(s.key)}
					fill="none"
					style="stroke:{seriesVar(s.slot)}"
					stroke-width="2"
					stroke-linejoin="round"
					stroke-linecap="round"
				/>
			{/each}

			{#if hp}
				<line
					x1={hx}
					x2={hx}
					y1={M.top}
					y2={M.top + innerH}
					style="stroke:var(--axis)"
					stroke-width="1"
					stroke-dasharray="3 3"
				/>
				{#each series as s (s.key)}
					{@const v = valAt(hp, s.key)}
					{#if v != null}
						<circle cx={hx} cy={yScale(v)} r="3" style="fill:{seriesVar(s.slot)}" />
					{/if}
				{/each}
			{/if}
		</svg>

		{#if hp}
			<div class="tt" style="left:{ttLeft}px; top:{M.top}px">
				<div class="tt-time tabular">{fmtDateTime(hp.ts)}</div>
				{#each series as s (s.key)}
					{@const v = valAt(hp, s.key)}
					{#if v != null}
						<div class="tt-row">
							<span class="sw" style="background:{seriesVar(s.slot)}"></span>
							<span class="tt-lbl secondary">{s.label}</span>
							<span class="tt-val tabular">{fmtVal(v)}</span>
						</div>
					{/if}
				{/each}
			</div>
		{/if}
	{/if}
</div>

<style>
	.chart {
		position: relative;
		width: 100%;
		max-width: 100%;
		overflow: hidden;
	}
	svg {
		display: block;
		width: 100%;
		height: auto;
	}
	.legend {
		display: flex;
		flex-wrap: wrap;
		gap: 6px 14px;
		margin-bottom: 6px;
		font-size: 12px;
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
		flex: none;
	}
	.empty {
		display: flex;
		align-items: center;
		justify-content: center;
		font-size: 13px;
	}
	.tt {
		position: absolute;
		pointer-events: none;
		background: var(--surface-2);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		padding: 6px 8px;
		font-size: 12px;
		min-width: 120px;
		z-index: 2;
		box-shadow: 0 2px 8px rgba(0, 0, 0, 0.25);
	}
	.tt-time {
		color: var(--text-primary);
		font-weight: 600;
		margin-bottom: 4px;
	}
	.tt-row {
		display: flex;
		align-items: center;
		gap: 6px;
	}
	.tt-lbl {
		flex: 1;
	}
	.tt-val {
		color: var(--text-primary);
		font-weight: 600;
	}
</style>
