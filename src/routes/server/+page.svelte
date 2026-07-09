<script lang="ts">
	import { api, ApiError, type Metric, type MetricsLatest } from '$lib/api';
	import { poll } from '$lib/stores';
	import { fmtClock } from '$lib/format';
	import PageHeader from '$lib/components/PageHeader.svelte';
	import Card from '$lib/components/Card.svelte';
	import StatTile from '$lib/components/StatTile.svelte';
	import EmptyState from '$lib/components/EmptyState.svelte';

	type TileStatus = 'good' | 'warning' | 'serious' | 'critical' | 'muted';
	type Tile = { label: string; value: string; sub?: string; status?: TileStatus };

	// Groups pinned to the front of the grid when present; everything else keeps
	// the order the agent sent it in (Array.sort is stable in modern engines).
	const PINNED = ['Power & cost', 'Thermal'];

	let latest = $state<MetricsLatest | null>(null);
	let loading = $state(true);
	let errKind = $state<'none' | 'unreachable' | 'other'>('none');
	let errMsg = $state('');

	function onData(v: MetricsLatest) {
		latest = v;
		errKind = 'none';
		errMsg = '';
		loading = false;
	}
	function onError(e: unknown) {
		loading = false;
		if (e instanceof ApiError && e.message === 'unreachable') {
			errKind = 'unreachable';
		} else {
			errKind = 'other';
			errMsg = e instanceof Error ? e.message : String(e);
		}
	}

	// Fetch immediately, then every 20s while the tab is visible.
	$effect(() => poll(() => api.metricsLatest(), 20000, onData, onError));

	// ---- helpers ------------------------------------------------------------
	function fmtNum(n: number): string {
		if (!Number.isFinite(n)) return '—';
		if (Number.isInteger(n)) return String(n);
		const abs = Math.abs(n);
		if (abs !== 0 && abs < 0.01) return n.toPrecision(2);
		return n.toFixed(abs < 1 ? 3 : abs < 100 ? 2 : 1);
	}

	/** Display value for a metric row: number + unit, or the text value. */
	function metricValue(m: Metric): { num: string; unit: string } | { text: string } {
		if (m.num != null) return { num: fmtNum(m.num), unit: m.unit ?? '' };
		return { text: m.txt ?? '—' };
	}

	// Flat view of every metric, for headline lookups.
	const all = $derived<Metric[]>((latest?.groups ?? []).flatMap((g) => g.metrics));

	const groups = $derived.by(() => {
		const gs = [...(latest?.groups ?? [])];
		const rank = (name: string) => {
			const i = PINNED.indexOf(name);
			return i === -1 ? PINNED.length : i;
		};
		return gs.sort((a, b) => rank(a.group) - rank(b.group));
	});

	// ---- headline metric lookups (heuristic, tolerant of naming) ------------
	const price = $derived(
		all.find((m) => m.num != null && (m.unit === '£/kWh' || /price/i.test(m.key)))
	);
	const priceSource = $derived(
		all.find(
			(m) => m.txt != null && /source|tariff|provider/i.test(m.key) && /price|electric|energy|tariff/i.test(m.key)
		)?.txt ?? price?.collector
	);
	const budgetW = $derived(
		all.find((m) => m.num != null && m.unit === 'W' && /budget|ceiling|cap|limit/i.test(m.key))
	);
	const cpuModel = $derived(
		all.find((m) => m.txt != null && /model/i.test(m.key) && /cpu/i.test(m.collector)) ??
			all.find((m) => m.txt != null && /cpu/i.test(m.collector) && /model|name/i.test(m.key))
	);
	const memPct = $derived(
		all.find(
			(m) => m.num != null && m.unit === '%' && /mem/i.test(m.collector + m.key) && /(used|usage|pct|percent)/i.test(m.key)
		) ?? all.find((m) => m.num != null && m.unit === '%' && /mem/i.test(m.collector + m.key))
	);
	const hottest = $derived.by(() => {
		let best: Metric | undefined;
		for (const m of all) {
			if (m.num == null) continue;
			if (m.unit !== '°C' && m.unit !== 'C' && !/temp/i.test(m.key)) continue;
			if (!best || (best.num ?? -Infinity) < m.num) best = m;
		}
		return best;
	});

	function tempKind(m: Metric): string {
		const s = `${m.collector}.${m.key}`;
		if (/disk|nvme|hdd|ssd|drive|sd[a-z]/i.test(s)) return 'disk';
		if (/cpu|core|pkg|package/i.test(s)) return 'CPU';
		return 'component';
	}
	function tempStatus(c: number): TileStatus {
		if (c >= 80) return 'critical';
		if (c >= 70) return 'serious';
		if (c >= 60) return 'warning';
		return 'good';
	}
	function memStatus(p: number): TileStatus {
		if (p >= 90) return 'critical';
		if (p >= 75) return 'warning';
		return 'good';
	}

	const tiles = $derived.by<Tile[]>(() => {
		const out: Tile[] = [];
		if (price?.num != null) {
			out.push({
				label: 'Electricity price',
				value: `£${price.num.toFixed(price.num < 1 ? 3 : 2)}/kWh`,
				sub: priceSource ? `source: ${priceSource}` : undefined
			});
		}
		if (budgetW?.num != null) {
			out.push({ label: 'Budget ceiling', value: `${fmtNum(budgetW.num)} W` });
		}
		if (cpuModel?.txt) {
			out.push({ label: 'CPU', value: cpuModel.txt });
		}
		if (memPct?.num != null) {
			out.push({
				label: 'Memory used',
				value: `${memPct.num.toFixed(0)}%`,
				status: memStatus(memPct.num)
			});
		}
		if (hottest?.num != null) {
			out.push({
				label: `Hottest ${tempKind(hottest)}`,
				value: `${hottest.num.toFixed(0)} ${hottest.unit || '°C'}`,
				sub: `${hottest.collector}.${hottest.key}`,
				status: tempStatus(hottest.num)
			});
		}
		return out;
	});

	// Only surface the empty/error state when there's nothing to show yet; a
	// transient poll failure after a good load keeps the last snapshot visible.
	const showEmpty = $derived(errKind !== 'none' && !latest);
	const noMetrics = $derived(!!latest && groups.length === 0);
