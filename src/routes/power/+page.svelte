<script lang="ts">
	import { onMount } from 'svelte';
	import { api, ApiError } from '$lib/api';
	import type { PowerPoint, CStatePoint, PowerEvent, MetricsLatest, Metric } from '$lib/api';
	import { fmtWatts, fmtPct, fmtDateTime } from '$lib/format';
	import PageHeader from '$lib/components/PageHeader.svelte';
	import Card from '$lib/components/Card.svelte';
	import EmptyState from '$lib/components/EmptyState.svelte';
	import ConfidenceBadge from '$lib/components/ConfidenceBadge.svelte';
	import LineChart from '$lib/charts/LineChart.svelte';
	import StackedArea from '$lib/charts/StackedArea.svelte';
	import TimeRange from '$lib/charts/TimeRange.svelte';

	// ---- state ---------------------------------------------------------------
	let range = $state(21600); // 6h default
	let loading = $state(true);
	let unreachable = $state(false);
	let errMsg = $state('');

	let powerPoints = $state<PowerPoint[]>([]);
	let pkgStates = $state<string[]>([]);
	let pkgPoints = $state<CStatePoint[]>([]);
	let coreStates = $state<string[]>([]);
	let corePoints = $state<CStatePoint[]>([]);
	let events = $state<PowerEvent[]>([]);
	let metrics = $state<MetricsLatest | null>(null);

	// ---- series definitions --------------------------------------------------
	// Package + core watts in fixed slots 0 and 1 (blue, aqua).
	const powerSeriesDef = [
		{ key: 'pkg_w', label: 'Package', slot: 0 },
		{ key: 'core_w', label: 'Core', slot: 1 }
	];
	// C-state bands take slots in the order the agent reports them (stable).
	const pkgSeriesDef = $derived(pkgStates.map((s, i) => ({ key: s, label: s, slot: i })));
	const coreSeriesDef = $derived(coreStates.map((s, i) => ({ key: s, label: s, slot: i })));

	// LineChart wants Array<Record<string, number>>; PowerPoint is all-number.
	const powerChart = $derived(powerPoints as unknown as Array<Record<string, number>>);

	// ---- deep-idle availability ----------------------------------------------
	// deep_available (num 1/0) is the corrected signal: whether the platform can
	// enter deep *package* idle at all. On hardware that only exposes a single
	// deepest state, deep idle isn't "blocked" by a process — it simply isn't
	// available, and the honest reason travels on the pkg_cstate_stall events.
	function findMetric(collector: string, key: string): Metric | undefined {
		if (!metrics) return undefined;
		for (const g of metrics.groups)
			for (const m of g.metrics) if (m.collector === collector && m.key === key) return m;
		return undefined;
	}
	const deepMetric = $derived(findMetric('cpu', 'cstates.deep_available'));
	const deepAvailable = $derived<number | null>(deepMetric?.num ?? null); // 1, 0, or null
	const deepTxt = $derived(deepMetric?.txt ?? '');
	const stallEvents = $derived(events.filter((e) => e.kind === 'pkg_cstate_stall'));

	// ---- data load -----------------------------------------------------------
	async function load() {
		loading = true;
		unreachable = false;
		errMsg = '';
		const to = Math.floor(Date.now() / 1000);
		const from = to - range;
		const res: 'raw' | 'hour' = range >= 86400 ? 'hour' : 'raw';
		try {
			const [pw, pkg, core, ev, ml] = await Promise.all([
				api.powerSeries(from, to, res),
				api.cstateSeries(from, to, 'package', res),
				api.cstateSeries(from, to, 'core', res),
				api.powerEvents(0, 100),
				api.metricsLatest()
			]);
			powerPoints = pw.points;
			pkgStates = pkg.states;
			pkgPoints = pkg.points;
			coreStates = core.states;
			corePoints = core.points;
			events = ev.events;
			metrics = ml;
		} catch (e) {
			if (e instanceof ApiError && (e.message === 'not-configured' || e.message === 'unreachable')) {
				unreachable = true;
			} else {
				errMsg = e instanceof Error ? e.message : String(e);
			}
		} finally {
			loading = false;
		}
	}

	onMount(load);

	function onRange(seconds: number) {
		if (seconds === range) return;
		range = seconds;
		load(); // time-series refetch on range change (no fast poll)
	}

	// ---- helpers -------------------------------------------------------------
	function kindLabel(kind: PowerEvent['kind']): string {
		return kind === 'watt_rise' ? 'Watt rise' : 'Package C-state stall';
	}
</script>

