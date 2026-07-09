<script lang="ts">
	import { seriesVar } from '$lib/format';

	interface Props {
		values: number[];
		slot?: number;
		height?: number;
	}
	let { values, slot = 0, height = 28 }: Props = $props();

	let cw = $state(0);
	const W = $derived(Math.max(40, cw || 100));
	const PAD = 2;

	const extent = $derived.by(() => {
		let mn = Infinity;
		let mx = -Infinity;
		for (const v of values) {
			if (!Number.isFinite(v)) continue;
			if (v < mn) mn = v;
			if (v > mx) mx = v;
		}
		if (!isFinite(mn)) return { mn: 0, mx: 1 };
		return { mn, mx: mx === mn ? mn + 1 : mx };
	});

	function xAt(i: number): number {
		if (values.length <= 1) return W / 2;
		return PAD + (i / (values.length - 1)) * (W - 2 * PAD);
	}
	function yAt(v: number): number {
		const { mn, mx } = extent;
		return height - PAD - ((v - mn) / (mx - mn)) * (height - 2 * PAD);
	}

	const path = $derived.by(() => {
		let d = '';
		let pen = false;
		values.forEach((v, i) => {
			if (!Number.isFinite(v)) {
				pen = false;
				return;
			}
			d += (pen ? 'L' : 'M') + xAt(i).toFixed(1) + ' ' + yAt(v).toFixed(1) + ' ';
			pen = true;
		});
		return d.trim();
	});
</script>

<div class="spark" bind:clientWidth={cw}>
	{#if path}
		<svg viewBox="0 0 {W} {height}" width={W} {height} role="img" aria-hidden="true">
			<path
				d={path}
				fill="none"
				style="stroke:{seriesVar(slot)}"
				stroke-width="1.5"
				stroke-linejoin="round"
				stroke-linecap="round"
			/>
		</svg>
	{/if}
</div>

<style>
	.spark {
		width: 100%;
		max-width: 100%;
		overflow: hidden;
		line-height: 0;
	}
	svg {
		display: block;
		width: 100%;
		height: auto;
	}
</style>
