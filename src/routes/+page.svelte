<script lang="ts">
	import { api, ApiError, type Status, type PowerState, type MetricsLatest, type Metric } from '$lib/api';
	import { poll, nowStore } from '$lib/stores';
	import { fmtWatts, fmtGbp, fmtPct, fmtDuration, fmtRelative, diskStateStatus } from '$lib/format';
	import PageHeader from '$lib/components/PageHeader.svelte';
	import Card from '$lib/components/Card.svelte';
	import StatTile from '$lib/components/StatTile.svelte';
	import Badge from '$lib/components/Badge.svelte';
	import EmptyState from '$lib/components/EmptyState.svelte';

	type TileStatus = 'good' | 'warning' | 'serious' | 'critical' | 'muted';
	interface Tile {
		label: string;
		value: string;
		sub?: string;
		status?: TileStatus;
		tip?: string;
	}

	let status = $state<Status | null>(null);
	let metrics = $state<MetricsLatest | null>(null);
	let error = $state<string | null>(null);

	// Live view: poll the agent snapshot at a slow, near-zero-cost cadence.
	$effect(() => {
		return poll(
			() => api.status(),
			20000,
			(s) => {
				status = s;
				error = null;
			},
			(e) => {
				error = e instanceof ApiError ? e.message : 'unknown';
			}
		);
	});

	// Latest generic-metric snapshot (deep-idle availability etc.). Errors are
	// swallowed so a metrics hiccup never masks the main status view.
	$effect(() => {
		return poll(
			() => api.metricsLatest(),
			20000,
			(m) => {
				metrics = m;
			},
			() => {}
		);
	});

	// Look up a single latest metric by collector + key.
	function findMetric(collector: string, key: string): Metric | undefined {
		if (!metrics) return undefined;
		for (const g of metrics.groups)
			for (const m of g.metrics) if (m.collector === collector && m.key === key) return m;
		return undefined;
	}

	// KPI row — derived straight from the snapshot; recomputed on each poll.
	const tiles = $derived.by<Tile[] | null>(() => {
		const s = status;
		if (!s) return null;
		const rot = s.disks.filter((d) => d.rotational);
		const asleep = rot.filter(
			(d) => d.power_state === 'standby' || d.power_state === 'sleeping'
		);
		const topPkg = [...s.cpu.pkg_cstates].sort((a, b) => b.pct - a.pct)[0];
		// Corrected attribution: when the platform can't enter deep package idle at
		// all (BIOS/kernel exposes only a single deepest state), that's "unavailable",
		// not "blocked" — no process is at fault, so it reads neutral/muted.
		const deep = findMetric('cpu', 'cstates.deep_available');
		const deepTile: Tile =
			deep?.num === 0
				? {
						label: 'Deep idle',
						value: 'unavailable (BIOS)',
						sub: 'no deep package C‑states',
						status: 'muted',
						tip:
							deep.txt ||
							'The platform exposes only cpu.cstates.deepest — a BIOS/kernel setting, not a process. Deep package idle cannot be entered on this hardware.'
					}
				: {
						label: 'Deep idle',
						value: s.cpu.pkg_deep_ok ? 'reaching' : 'blocked',
						sub: topPkg ? `${topPkg.name} ${fmtPct(topPkg.pct)}` : 'package C‑states',
						status: s.cpu.pkg_deep_ok ? 'good' : 'serious'
					};
		return [
			{
				label: 'Package power',
				value: fmtWatts(s.cpu.pkg_w),
				sub: `core ${fmtWatts(s.cpu.core_w)} · busy ${fmtPct(s.cpu.busy_pct)}`,
				status: s.cpu.pkg_w < 6 ? 'good' : 'warning'
			},
			deepTile,
			{
				label: 'HDDs asleep',
				value: `${asleep.length} / ${rot.length}`,
				sub: rot.length ? 'rotational disks in standby' : 'no rotational disks',
				status:
					rot.length === 0 ? 'muted' : asleep.length >= rot.length / 2 ? 'good' : 'warning'
			},
			{
				label: 'Est. overhead',
				value: `${fmtGbp(s.overhead.gbp_year)}/yr`,
				sub: `budget ${fmtGbp(s.overhead.budget_gbp)}/yr`,
				status: s.overhead.within_budget ? 'good' : 'warning'
			}
		];
	});

	const subtitle = $derived.by(() => {
		if (!status) return undefined;
		void $nowStore; // refresh the relative timestamp on the nowStore tick
		const base = `Updated ${fmtRelative(status.ts)} · ${status.mode} mode`;
		return error ? `${base} · reconnecting…` : base;
	});

	const empty = $derived.by(() => {
		if (error === 'not-configured')
			return {
				title: 'Agent not connected',
				message:
					'Enter the a3watch API URL and token on the connect screen to see live data. This is expected when viewing on Vercel with no tunnel running.'
			};
		if (error === 'unreachable')
			return {
				title: 'Agent unreachable',
				message:
					'The a3watch agent isn’t responding over the tunnel. This is normal when viewing the dashboard on Vercel without an active tunnel.'
			};
		return { title: 'Couldn’t load status', message: error ?? 'Unknown error.' };
	});

	const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

	function diskType(dev: string, rotational: boolean): string {
		if (rotational) return 'HDD';
		return dev.startsWith('nvme') ? 'NVMe' : 'SSD';
	}

	function diskIcon(state: PowerState): string {
		if (state === 'standby' || state === 'sleeping') return '☾';
		if (state === 'active') return '⟳';
		return '·';
	}