<PageHeader title="Power & C-states" subtitle="CPU package/core draw and idle-state residency over time">
	{#snippet actions()}
		<TimeRange value={range} onChange={onRange} />
	{/snippet}
</PageHeader>

{#if unreachable}
	<EmptyState
		title="Agent not reachable"
		message="The a3watch agent isn't responding. This is expected on the public dashboard when no cloudflared tunnel is connected — power draw and C-state history appear once the agent is online."
	/>
{:else}
	<div class="stack">
		{#if errMsg}
			<Card>
				<p class="err">Couldn't load power data: {errMsg}</p>
			</Card>
		{/if}

		<Card title="Deep idle">
			{#if loading}
				<p class="loading muted">Loading…</p>
			{:else if deepAvailable === 0}
				<p class="deep deep-unavail">
					<span class="deep-dot" aria-hidden="true"></span>
					<span>
						Deep idle: <strong>UNAVAILABLE</strong> — platform exposes only
						<code>{'<cpu.cstates.deepest>'}</code> (a BIOS/kernel setting, not a process)
					</span>
				</p>
				{#if deepTxt}<p class="note muted">{deepTxt}</p>{/if}
				{#if stallEvents.length}
					<ul class="stalls">
						{#each stallEvents as ev (ev.id)}
							<li class="stall">
								<span class="ts muted tabular">{fmtDateTime(ev.ts)}</span>
								<span class="reason">{ev.detail || ev.primary_cause}</span>
							</li>
						{/each}
					</ul>
				{/if}
			{:else if deepAvailable === 1}
				<p class="deep deep-ok">
					<span class="deep-dot" aria-hidden="true"></span>
					<span>Deep idle: reachable — the package can enter deep C‑states.</span>
				</p>
			{:else}
				<p class="muted">Deep-idle availability not reported by the agent.</p>
			{/if}
		</Card>

		<Card title="CPU package & core power">
			{#if loading}
				<p class="loading muted">Loading…</p>
			{:else}
				<LineChart
					points={powerChart}
					series={powerSeriesDef}
					format={fmtWatts}
					yLabel="W"
					height={240}
				/>
			{/if}
		</Card>

		<div class="grid2">
			<Card title="Package C-state residency">
				{#if loading}
					<p class="loading muted">Loading…</p>
				{:else}
					<StackedArea
						points={pkgPoints}
						series={pkgSeriesDef}
						yMax={100}
						format={fmtPct}
						yLabel="%"
					/>
					<p class="note muted">
						<strong>PC6 / PC8 / PC10</strong> are the deep package idle states — the whole CPU package
						powering down between work. If they sit near 0%, the package can't sleep (something keeps
						it awake) and idle draw stays high.
					</p>
				{/if}
			</Card>

			<Card title="Core C-state residency">
				{#if loading}
					<p class="loading muted">Loading…</p>
				{:else}
					<StackedArea
						points={corePoints}
						series={coreSeriesDef}
						yMax={100}
						format={fmtPct}
						yLabel="%"
					/>
					<p class="note muted">
						Per-core idle residency (POLL / C1 / C6…). Cores reaching deep C-states is a prerequisite
						for the whole package sleeping.
					</p>
				{/if}
			</Card>
		</div>

		<Card title="Power events">
			{#if loading}
				<p class="loading muted">Loading…</p>
			{:else if !events.length}
				<p class="muted">No power events recorded.</p>
			{:else}
				<ul class="events">
					{#each events as ev (ev.id)}
						<li class="event">
							<div class="event-head">
								<span class="kind">{kindLabel(ev.kind)}</span>
								<ConfidenceBadge confidence={ev.confidence} />
								<span class="when muted tabular">{fmtDateTime(ev.ts)}</span>
							</div>
							{#if ev.primary_cause}<div class="cause">{ev.primary_cause}</div>{/if}
							{#if ev.detail}<div class="detail secondary">{ev.detail}</div>{/if}
						</li>
					{/each}
				</ul>
			{/if}
		</Card>
	</div>
{/if}

<style>
	.stack {
		display: flex;
		flex-direction: column;
		gap: var(--gap);
	}
	.grid2 {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: var(--gap);
	}
	@media (max-width: 800px) {
		.grid2 {
			grid-template-columns: 1fr;
		}
	}
	.loading {
		font-size: 13px;
		padding: 24px 0;
		text-align: center;
	}
	.note {
		margin: 10px 0 0;
		font-size: 12px;
		line-height: 1.5;
	}
	.err {
		margin: 0;
		color: var(--critical);
		font-size: 13px;
	}
	.deep {
		display: flex;
		align-items: baseline;
		gap: 8px;
		margin: 0;
		font-size: 14px;
		line-height: 1.5;
		color: var(--text-primary);
	}
	.deep code {
		font-family: ui-monospace, 'SFMono-Regular', Menlo, monospace;
		font-size: 12px;
		background: var(--surface-2);
		border: 1px solid var(--border);
		border-radius: 4px;
		padding: 1px 5px;
		white-space: nowrap;
	}
	.deep-dot {
		width: 8px;
		height: 8px;
		border-radius: 999px;
		flex-shrink: 0;
		align-self: center;
		background: var(--text-muted);
	}
	.deep-ok .deep-dot {
		background: var(--good);
	}
	.stalls {
		list-style: none;
		margin: 12px 0 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 8px;
	}
	.stall {
		display: flex;
		align-items: baseline;
		gap: 10px;
		flex-wrap: wrap;
		font-size: 12px;
	}
	.stall .ts {
		color: var(--text-muted);
		white-space: nowrap;
	}
	.stall .reason {
		color: var(--text-secondary);
		line-height: 1.5;
		min-width: 0;
	}
	.events {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
	}
	.event {
		padding: 12px 0;
		border-top: 1px solid var(--border);
	}
	.event:first-child {
		padding-top: 0;
		border-top: none;
	}
	.event-head {
		display: flex;
		align-items: center;
		gap: 10px;
		flex-wrap: wrap;
	}
	.kind {
		font-size: 12px;
		font-weight: 650;
		color: var(--text-primary);
		background: var(--surface-2);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		padding: 2px 8px;
	}
	.when {
		margin-left: auto;
		font-size: 12px;
	}
	.cause {
		margin-top: 6px;
		font-size: 13px;
		color: var(--text-primary);
	}
	.detail {
		margin-top: 2px;
		font-size: 12px;
		line-height: 1.5;
	}
</style>
