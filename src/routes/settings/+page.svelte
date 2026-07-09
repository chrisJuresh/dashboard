<script lang="ts">
	import { onMount } from 'svelte';
	import {
		api,
		ApiError,
		getApiBase,
		getToken,
		isConfigured,
		clearConnection,
		type ConfigView
	} from '$lib/api';
	import { theme, toggleTheme } from '$lib/stores';
	import { fmtGbp } from '$lib/format';
	import PageHeader from '$lib/components/PageHeader.svelte';
	import Card from '$lib/components/Card.svelte';
	import StatTile from '$lib/components/StatTile.svelte';
	import Badge from '$lib/components/Badge.svelte';
	import EmptyState from '$lib/components/EmptyState.svelte';

	type ErrKind = 'not-configured' | 'unreachable' | 'error';

	let config = $state<ConfigView | null>(null);
	let loading = $state(true);
	let errKind = $state<ErrKind | null>(null);
	let errMsg = $state('');

	// Local-only connection details (read from localStorage, never the network).
	let apiBase = $state('');
	let hasToken = $state(false);
	let configured = $state(false);

	function friendly(err: unknown): string {
		if (err instanceof ApiError) {
			if (err.status === 401 || err.status === 403)
				return 'Unauthorized — the stored bearer token was rejected by the agent.';
			return err.message || `Request failed (${err.status}).`;
		}
		return err instanceof Error ? err.message : 'Failed to load configuration.';
	}

	async function loadConfig() {
		loading = true;
		errKind = null;
		errMsg = '';
		try {
			config = await api.config();
		} catch (err) {
			config = null;
			if (err instanceof ApiError && err.message === 'not-configured') errKind = 'not-configured';
			else if (err instanceof ApiError && err.message === 'unreachable') errKind = 'unreachable';
			else errKind = 'error';
			errMsg = friendly(err);
		} finally {
			loading = false;
		}
	}

	function disconnect() {
		clearConnection();
		window.location.reload();
	}

	function diskType(rotational: boolean): string {
		return rotational ? 'HDD (rotational)' : 'SSD / NVMe';
	}

	onMount(() => {
		apiBase = getApiBase();
		hasToken = getToken() !== '';
		configured = isConfigured();
		loadConfig();
	});
</script>

<PageHeader
	title="Settings"
	subtitle="Read-only view of the agent's configuration and disk topology."
/>

