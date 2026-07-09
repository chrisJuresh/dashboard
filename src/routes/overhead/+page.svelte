<script lang="ts">
	import { api, ApiError, type Status } from '$lib/api';
	import { poll } from '$lib/stores';
	import { fmtGbp, fmtBytes } from '$lib/format';
	import PageHeader from '$lib/components/PageHeader.svelte';
	import Card from '$lib/components/Card.svelte';
	import StatTile from '$lib/components/StatTile.svelte';
	import EmptyState from '$lib/components/EmptyState.svelte';
	import LineChart from '$lib/charts/LineChart.svelte';

	type OverheadResp = Awaited<ReturnType<typeof api.overhead>>;

	const WEEK = 604800;

	let overhead = $state<OverheadResp | null>(null);
	let status = $state<Status | null>(null);
	let loading = $state(true);
	let errKind = $state<'none' | 'not-configured' | 'unreachable' | 'other'>('none');
	let errMsg = $state('');

	async function fetchAll() {
		const now = Math.floor(Date.now() / 1000);
		const [ov, st] = await Promise.all([api.overhead(now - WEEK, now), api.status()]);
		return { ov, st };
	}

	function onData(v: { ov: OverheadResp; st: Status }) {
		overhead = v.ov;
		status = v.st;
		errKind = 'none';
		errMsg = '';
		loading = false;
	}

	function onError(e: unknown) {
		loading = false;
		if (e instanceof ApiError && (e.message === 'not-configured' || e.message === 'unreachable')) {
			errKind = e.message;
		} else {
			errKind = 'other';
			errMsg = e instanceof Error ? e.message : String(e);
		}
	}

	$effect(() => poll(fetchAll, 20000, onData, onError));

	// Derived views over the fetched data.
	const current = $derived(overhead?.current ?? null);
	const budget = $derived(overhead?.budget_gbp ?? 0);
	const chartPoints = $derived(
		(overhead?.points ?? []).map((p) => ({ ts: p.ts, mw: p.avg_watts * 1000 }))
	);

	// Prefer the agent's own within_budget verdict; fall back to a local compare.
	const budgetStatus = $derived<'good' | 'warning'>(
		status
			? status.overhead.within_budget
				? 'good'
				: 'warning'
			: current && current.gbp_year <= budget
				? 'good'
				: 'warning'
	);
	const budgetPct = $derived(
		budget > 0 && current ? Math.min(100, (current.gbp_year / budget) * 100) : 0
	);
	const barColor = $derived(budgetStatus === 'good' ? 'var(--good)' : 'var(--warning)');

	// Show the empty/unreachable state only when we have nothing to show yet; a
	// transient poll failure after a good load keeps the last snapshot visible.
	const showEmpty = $derived(errKind !== 'none' && !current);
</script>

<PageHeader title="a3watch's own overhead" subtitle="this tool must stay near-invisible" />

{#if showEmpty}
	<Card>
		{#if errKind === 'not-configured'}
			<EmptyState
				title="No agent connected"
				message="No a3watch agent connection is configured. This is the normal state when viewing the dashboard on Vercel with no tunnel — connect an agent to see live overhead figures."
			/>
		{:else if errKind === 'unreachable'}
			<EmptyState
				title="Agent unreachable"
				message="The a3watch agent could not be reached over the tunnel. This is expected when the tunnel is down or the dashboard is viewed without one. Overhead figures will appear once the agent is reachable."
			/>
		{:else}
			<EmptyState title="Could not load overhead" message={errMsg || 'An unexpected error occurred while fetching overhead data.'} />
		{/if}
	</Card>
{:else if loading && !current}
	<Card>
		<p class="muted">Loading overhead…</p>
	</Card>
{:else if current}
	<div class="stack">
		<Card>
			<div class="tiles">
				<StatTile
					label="Projected cost"
					value={`${fmtGbp(current.gbp_year)}/yr`}
					sub={`budget ${fmtGbp(budget)}`}
					status={budgetStatus}
				/>
				<StatTile label="Average power" value={`${(current.avg_watts * 1000).toFixed(1)} mW`} />
				<StatTile label="Database size" value={fmtBytes(current.db_bytes)} sub="on NVMe" />
				<StatTile label="CPU time/day" value={`${(current.cpu_ms_day / 1000).toFixed(1)} s`} />
			</div>
		</Card>

		<Card title="Cost vs budget">
			<div class="budget-head">
				<span class="tabular big" style:color={barColor}>{fmtGbp(current.gbp_year)}<span class="unit"
						>/yr</span
					></span>
				<span class="muted tabular">budget {fmtGbp(budget)}</span>
			</div>
			<div
				class="meter"
				role="img"
				aria-label={`${fmtGbp(current.gbp_year)} per year, ${budgetPct.toFixed(0)}% of the ${fmtGbp(budget)} budget`}
			>
				<div class="meter-fill" style:width={`${budgetPct}%`} style:background={barColor}></div>
			</div>
			<p class="note">
				{#if budgetStatus === 'good'}
					<span style:color="var(--good)">Under budget</span> — {budgetPct.toFixed(0)}% of the annual
					allowance.
				{:else}
					<span style:color="var(--warning)">Over budget</span> — projected spend exceeds the {fmtGbp(
						budget
					)} annual allowance.
				{/if}
			</p>
		</Card>

		<Card title="Overhead power (last 7 days)">
			<LineChart
				points={chartPoints}
				series={[{ key: 'mw', label: 'overhead (mW)', slot: 1 }]}
				height={240}
				yLabel="mW"
				format={(n) => n.toFixed(1)}
			/>
			<p class="note">
				The power estimate assumes roughly 3 W per active core-second and is derived from the
				agent's own measured CPU time — not a wall-plug reading. Projected cost annualises that
				average draw.
			</p>
		</Card>
	</div>
{/if}

<style>
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
	.budget-head {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 12px;
		margin-bottom: 10px;
		flex-wrap: wrap;
	}
	.big {
		font-size: 24px;
		font-weight: 650;
		font-variant-numeric: proportional-nums lining-nums;
		line-height: 1;
	}
	.unit {
		font-size: 14px;
		font-weight: 600;
		color: var(--text-muted);
		margin-left: 2px;
	}
	.meter {
		position: relative;
		width: 100%;
		height: 12px;
		background: var(--surface-2);
		border: 1px solid var(--border);
		border-radius: 999px;
		overflow: hidden;
	}
	.meter-fill {
		height: 100%;
		border-radius: 999px;
		transition: width 0.3s ease;
	}
	.note {
		margin: 12px 0 0;
		font-size: 12px;
		line-height: 1.5;
		color: var(--text-secondary);
	}
</style>
