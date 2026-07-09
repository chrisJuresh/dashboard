<script lang="ts">
	import { onMount } from 'svelte';
	import { api, ApiError, type ProcInfo } from '$lib/api';
	import { poll } from '$lib/stores';
	import { fmtBytes, fmtPct } from '$lib/format';
	import PageHeader from '$lib/components/PageHeader.svelte';
	import Card from '$lib/components/Card.svelte';
	import Badge from '$lib/components/Badge.svelte';
	import EmptyState from '$lib/components/EmptyState.svelte';

	type BadgeStatus = 'good' | 'warning' | 'serious' | 'critical' | 'muted';

	// ---- state ---------------------------------------------------------------
	let procs = $state<ProcInfo[] | null>(null);
	let loaded = $state(false);
	let errorKind = $state<'none' | 'unreachable' | 'other'>('none');
	let errorMsg = $state('');

	// ---- flag metadata (fixed) ----------------------------------------------
	const FLAG_META: Record<string, { status: BadgeStatus; label: string; icon?: string }> = {
		crashloop: { status: 'critical', label: 'crash-loop', icon: '↻' },
		stray: { status: 'serious', label: 'stray', icon: '⚠' },
		poller: { status: 'warning', label: 'poller' }
	};
	function flagMeta(flag: string): { status: BadgeStatus; label: string; icon?: string } {
		return FLAG_META[flag] ?? { status: 'muted', label: flag };
	}

	// ---- derived: flagged first, then by cpu% desc ---------------------------
	const sorted = $derived(
		procs
			? [...procs].sort((a, b) => {
					const af = a.flags.length > 0 ? 1 : 0;
					const bf = b.flags.length > 0 ? 1 : 0;
					if (af !== bf) return bf - af;
					return b.cpu_pct - a.cpu_pct;
				})
			: []
	);
	const flaggedCount = $derived(sorted.filter((p) => p.flags.length > 0).length);

	// ---- live polling (20s) --------------------------------------------------
	onMount(() =>
		poll(
			() => api.processes(),
			20_000,
			(v) => {
				procs = v.procs;
				loaded = true;
				errorKind = 'none';
				errorMsg = '';
			},
			(e) => {
				loaded = true;
				if (e instanceof ApiError && (e.message === 'not-configured' || e.message === 'unreachable')) {
					errorKind = 'unreachable';
				} else {
					errorKind = 'other';
					errorMsg = e instanceof Error ? e.message : String(e);
				}
			}
		)
	);

	// Show the unreachable/error empty state only when we have no data to show.
	const showError = $derived(loaded && procs === null && errorKind !== 'none');
</script>

<PageHeader title="Processes" subtitle="stray, crash-looping, and heavy processes" />

