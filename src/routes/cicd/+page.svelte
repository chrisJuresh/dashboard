<script lang="ts">
	import { onMount } from 'svelte';
	import { api, ApiError, type CicdStatus, type CicdItem, type CicdState } from '$lib/api';
	import { fmtRelative } from '$lib/format';
	import { nowStore } from '$lib/stores';
	import PageHeader from '$lib/components/PageHeader.svelte';
	import Card from '$lib/components/Card.svelte';
	import EmptyState from '$lib/components/EmptyState.svelte';

	let status = $state<CicdStatus | null>(null);
	let loading = $state(true);
	let refreshing = $state(false);
	let error = $state<unknown>(null);

	function isUnreachable(e: unknown): boolean {
		return e instanceof ApiError && (e.message === 'not-configured' || e.message === 'unreachable');
	}

	async function load(force = false) {
		if (force) refreshing = true;
		try {
			status = await api.cicd(force);
			error = null;
		} catch (e) {
			error = e;
		} finally {
			loading = false;
			refreshing = false;
		}
	}
	// Fetch once on open (not polled — the unauthenticated GitHub API is rate-limited).
	onMount(() => load(false));

	const STATE: Record<CicdState, { label: string; icon: string; cls: string }> = {
		up_to_date: { label: 'up to date', icon: '✓', cls: 'ok' },
		deploying: { label: 'deploying', icon: '↻', cls: 'deploy' },
		out_of_date: { label: 'out of date', icon: '↑', cls: 'stale' },
		ci_failed: { label: 'CI failed', icon: '✕', cls: 'fail' },
		unknown: { label: 'unknown', icon: '?', cls: 'unknown' }
	};
	function st(i: CicdItem) {
		return STATE[i.state] ?? STATE.unknown;
	}

	const checkedAgo = $derived.by(() => {
		void $nowStore;
		return status ? fmtRelative(status.generated) : '';
	});
</script>

<PageHeader title="CI/CD" subtitle="are the live deployments current with GitHub, mid-deploy, or behind?" />

