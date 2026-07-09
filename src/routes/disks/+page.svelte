<script lang="ts">
	import { onMount } from 'svelte';
	import {
		api,
		ApiError,
		type ConfigView,
		type DiskEvent,
		type DiskPoint,
		type MetricsLatest,
		type Metric
	} from '$lib/api';
	import { poll, nowStore } from '$lib/stores';
	import { fmtRelative, fmtDateTime } from '$lib/format';
	import PageHeader from '$lib/components/PageHeader.svelte';
	import Card from '$lib/components/Card.svelte';
	import ConfidenceBadge from '$lib/components/ConfidenceBadge.svelte';
	import EvidenceDrawer from '$lib/components/EvidenceDrawer.svelte';
	import EmptyState from '$lib/components/EmptyState.svelte';
	import ActivityStrip from '$lib/charts/ActivityStrip.svelte';

	let config = $state<ConfigView | null>(null);
	let events = $state<DiskEvent[]>([]);
	let stripSeries = $state<Record<string, DiskPoint[]>>({}); // per-dev 24h activity
	let metrics = $state<MetricsLatest | null>(null);
	let selectedDev = $state(''); // '' = All
	let loading = $state(true);
	let error = $state<unknown>(null);

	function isUnreachable(e: unknown): boolean {
		return e instanceof ApiError && (e.message === 'not-configured' || e.message === 'unreachable');
	}

	// Rotational devices only — spin state is meaningful for HDDs, not NVMe.
	const rotational = $derived(config ? config.disks.filter((d) => d.rotational) : []);

	// Look up a single latest metric by collector + key (metrics are grouped for display).
	function findMetric(collector: string, key: string): Metric | undefined {
		if (!metrics) return undefined;
		for (const g of metrics.groups)
			for (const m of g.metrics) if (m.collector === collector && m.key === key) return m;
		return undefined;
	}

	// Per-HDD temperature + awake state from the storage collector. A sleeping disk
	// isn't probed (spinning it up just to read a sensor would defeat the point), so
	// its temp metric carries an explanatory txt instead of a number.
	interface DiskTemp {
		dev: string;
		label: string;
		awake: boolean | null;
		hasTemp: boolean;
		reading: string;
		tip: string;
	}
	const diskTemps = $derived.by<DiskTemp[]>(() =>
		rotational.map((d) => {
			const temp = findMetric('storage', `${d.dev}.temp`);
			const awakeM = findMetric('storage', `${d.dev}.awake`);
			const awake =
				awakeM?.num != null ? awakeM.num !== 0 : temp?.num != null ? true : null;
			const hasTemp = temp?.num != null;
			const reading = hasTemp ? `${Math.round(temp!.num as number)} °C` : (temp?.txt ?? 'no reading');
			return {
				dev: d.dev,
				label: d.label,
				awake,
				hasTemp,
				reading,
				tip: awakeM?.txt ?? temp?.txt ?? ''
			};
		})
	);

	// Relative timestamps refresh with nowStore (30s tick) as well as on reload.
	const rows = $derived.by(() => {
		void $nowStore;
		return events.map((e) => ({
			event: e,
			rel: fmtRelative(e.ts),
			when: fmtDateTime(e.ts)
		}));
	});

	const KIND: Record<DiskEvent['kind'], { label: string; icon: string }> = {
		spinup: { label: 'spin-up', icon: '▲' },
		stay_awake: { label: 'stay awake', icon: '◔' },
		read: { label: 'read', icon: '▸' },
		write: { label: 'write', icon: '◆' }
	};

	// Config once on mount (device list for the filter).
	onMount(() => {
		api
			.config()
			.then((c) => {
				config = c;
			})
			.catch((e) => {
				error = e;
			});
	});

	// Live events: poll every 30s. Re-created when the device filter changes so the
	// closure always queries the current dev.
	$effect(() => {
		const dev = selectedDev;
		loading = true;
		const stop = poll(
			() => api.diskEvents(0, 200, dev),
			30000,
			(r) => {
				events = r.events;
				loading = false;
				error = null;
			},
			(e) => {
				loading = false;
				error = e;
			}
		);
		return stop;
	});

	// Latest metric snapshot (drive temps / awake state). Dev-independent slow poll;
	// swallow its errors so a metrics hiccup never clobbers the events view.
	$effect(() => {
		return poll(
			() => api.metricsLatest(),
			30000,
			(r) => {
				metrics = r;
			},
			() => {}
		);
	});

	// Last-24h activity strip for EVERY rotational disk (small multiples), so all
	// disks are visible at once — independent of the events filter below.
	$effect(() => {
		const devs = rotational.map((d) => d.dev);
		if (devs.length === 0) return;
		let cancelled = false;
		const now = Math.floor(Date.now() / 1000);
		Promise.all(
			devs.map((dev) =>
				api
					.diskSeries(dev, now - 86400, now)
					.then((r) => [dev, r.points] as const)
					.catch(() => [dev, [] as DiskPoint[]] as const)
			)
		).then((pairs) => {
			if (!cancelled) stripSeries = Object.fromEntries(pairs);
		});
		return () => {
			cancelled = true;
		};
	});