</script>

<PageHeader title="Server" subtitle="Live host metrics from the agent's collectors">
	{#snippet actions()}
		{#if latest}<span class="asof muted tabular">as of {fmtClock(latest.ts)}</span>{/if}
	{/snippet}
</PageHeader>

{#if showEmpty}
	<Card>
		{#if errKind === 'unreachable'}
			<EmptyState
				title="Agent unreachable"
				message="The a3watch agent could not be reached. Server metrics will appear here once the agent is back online."
			/>
		{:else}
			<EmptyState
				title="Could not load metrics"
				message={errMsg || 'An unexpected error occurred while fetching server metrics.'}
			/>
		{/if}
	</Card>
{:else if loading && !latest}
	<Card>
		<p class="muted">Loading metrics…</p>
	</Card>
{:else if noMetrics}
	<Card>
		<EmptyState
			title="No metrics reported"
			message="The agent is reachable but no collector metrics were returned."
		/>
	</Card>
{:else if latest}
	<div class="stack">
		{#if tiles.length}
			<Card>
				<div class="tiles">
					{#each tiles as t}
						<StatTile label={t.label} value={t.value} sub={t.sub} status={t.status} />
					{/each}
				</div>
			</Card>
		{/if}

		<div class="grid">
			{#each groups as g (g.group)}
				<Card title={g.group}>
					<ul class="metrics">
						{#each g.metrics as m (m.collector + '.' + m.key)}
							{@const v = metricValue(m)}
							<li class="metric">
								<span class="mkey muted">{m.collector}.{m.key}</span>
								<span class="leader" aria-hidden="true"></span>
								<span class="mval tabular">
									{#if 'num' in v}
										{v.num}{#if v.unit}<span class="unit muted"> {v.unit}</span>{/if}
									{:else}
										{v.text}
									{/if}
								</span>
							</li>
						{/each}
					</ul>
				</Card>
			{/each}
		</div>
	</div>
{/if}

<style>
	.asof {
		font-size: 12px;
	}
	.stack {
		display: flex;
		flex-direction: column;
		gap: var(--gap);
	}
	.tiles {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
		gap: var(--gap);
	}
	.grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
		gap: var(--gap);
	}
	.metrics {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 8px;
	}
	.metric {
		display: flex;
		align-items: flex-end;
		gap: 4px;
		min-width: 0;
		font-size: 13px;
	}
	.mkey {
		min-width: 0;
		overflow-wrap: anywhere;
	}
	.leader {
		flex: 1 1 12px;
		min-width: 12px;
		align-self: stretch;
		border-bottom: 1px dotted var(--border);
		transform: translateY(-4px);
	}
	.mval {
		flex-shrink: 1;
		min-width: 0;
		text-align: right;
		overflow-wrap: anywhere;
		color: var(--text-primary);
		font-weight: 550;
	}
	.unit {
		font-weight: 500;
	}
</style>