{#if showError}
	{#if errorKind === 'unreachable'}
		<EmptyState
			title="Agent not reachable"
			message="No connection to the a3watch agent. This is expected when the dashboard is open without the cloudflared tunnel up (for example on Vercel). Start the agent and open the tunnel to see live processes."
		/>
	{:else}
		<EmptyState title="Could not load processes" message={errorMsg || 'The agent returned an error.'} />
	{/if}
{:else if !loaded}
	<Card>
		<p class="muted status-line">Loading processes…</p>
	</Card>
{:else if sorted.length === 0}
	<EmptyState
		title="Nothing flagged"
		message="No stray, crash-looping, or heavy processes right now. The agent surfaces processes here only when they draw attention."
	/>
{:else}
	<Card>
		<p class="summary muted tabular">
			{sorted.length} process{sorted.length === 1 ? '' : 'es'} · {flaggedCount} flagged
		</p>
		<div class="table-wrap">
			<table class="proc-table tabular">
				<thead>
					<tr>
						<th class="col-comm">Process</th>
						<th class="col-pid num">PID</th>
						<th class="col-cgroup">cgroup</th>
						<th class="col-cpu num">CPU</th>
						<th class="col-io num">Read</th>
						<th class="col-io num">Write</th>
						<th class="col-flags">Flags</th>
					</tr>
				</thead>
				<tbody>
					{#each sorted as p (p.pid)}
						<tr class:flagged={p.flags.length > 0}>
							<td class="col-comm">
								<span class="comm">{p.comm}</span>
								{#if p.note}<span class="note muted">{p.note}</span>{/if}
							</td>
							<td class="col-pid num">{p.pid}</td>
							<td class="col-cgroup">
								<span class="cgroup" title={p.cgroup}>{p.cgroup || '—'}</span>
							</td>
							<td class="col-cpu num">{fmtPct(p.cpu_pct)}</td>
							<td class="col-io num">{fmtBytes(p.read_bytes_d)}</td>
							<td class="col-io num">{fmtBytes(p.write_bytes_d)}</td>
							<td class="col-flags">
								{#if p.flags.length > 0}
									<span class="flags">
										{#each p.flags as f (f)}
											{@const m = flagMeta(f)}
											<Badge status={m.status} label={m.label} icon={m.icon} />
										{/each}
									</span>
								{:else}
									<span class="muted">—</span>
								{/if}
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	</Card>

	<footer class="legend">
		<h2 class="legend-title">What the flags mean</h2>
		<dl>
			<div class="legend-row">
				<dt><Badge status="critical" label="crash-loop" icon="↻" /></dt>
				<dd class="muted">
					A container or service is restarting repeatedly — short-lived respawns in a tight window.
					Constant restarts churn CPU and can keep disks awake.
				</dd>
			</div>
			<div class="legend-row">
				<dt><Badge status="serious" label="stray" icon="⚠" /></dt>
				<dd class="muted">
					High CPU with no owning service cgroup, or an orphaned process reparented to PID 1 that
					isn't a known service — likely something that escaped its unit.
				</dd>
			</div>
			<div class="legend-row">
				<dt><Badge status="warning" label="poller" /></dt>
				<dd class="muted">
					Wakes up on a fixed cadence (busy-polling). Frequent wakeups stop the CPU package from
					reaching deep C-states and can prevent HDDs from spinning down.
				</dd>
			</div>
		</dl>
	</footer>
{/if}

<style>
	.status-line {
		margin: 0;
		font-size: 13px;
	}
	.summary {
		margin: 0 0 12px;
		font-size: 12px;
	}

	.table-wrap {
		overflow-x: auto;
	}
	.proc-table {
		width: 100%;
		border-collapse: collapse;
		font-size: 13px;
	}
	.proc-table th,
	.proc-table td {
		text-align: left;
		padding: 8px 12px;
		border-bottom: 1px solid var(--border);
		vertical-align: top;
		white-space: nowrap;
	}
	.proc-table th {
		font-size: 11px;
		font-weight: 650;
		letter-spacing: 0.02em;
		text-transform: uppercase;
		color: var(--text-secondary);
		border-bottom: 1px solid var(--border);
		position: sticky;
		top: 0;
		background: var(--surface-1);
	}
	.proc-table tbody tr:last-child td {
		border-bottom: none;
	}
	.proc-table tbody tr.flagged {
		background: var(--surface-2);
	}
	.num {
		text-align: right;
		font-variant-numeric: tabular-nums;
	}

	.col-comm {
		min-width: 0;
	}
	.comm {
		display: block;
		font-weight: 600;
		color: var(--text-primary);
	}
	.note {
		display: block;
		margin-top: 2px;
		font-size: 11px;
		line-height: 1.35;
		white-space: normal;
		max-width: 34ch;
	}

	.col-cgroup {
		max-width: 0;
		width: 30%;
	}
	.cgroup {
		display: block;
		max-width: 28ch;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		color: var(--text-secondary);
		font-size: 12px;
	}

	.flags {
		display: inline-flex;
		flex-wrap: wrap;
		gap: 6px 10px;
	}

	.legend {
		margin-top: var(--gap);
		padding: var(--gap);
		background: var(--surface-1);
		border: 1px solid var(--border);
		border-radius: var(--radius);
	}
	.legend-title {
		font-size: 13px;
		font-weight: 650;
		letter-spacing: 0.02em;
		text-transform: uppercase;
		color: var(--text-secondary);
		margin: 0 0 12px;
	}
	.legend dl {
		margin: 0;
		display: flex;
		flex-direction: column;
		gap: 12px;
	}
	.legend-row {
		display: grid;
		grid-template-columns: 130px 1fr;
		gap: 12px;
		align-items: start;
	}
	.legend dt {
		margin: 0;
	}
	.legend dd {
		margin: 0;
		font-size: 12px;
		line-height: 1.5;
	}
	@media (max-width: 520px) {
		.legend-row {
			grid-template-columns: 1fr;
			gap: 4px;
		}
	}
</style>