</script>

<PageHeader
	title="Disk wake events"
	subtitle="what spun a disk up, with evidence and confidence"
/>

{#if error && isUnreachable(error)}
	<EmptyState
		title="Agent not reachable"
		message="The a3watch agent isn't configured or can't be reached over the tunnel. This is expected when viewing the dashboard without a live connection — disk wake events and activity will appear here once the agent is online."
	/>
{:else}
	<div class="filter" role="group" aria-label="Filter by device">
		<button
			type="button"
			class="filter-btn"
			class:active={selectedDev === ''}
			aria-pressed={selectedDev === ''}
			onclick={() => (selectedDev = '')}
		>
			All
		</button>
		{#each rotational as d (d.dev)}
			<button
				type="button"
				class="filter-btn"
				class:active={selectedDev === d.dev}
				aria-pressed={selectedDev === d.dev}
				title={d.label || d.role}
				onclick={() => (selectedDev = d.dev)}
			>
				{d.dev}
			</button>
		{/each}
	</div>

	<p class="note muted">
		Attribution can be uncertain. mergerfs pools, SMART polling, and kernel writeback
		(kworker / flush / jbd2 / md) can obscure exactly which process woke a disk — so each event
		carries its own confidence and evidence rather than a false certainty.
	</p>

	<div class="strip-card">
		<Card title="Disk activity — last 24 h (all disks)">
			{#if rotational.length === 0}
				<p class="muted">No rotational disks to show.</p>
			{:else}
				<div class="strips">
					{#each rotational as d (d.dev)}
						<div class="strip-row">
							<div class="strip-label">
								<span class="dev tabular">{d.dev}</span>
								{#if d.label}<span class="lbl muted">{d.label}</span>{/if}
							</div>
							<div class="strip-viz"><ActivityStrip points={stripSeries[d.dev] ?? []} /></div>
						</div>
					{/each}
				</div>
			{/if}
		</Card>
	</div>

	{#if rotational.length > 0}
		<div class="temp-card">
			<Card title="Drive temperature">
				{#if !metrics}
					<p class="muted loading-line">Loading…</p>
				{:else}
					<ul class="temps">
						{#each diskTemps as dt (dt.dev)}
							<li class="temp-row">
								<span
									class="state"
									class:awake={dt.awake === true}
									class:asleep={dt.awake === false}
									aria-hidden="true">{dt.awake === true ? '⟳' : dt.awake === false ? '☾' : '·'}</span
								>
								<span class="dev tabular">{dt.dev}</span>
								{#if dt.label}<span class="lbl muted">{dt.label}</span>{/if}
								<span class="reading tabular" class:muted={!dt.hasTemp} title={dt.tip}
									>{dt.reading}</span
								>
							</li>
						{/each}
					</ul>
				{/if}
			</Card>
		</div>
	{/if}

	{#if error && !isUnreachable(error)}
		<p class="err">Couldn't load events: {error instanceof Error ? error.message : 'unknown error'}</p>
	{/if}

	{#if loading && events.length === 0}
		<p class="muted loading-line">Loading events…</p>
	{:else if events.length === 0}
		<EmptyState
			title="No wake events"
			message={selectedDev
				? `No recorded wake events for ${selectedDev} yet.`
				: 'No recorded disk wake events yet. Disks that stay asleep produce nothing here — which is the goal.'}
		/>
	{:else}
		<ul class="timeline">
			{#each rows as { event: e, rel, when } (e.id)}
				<li class="event card">
					<div class="event-head">
						<span class="kind" data-kind={e.kind}>
							<span class="kind-icon" aria-hidden="true">{KIND[e.kind].icon}</span>{KIND[e.kind]
								.label}
						</span>
						<span class="dev tabular">{e.dev}</span>
						<ConfidenceBadge confidence={e.confidence} />
						<span class="when muted tabular">{rel} · {when}</span>
					</div>

					<div class="cause">
						<span class="cause-primary">{e.primary_cause}</span>
						{#if e.cause_kind}<span class="cause-kind muted">{e.cause_kind}</span>{/if}
					</div>

					<EvidenceDrawer event={e} />
				</li>
			{/each}
		</ul>
	{/if}
{/if}

<style>
	.filter {
		display: flex;
		flex-wrap: wrap;
		gap: 4px;
		background: var(--surface-2);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		padding: 3px;
		width: fit-content;
		max-width: 100%;
		margin-bottom: 12px;
	}
	.filter-btn {
		border: none;
		background: transparent;
		color: var(--text-secondary);
		padding: 4px 12px;
		border-radius: var(--radius-sm);
		font-weight: 500;
		line-height: 1.2;
		font-variant-numeric: tabular-nums;
	}
	.filter-btn:hover {
		color: var(--text-primary);
	}
	.filter-btn.active {
		background: var(--surface-1);
		color: var(--series-1);
		font-weight: 700;
	}

	.note {
		font-size: 13px;
		line-height: 1.5;
		max-width: 72ch;
		margin: 0 0 var(--gap);
	}

	.strip-card {
		margin-bottom: var(--gap);
	}
	.strips {
		display: flex;
		flex-direction: column;
		gap: 12px;
	}
	.strip-row {
		display: grid;
		grid-template-columns: 150px 1fr;
		gap: 12px;
		align-items: center;
		min-width: 0;
	}
	.strip-label {
		display: flex;
		flex-direction: column;
		gap: 2px;
		min-width: 0;
	}
	.strip-label .lbl {
		font-size: 11px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.strip-viz {
		min-width: 0;
	}
	@media (max-width: 520px) {
		.strip-row {
			grid-template-columns: 1fr;
			gap: 4px;
		}
	}

	.temp-card {
		margin-bottom: var(--gap);
	}
	.temps {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 8px;
	}
	.temp-row {
		display: flex;
		align-items: baseline;
		gap: 10px;
		min-width: 0;
	}
	.temp-row .state {
		font-size: 12px;
		line-height: 1;
		align-self: center;
		color: var(--text-muted);
		flex-shrink: 0;
	}
	.temp-row .state.asleep {
		color: var(--good);
	}
	.temp-row .state.awake {
		color: var(--warning);
	}
	.temp-row .lbl {
		font-size: 12px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		min-width: 0;
	}
	.temp-row .reading {
		margin-left: auto;
		font-size: 13px;
		font-weight: 600;
		white-space: nowrap;
		flex-shrink: 0;
	}

	.err {
		color: var(--serious);
		font-size: 13px;
		margin: 0 0 12px;
	}
	.loading-line {
		font-size: 13px;
		padding: 8px 0;
	}

	.timeline {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 10px;
	}
	.event {
		display: flex;
		flex-direction: column;
		gap: 10px;
	}
	.event-head {
		display: flex;
		align-items: center;
		flex-wrap: wrap;
		gap: 10px;
	}
	.kind {
		display: inline-flex;
		align-items: center;
		gap: 5px;
		padding: 2px 8px;
		border-radius: var(--radius-sm);
		background: var(--surface-2);
		border: 1px solid var(--border);
		color: var(--text-secondary);
		font-size: 12px;
		font-weight: 600;
		white-space: nowrap;
	}
	.kind-icon {
		font-size: 10px;
		line-height: 1;
	}
	.dev {
		font-family: ui-monospace, 'SFMono-Regular', Menlo, monospace;
		font-size: 13px;
		font-weight: 600;
		color: var(--text-primary);
	}
	.when {
		margin-left: auto;
		font-size: 12px;
		white-space: nowrap;
	}
	.cause {
		display: flex;
		align-items: baseline;
		flex-wrap: wrap;
		gap: 8px;
	}
	.cause-primary {
		font-size: 14px;
		font-weight: 600;
		color: var(--text-primary);
	}
	.cause-kind {
		font-size: 12px;
		font-family: ui-monospace, 'SFMono-Regular', Menlo, monospace;
	}

	@media (max-width: 520px) {
		.when {
			margin-left: 0;
			flex-basis: 100%;
		}
	}
</style>