{#if error && isUnreachable(error)}
	<EmptyState
		title="Agent not reachable"
		message="The a3watch agent isn't reachable. CI/CD status appears here once it's online."
	/>
{:else if loading && !status}
	<p class="muted line">Checking GitHub…</p>
{:else if status}
	<!-- headline -->
	<div class="block">
		<div class="banner" class:good={status.summary.all_up_to_date} class:warn={!status.summary.all_up_to_date}>
			<span class="b-icon">{status.summary.all_up_to_date ? '✓' : '●'}</span>
			<div class="b-text">
				{#if status.summary.all_up_to_date}
					<strong>All deployments up to date</strong>
				{:else}
					<strong>Attention needed</strong>
					<span class="b-sub">
						{[
							status.summary.deploying ? `${status.summary.deploying} deploying` : '',
							status.summary.out_of_date ? `${status.summary.out_of_date} out of date` : '',
							status.summary.ci_failed ? `${status.summary.ci_failed} CI failed` : ''
						]
							.filter(Boolean)
							.join(' · ')}
					</span>
				{/if}
			</div>
			<div class="b-actions">
				<span class="muted checked">checked {checkedAgo}{status.cached ? ' (cached)' : ''}</span>
				<button type="button" class="refresh" onclick={() => load(true)} disabled={refreshing}>
					{refreshing ? 'Checking…' : 'Refresh'}
				</button>
			</div>
		</div>
	</div>

	{#if error && !isUnreachable(error)}
		<p class="err line">Couldn't refresh: {error instanceof Error ? error.message : 'unknown'}</p>
	{/if}

	<!-- per-deployment -->
	<div class="grid">
		{#each status.items as i (i.name)}
			<Card>
				<div class="head">
					<span class="name">{i.name}</span>
					<span class="state {st(i).cls}"><span class="s-icon">{st(i).icon}</span>{st(i).label}</span>
				</div>
				<div class="repo mono muted">{i.repo}{i.kind === 'manual' ? ' · manual deploy' : ''}</div>
				<p class="detail">{i.detail}</p>

				<dl class="kv">
					{#if i.latest_commit}
						<dt>Latest commit</dt>
						<dd><span class="mono">{i.latest_commit}</span> {i.commit_msg ? `— ${i.commit_msg}` : ''}</dd>
					{/if}
					{#if i.run}
						<dt>Workflow</dt>
						<dd>
							<span
								class="run"
								class:running={i.run.status !== 'completed'}
								class:ok={i.run.conclusion === 'success'}
								class:bad={i.run.conclusion && i.run.conclusion !== 'success'}
							>
								{i.run.status === 'completed' ? i.run.conclusion : i.run.status}
							</span>
							{#if i.run.head}<span class="mono muted"> · {i.run.head}</span>{/if}
							{#if i.run.url}<a class="link" href={i.run.url} target="_blank" rel="noreferrer">view run ↗</a>{/if}
						</dd>
					{/if}
					{#if i.kind === 'manual'}
						<dt>Checkout</dt>
						<dd>
							<span class="mono">{i.local_head}</span> vs origin <span class="mono">{i.origin_head}</span>
							{#if i.behind}<span class="behind"> · {i.behind} behind</span>{/if}
						</dd>
					{/if}
				</dl>
			</Card>
		{/each}
	</div>

	<p class="foot muted">
		CI apps (films, which) deploy via GitHub Actions → GHCR → Watchtower (~2 min after a build).
		"Up to date" means the running container is at least as new as the last successful build.
		dashboard is built + deployed manually, so it's compared to origin/main.
	</p>
{/if}

<style>
	.block {
		margin-bottom: var(--gap);
	}
	.line {
		font-size: 13px;
		padding: 8px 0;
	}
	.err {
		color: var(--serious);
	}
	.mono {
		font-family: ui-monospace, 'SFMono-Regular', Menlo, monospace;
		font-size: 12px;
	}

	.banner {
		display: flex;
		align-items: center;
		gap: 12px;
		padding: 14px 16px;
		border-radius: var(--radius);
		border: 1px solid var(--border);
		background: var(--surface-1);
		flex-wrap: wrap;
	}
	.banner.good {
		border-color: color-mix(in srgb, var(--good) 45%, var(--border));
		background: color-mix(in srgb, var(--good) 8%, var(--surface-1));
	}
	.banner.warn {
		border-color: color-mix(in srgb, var(--warning) 50%, var(--border));
		background: color-mix(in srgb, var(--warning) 10%, var(--surface-1));
	}
	.b-icon {
		font-size: 20px;
		line-height: 1;
	}
	.banner.good .b-icon {
		color: var(--good);
	}
	.banner.warn .b-icon {
		color: var(--warning);
	}
	.b-text {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}
	.b-sub {
		font-size: 13px;
		color: var(--text-secondary);
	}
	.b-actions {
		margin-left: auto;
		display: flex;
		align-items: center;
		gap: 12px;
	}
	.checked {
		font-size: 12px;
	}
	.refresh {
		border: 1px solid var(--border);
		background: var(--surface-2);
		color: var(--text-primary);
		border-radius: var(--radius-sm);
		padding: 5px 12px;
		font-weight: 600;
		font-size: 13px;
	}
	.refresh:hover:not(:disabled) {
		border-color: var(--series-1);
		color: var(--series-1);
	}
	.refresh:disabled {
		opacity: 0.6;
	}

	.grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
		gap: var(--gap);
	}
	.head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
	}
	.name {
		font-size: 15px;
		font-weight: 700;
	}
	.repo {
		margin: 2px 0 8px;
	}
	.detail {
		font-size: 13px;
		margin: 0 0 10px;
		color: var(--text-secondary);
		line-height: 1.45;
	}
	.state {
		display: inline-flex;
		align-items: center;
		gap: 5px;
		font-size: 12px;
		font-weight: 700;
		padding: 2px 9px;
		border-radius: 999px;
		white-space: nowrap;
	}
	.s-icon {
		font-size: 11px;
	}
	.state.ok {
		color: var(--good);
		background: color-mix(in srgb, var(--good) 14%, transparent);
	}
	.state.deploy {
		color: var(--series-1);
		background: color-mix(in srgb, var(--series-1) 14%, transparent);
	}
	.state.stale {
		color: var(--warning);
		background: color-mix(in srgb, var(--warning) 16%, transparent);
	}
	.state.fail {
		color: var(--serious);
		background: color-mix(in srgb, var(--serious) 14%, transparent);
	}
	.state.unknown {
		color: var(--text-muted);
		background: var(--surface-2);
	}

	.kv {
		display: grid;
		grid-template-columns: max-content 1fr;
		gap: 4px 12px;
		margin: 0;
		font-size: 13px;
	}
	.kv dt {
		color: var(--text-muted);
		font-size: 12px;
	}
	.kv dd {
		margin: 0;
		min-width: 0;
		overflow-wrap: anywhere;
	}
	.run {
		font-weight: 600;
		font-size: 12px;
	}
	.run.ok {
		color: var(--good);
	}
	.run.bad {
		color: var(--serious);
	}
	.run.running {
		color: var(--series-1);
	}
	.link {
		font-size: 12px;
		margin-left: 6px;
	}
	.behind {
		color: var(--warning);
		font-weight: 600;
	}
	.foot {
		font-size: 12px;
		line-height: 1.5;
		max-width: 76ch;
		margin: var(--gap) 0 0;
	}
</style>