<div class="stack">
	<div class="cols">
		<Card title="Connection">
			<dl class="kv">
				<div class="row">
					<dt>API base</dt>
					<dd><code class="tabular">{apiBase || '—'}</code></dd>
				</div>
				<div class="row">
					<dt>Bearer token</dt>
					<dd>
						{#if hasToken}
							<span class="mask">•••••••• <span class="muted">stored locally</span></span>
						{:else}
							<span class="muted">not set</span>
						{/if}
					</dd>
				</div>
			</dl>

			<p class="note muted">
				The API URL and bearer token are kept only in this browser (localStorage) and are sent
				nowhere except directly to your agent over the tunnel.
			</p>

			{#if configured}
				<button type="button" class="btn danger" onclick={disconnect}>Disconnect</button>
			{:else}
				<button type="button" class="btn" disabled>Not connected</button>
			{/if}
		</Card>

		<Card title="Appearance">
			<dl class="kv">
				<div class="row">
					<dt>Theme</dt>
					<dd class="cap">{$theme}</dd>
				</div>
			</dl>
			<p class="note muted">Theme preference is stored in this browser.</p>
			<button type="button" class="btn" onclick={toggleTheme}>
				Switch to {$theme === 'dark' ? 'light' : 'dark'}
			</button>
		</Card>
	</div>

	{#if loading}
		<Card title="Configuration">
			<p class="muted">Loading configuration…</p>
		</Card>
	{:else if errKind === 'not-configured'}
		<Card title="Configuration">
			<EmptyState
				title="Agent not configured"
				message="No agent connection is set — expected on the public dashboard. Add your agent URL and bearer token to connect."
			/>
		</Card>
	{:else if errKind === 'unreachable'}
		<Card title="Configuration">
			<EmptyState
				title="Agent unreachable"
				message="Couldn't reach the a3watch agent. If you're viewing this on Vercel, the cloudflared tunnel is likely down — this is the normal state without an active tunnel."
			/>
		</Card>
	{:else if errKind === 'error' || !config}
		<Card title="Configuration">
			<EmptyState title="Couldn't load configuration" message={errMsg} />
		</Card>
	{:else}
		<Card title="Configuration">
			<div class="tiles">
				<StatTile label="Sample interval" value={`${config.interval_s}s`} sub="between samples" />
				<StatTile
					label="Annual budget"
					value={fmtGbp(config.budget_gbp_year)}
					sub="self-overhead cap"
				/>
				<StatTile
					label="Mode"
					value={config.mode === 'diagnostic' ? 'Diagnostic' : 'Normal'}
					status={config.mode === 'diagnostic' ? 'warning' : 'good'}
					sub="agent operating mode"
				/>
			</div>

			<dl class="kv">
				<div class="row">
					<dt>Data directory</dt>
					<dd><code class="tabular">{config.data_dir || '—'}</code></dd>
				</div>
				<div class="row">
					<dt>Tunnel hostname</dt>
					<dd><code class="tabular">{config.tunnel_hostname || '—'}</code></dd>
				</div>
			</dl>
		</Card>

		<Card title="Disk topology">
			{#if config.disks.length === 0}
				<p class="muted">No disks reported by the agent.</p>
			{:else}
				<div class="table-wrap">
					<table class="disks tabular">
						<thead>
							<tr>
								<th>Device</th>
								<th>Role</th>
								<th>Label</th>
								<th>Mount</th>
								<th>Type</th>
								<th>Protected</th>
								<th>Monitored</th>
								<th>Pool</th>
							</tr>
						</thead>
						<tbody>
							{#each config.disks as d (d.dev)}
								<tr>
									<td><code>{d.dev}</code></td>
									<td>{d.role || '—'}</td>
									<td>{d.label || '—'}</td>
									<td><code class="mount">{d.mount || '—'}</code></td>
									<td class="muted">{diskType(d.rotational)}</td>
									<td>
										{#if d.protected}
											<Badge status="good" icon="✓" label="protected" />
										{:else}
											<Badge status="warning" icon="!" label="probeable" />
										{/if}
									</td>
									<td>
										{#if d.monitored}
											<Badge status="good" icon="✓" label="yes" />
										{:else}
											<Badge status="muted" icon="○" label="no" />
										{/if}
									</td>
									<td>{d.pool || '—'}</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			{/if}

			<p class="note muted">
				<strong class="ink">Protected</strong> disks are never probed — the agent won't even issue a
				non-waking power-state check against them. Roles, monitoring and protection are detected
				automatically and can only be changed on the server, by editing the config file and
				re-running <code>a3watch install --confirm</code>. This dashboard is read-only.
			</p>
		</Card>
	{/if}
</div>

<style>
	.stack {
		display: flex;
		flex-direction: column;
		gap: var(--gap);
	}
	.cols {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
		gap: var(--gap);
	}

	.kv {
		margin: 0 0 12px;
		display: flex;
		flex-direction: column;
		gap: 8px;
	}
	.kv .row {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 12px;
	}
	.kv dt {
		font-size: 13px;
		color: var(--text-secondary);
		flex-shrink: 0;
	}
	.kv dd {
		margin: 0;
		text-align: right;
		min-width: 0;
		word-break: break-all;
	}
	.cap {
		text-transform: capitalize;
	}
	.mask {
		letter-spacing: 0.1em;
	}

	code {
		font-family: ui-monospace, 'SF Mono', 'Cascadia Code', Menlo, Consolas, monospace;
		font-size: 12.5px;
		background: var(--surface-2);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		padding: 1px 6px;
	}

	.tiles {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
		gap: var(--gap);
		margin-bottom: 16px;
	}

	.table-wrap {
		overflow-x: auto;
	}
	table.disks {
		width: 100%;
		border-collapse: collapse;
		font-size: 13px;
		white-space: nowrap;
	}
	table.disks th {
		text-align: left;
		font-size: 11px;
		font-weight: 650;
		letter-spacing: 0.03em;
		text-transform: uppercase;
		color: var(--text-muted);
		padding: 0 12px 8px 0;
		border-bottom: 1px solid var(--border);
	}
	table.disks td {
		padding: 9px 12px 9px 0;
		border-bottom: 1px solid var(--border);
		color: var(--text-primary);
		vertical-align: middle;
	}
	table.disks tbody tr:last-child td {
		border-bottom: none;
	}
	.mount {
		max-width: 260px;
		display: inline-block;
		overflow: hidden;
		text-overflow: ellipsis;
		vertical-align: bottom;
	}

	.note {
		margin: 0 0 12px;
		font-size: 12.5px;
		line-height: 1.55;
		max-width: 80ch;
	}
	.note:last-child {
		margin-bottom: 0;
	}
	.ink {
		color: var(--text-secondary);
		font-weight: 650;
	}

	.btn {
		padding: 8px 14px;
		border-radius: var(--radius-sm);
		border: 1px solid var(--border);
		background: var(--surface-2);
		color: var(--text-primary);
		font-weight: 600;
	}
	.btn:hover:not(:disabled) {
		border-color: var(--axis);
	}
	.btn:disabled {
		opacity: 0.5;
		cursor: default;
	}
	.btn.danger {
		color: var(--critical);
		border-color: var(--critical);
		background: transparent;
	}
	.btn.danger:hover {
		background: color-mix(in srgb, var(--critical) 12%, transparent);
	}
</style>