</script>

<div class="overview">
	<PageHeader title="Overview" {subtitle} />

	{#if status}
		<div class="tiles">
			{#each tiles ?? [] as t (t.label)}
				<div class="card" title={t.tip}>
					<StatTile label={t.label} value={t.value} sub={t.sub} status={t.status} />
				</div>
			{/each}
		</div>

		<section class="block">
			<h2 class="section">Disks</h2>
			{#if status.disks.length === 0}
				<div class="card">
					<EmptyState title="No disks reported" message="The agent isn’t tracking any block devices yet." />
				</div>
			{:else}
				<div class="disks">
					{#each status.disks as d (d.dev)}
						<Card>
							<div class="disk">
								<div class="disk-top">
									<div class="ident">
										<span class="dev tabular">{d.dev}</span>
										<span class="type muted">{diskType(d.dev, d.rotational)}</span>
									</div>
									{#if d.rotational}
										<Badge
											status={diskStateStatus(d.power_state, true)}
											icon={diskIcon(d.power_state)}
											label={cap(d.power_state)}
										/>
									{:else}
										<span class="nosleep muted">no sleep state</span>
									{/if}
								</div>

								<div class="role">{d.role}</div>
								<div class="model muted">{d.model || '—'}</div>
								{#if d.mount}<div class="mount muted tabular">{d.mount}</div>{/if}

								<div class="metrics">
									<div class="metric">
										<span class="k muted">In state</span>
										<span class="v tabular">{fmtDuration(d.minutes_in_state)}</span>
									</div>
									<div class="metric" title="recent read operations">
										<span class="k muted">Reads</span>
										<span class="v tabular">{d.reads_recent.toLocaleString()}</span>
									</div>
									<div class="metric" title="recent write operations">
										<span class="k muted">Writes</span>
										<span class="v tabular">{d.writes_recent.toLocaleString()}</span>
									</div>
								</div>
							</div>
						</Card>
					{/each}
				</div>
			{/if}
		</section>

		<div class="activity-wrap">
			<Card title="Recent activity">
				<ul class="activity">
					<li>
						<a href="/disks">
							<span class="n tabular" class:hot={status.counts.open_disk_events > 0}
								>{status.counts.open_disk_events}</span
							>
							<span class="lbl">open disk events</span>
							<span class="go" aria-hidden="true">→</span>
						</a>
					</li>
					<li>
						<a href="/processes">
							<span class="n tabular" class:hot={status.counts.stray_procs > 0}
								>{status.counts.stray_procs}</span
							>
							<span class="lbl">stray processes</span>
							<span class="go" aria-hidden="true">→</span>
						</a>
					</li>
				</ul>
			</Card>
		</div>
	{:else if error}
		<div class="card">
			<EmptyState title={empty.title} message={empty.message} />
		</div>
	{:else}
		<div class="card loading muted">Loading live status…</div>
	{/if}
</div>

<style>
	.overview {
		display: flex;
		flex-direction: column;
		gap: 24px;
	}

	.tiles {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
		gap: var(--gap);
	}

	.section {
		font-size: 13px;
		font-weight: 650;
		letter-spacing: 0.02em;
		text-transform: uppercase;
		color: var(--text-secondary);
		margin: 0 0 12px;
	}

	.disks {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
		gap: var(--gap);
	}
	.disk {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}
	.disk-top {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
	}
	.ident {
		display: flex;
		align-items: baseline;
		gap: 8px;
		min-width: 0;
	}
	.dev {
		font-size: 16px;
		font-weight: 650;
	}
	.type {
		font-size: 11px;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}
	.role {
		font-size: 13px;
		color: var(--text-secondary);
		text-transform: capitalize;
	}
	.model {
		font-size: 12px;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.mount {
		font-size: 12px;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.nosleep {
		font-size: 12px;
		font-weight: 600;
	}

	.metrics {
		display: grid;
		grid-template-columns: repeat(3, 1fr);
		gap: 8px;
		margin-top: 6px;
		padding-top: 10px;
		border-top: 1px solid var(--border);
	}
	.metric {
		display: flex;
		flex-direction: column;
		gap: 2px;
		min-width: 0;
	}
	.metric .k {
		font-size: 11px;
		font-weight: 600;
	}
	.metric .v {
		font-size: 14px;
		font-weight: 600;
	}

	.activity-wrap {
		max-width: 520px;
	}
	.activity {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 2px;
	}
	.activity a {
		display: flex;
		align-items: center;
		gap: 12px;
		padding: 10px 8px;
		border-radius: var(--radius-sm);
		color: var(--text-primary);
	}
	.activity a:hover {
		background: var(--surface-2);
		text-decoration: none;
	}
	.activity .n {
		font-size: 20px;
		font-weight: 650;
		min-width: 2ch;
		color: var(--text-secondary);
	}
	.activity .n.hot {
		color: var(--warning);
	}
	.activity .lbl {
		flex: 1;
		color: var(--text-secondary);
	}
	.activity .go {
		color: var(--text-muted);
	}

	.loading {
		padding: 32px;
		text-align: center;
	}
</style>
