<script lang="ts">
	import { onMount } from 'svelte';
	import { api, ApiError, type ConfigView, type DiskEvent, type DiskPoint } from '$lib/api';
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
	let diskPoints = $state<DiskPoint[]>([]);
	let selectedDev = $state(''); // '' = All
	let loading = $state(true);
	let error = $state<unknown>(null);

	function isUnreachable(e: unknown): boolean {
		return e instanceof ApiError && (e.message === 'not-configured' || e.message === 'unreachable');
	}

	// Rotational devices only — spin state is meaningful for HDDs, not NVMe.
	const rotational = $derived(config ? config.disks.filter((d) => d.rotational) : []);
	// The activity strip needs a concrete device: the selected one, or the first HDD.
	const stripDev = $derived(selectedDev || rotational[0]?.dev || '');
	const stripMeta = $derived(rotational.find((d) => d.dev === stripDev) ?? null);

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

	// Last-24h activity strip for the strip device. Time-series — fetched on
	// device change, not on a fast poll.
	$effect(() => {
		const dev = stripDev;
		if (!dev) {
			diskPoints = [];
			return;
		}
		let cancelled = false;
		const now = Math.floor(Date.now() / 1000);
		api
			.diskSeries(dev, now - 86400, now)
			.then((r) => {
				if (!cancelled) diskPoints = r.points;
			})
			.catch((e) => {
				if (!cancelled) error = e;
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
		<Card title={`Last 24 hours${stripMeta ? ` · ${stripDev}${stripMeta.label ? ` (${stripMeta.label})` : ''}` : ''}`}>
			{#if !stripDev}
				<p class="muted">No rotational disks to show.</p>
			{:else}
				<ActivityStrip points={diskPoints} />
			{/if}
		</Card>
	</div>

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
