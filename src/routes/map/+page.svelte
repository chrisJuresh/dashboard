<script lang="ts">
	import { api, ApiError, type SysMap, type TreeNode } from '$lib/api';
	import { poll } from '$lib/stores';
	import { fmtBytes } from '$lib/format';
	import PageHeader from '$lib/components/PageHeader.svelte';
	import Card from '$lib/components/Card.svelte';
	import EmptyState from '$lib/components/EmptyState.svelte';

	let map = $state<SysMap | null>(null);
	let loading = $state(true);
	let error = $state<unknown>(null);

	function isUnreachable(e: unknown): boolean {
		return e instanceof ApiError && (e.message === 'not-configured' || e.message === 'unreachable');
	}

	// Auto-updating: re-fetch every 90s so the map tracks changes (containers,
	// commits, units). The endpoint is read-only + non-waking.
	$effect(() =>
		poll(
			() => api.sysmap(),
			90000,
			(r) => {
				map = r;
				loading = false;
				error = null;
			},
			(e) => {
				loading = false;
				error = e;
			}
		)
	);

	const worldSecrets = $derived((map?.secrets ?? []).filter((s) => s.world_accessible));
</script>

<PageHeader
	title="System map"
	subtitle="how docker, the tunnel, Access, git/deploy, systemd and storage fit together — auto-discovered, read-only, secrets redacted"
/>

{#if error && isUnreachable(error)}
	<EmptyState
		title="Agent not reachable"
		message="The a3watch agent isn't reachable. The system map appears here once the agent is online."
	/>
{:else if loading && !map}
	<p class="muted loading">Discovering configuration…</p>
{:else if map}
	{#if error && !isUnreachable(error)}
		<p class="err">Couldn't refresh: {error instanceof Error ? error.message : 'unknown error'}</p>
	{/if}

	<!-- How it fits together -->
	<div class="block">
		<Card title="How it fits together">
			<div class="pipes">
				{#each map.pipelines as p (p.name)}
					<div class="pipe">
						<div class="pipe-name">{p.name}</div>
						<div class="flow">
							{#each p.steps as s, i (i)}
								{#if i > 0}<span class="arrow" aria-hidden="true">→</span>{/if}
								<span class="step">{s}</span>
							{/each}
						</div>
					</div>
				{/each}
			</div>
		</Card>
	</div>

	<!-- Security: world-readable secrets -->
	{#if worldSecrets.length}
		<div class="block">
			<Card title="⚠ World-readable secret files">
				<p class="warn-note">
					These secret files are readable by any user on the box. Consider tightening to
					<code>0600</code> or moving them into a secrets manager. (a3watch never reads their contents.)
				</p>
				<ul class="plain">
					{#each worldSecrets as s (s.path)}
						<li><code class="path">{s.path}</code> <span class="muted">{s.mode} · {s.owner}</span></li>
					{/each}
				</ul>
			</Card>
		</div>
	{/if}

	<!-- Docker services -->
	<div class="block">
		<Card title={`Services — Docker (${map.docker.containers.length})`}>
			<div class="table-wrap">
				<table>
					<thead>
						<tr><th>Container</th><th>Image</th><th>State</th><th>Ports</th><th>Networks</th></tr>
					</thead>
					<tbody>
						{#each map.docker.containers as c (c.name)}
							<tr>
								<td class="mono">{c.name}</td>
								<td class="mono muted">{c.image}</td>
								<td>
									<span class="state" class:bad={c.state === 'restarting' || c.state === 'exited'} class:ok={c.state === 'running'}>{c.state}</span>
									{#if c.restarts > 0}<span class="muted"> ×{c.restarts}</span>{/if}
								</td>
								<td class="mono muted">{c.ports.join(', ') || '—'}</td>
								<td class="mono muted">{c.networks.join(', ') || '—'}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
			{#if map.docker.networks.length}
				<p class="sub muted">
					Networks: {map.docker.networks.map((n) => `${n.name} (${n.driver})`).join(' · ')}
				</p>
			{/if}
			{#if map.docker.compose_files.length}
				<p class="sub muted">Compose: {map.docker.compose_files.join(' · ')}</p>
			{/if}
		</Card>
	</div>

	<!-- Access / domains / tunnel -->
	<div class="block">
		<Card title="Access, domains & tunnel">
			<dl class="kv">
				<dt>Hostnames</dt>
				<dd class="mono">{map.access_domains.hostnames.join(', ') || '—'}</dd>
				<dt>Cloudflare Access</dt>
				<dd>
					{map.access_domains.access.enabled ? 'enabled' : 'disabled'} · team
					<span class="mono">{map.access_domains.access.team_domain || '—'}</span> · aud
					<span class="mono">{map.access_domains.access.aud || '—'}</span>
				</dd>
				<dt>Tunnel</dt>
				<dd>
					{map.tunnel.running ? 'running' : 'down'}
					{#if map.tunnel.container}<span class="mono muted"> · {map.tunnel.container}</span>{/if}
					<div class="muted sub">{map.tunnel.note}</div>
				</dd>
				<dt>API bind</dt>
				<dd class="mono">{map.access_domains.api_bind}</dd>
				<dt>CORS origins</dt>
				<dd class="mono muted">{map.access_domains.cors_allow_origins.join(', ') || '—'}</dd>
			</dl>
			{#if !map.cloud_snapshot}
				<p class="sub muted">
					Cloud-side DNS + Access policies aren't pulled automatically (no stored keys). A
					point-in-time snapshot can be added with a one-off read-only token.
				</p>
			{/if}
		</Card>
	</div>

	<!-- Cloudflare cloud-side snapshot -->
	{#if map.cloud_snapshot}
		{@const cs = map.cloud_snapshot}
		<div class="block">
			<Card title="Cloudflare — cloud-side snapshot">
				<p class="sub muted">
					Point-in-time snapshot of zone <span class="mono">{cs.zone}</span> (DNS · Access ·
					tunnel ingress), taken {new Date((cs.generated ?? 0) * 1000).toLocaleString()}. Not
					live-polled — no token stored.
				</p>

				{#if cs.tunnels?.length}
					<h4 class="csh">Tunnel ingress</h4>
					{#each cs.tunnels as t (t.id)}
						<div class="sub"><span class="mono">{t.name}</span> <span class="muted">({t.status})</span></div>
						<ul class="plain ind">
							{#each t.ingress as r (r.hostname + r.service)}
								<li class="mono">
									<span class="host">{r.hostname || '(catch-all)'}</span> → {r.service}
								</li>
							{/each}
						</ul>
					{/each}
				{/if}

				{#if cs.access_apps?.length}
					<h4 class="csh">Access apps</h4>
					<ul class="plain">
						{#each cs.access_apps as a (a.domain)}
							<li>
								<span class="mono">{a.domain}</span>
								<span class="muted">— {a.name} · {a.session}</span>
								{#each a.policies as p (p.name)}
									<div class="sub ind">{p.decision}: {p.allow.join(', ') || '—'}</div>
								{/each}
							</li>
						{/each}
					</ul>
				{/if}

				{#if cs.dns?.length}
					<h4 class="csh">DNS ({cs.dns.length})</h4>
					<div class="table-wrap">
						<table>
							<thead><tr><th>Name</th><th>Type</th><th>Content</th><th></th></tr></thead>
							<tbody>
								{#each cs.dns as d (d.name + d.type + d.content)}
									<tr>
										<td class="mono">{d.name}</td>
										<td>{d.type}</td>
										<td class="mono muted">{d.content}</td>
										<td class="muted">{d.proxied ? '☁ proxied' : 'DNS-only'}</td>
									</tr>
								{/each}
							</tbody>
						</table>
					</div>
				{/if}
			</Card>
		</div>
	{/if}

	<!-- Git & deploy -->
	<div class="block">
		<Card title="Git & deploy">
			<div class="repos">
				{#each map.git as g (g.path)}
					<div class="repo">
						<div class="repo-head">
							<span class="mono repo-path">{g.path}</span>
							<span class="branch">{g.branch}</span>
							{#if g.dirty_files > 0}<span class="dirty">{g.dirty_files} uncommitted</span>{/if}
						</div>
						<div class="muted mono sub">{g.remote}</div>
						<div class="muted sub">{g.last_commit}</div>
						{#if g.ci_workflows.length}
							<div class="sub">CI: <span class="mono">{g.ci_workflows.join(', ')}</span></div>
						{/if}
					</div>
				{/each}
			</div>
		</Card>
	</div>

	<!-- systemd -->
	<div class="block">
		<Card title="Scheduled jobs & services (systemd)">
			<ul class="plain">
				{#each map.systemd.units as u (u.unit)}
					<li class="unit">
						<span class="mono">{u.unit}</span>
						<span class="ustate" class:ok={u.active === 'active'} class:bad={u.active === 'failed'}
							>{u.active}</span
						><span class="muted"> · {u.enabled}</span>
						{#if u.description}<span class="muted"> — {u.description}</span>{/if}
						{#if u.schedule}<div class="sub mono">⏱ {u.schedule}</div>{/if}
						{#if u.exec}<div class="sub mono muted">{u.exec}</div>{/if}
					</li>
				{/each}
			</ul>
		</Card>
	</div>

	<!-- Secret inventory -->
	<div class="block">
		<Card title="Secret files (paths only — never contents)">
			<ul class="plain">
				{#each map.secrets as s (s.path)}
					<li>
						<code class="path" class:danger={s.world_accessible}>{s.path}</code>
						<span class="muted">{s.mode} · {s.owner}</span>
						{#if s.world_accessible}<span class="dirty">world-readable</span>{/if}
					</li>
				{/each}
			</ul>
		</Card>
	</div>

	<!-- ~/ tree -->
	<div class="block">
		<Card
			title={`Home folder (~/) — ${map.home_tree.total_files ?? 0} files, ${fmtBytes(map.home_tree.total_bytes ?? 0)}`}
		>
			{#if map.home_tree.error}
				<p class="muted">{map.home_tree.error}</p>
			{:else if map.home_tree.root}
				<div class="tree">
					{#each map.home_tree.root.children ?? [] as c (c.name)}
						{@render treeNode(c)}
					{/each}
				</div>
				{#if map.home_tree.capped}
					<p class="sub muted">Listing capped for size — largest branches shown.</p>
				{/if}
			{/if}
		</Card>
	</div>
{/if}

{#snippet treeNode(n: TreeNode)}
	{#if n.type === 'dir' && n.children && n.children.length}
		<details class="tdir">
			<summary
				><span class="tname">{n.name}/</span>
				<span class="tmeta muted">{fmtBytes(n.size ?? 0)} · {n.files ?? 0} files</span></summary
			>
			<div class="tchildren">
				{#each n.children as c (c.name)}{@render treeNode(c)}{/each}
			</div>
		</details>
	{:else}
		<div class="tleaf">
			<span class="tname" class:tdirname={n.type === 'dir' || n.type === 'mount'}
				>{n.name}{n.type === 'dir' || n.type === 'mount' ? '/' : ''}</span
			>
			<span class="tmeta muted">
				{#if n.type === 'file'}{fmtBytes(n.size ?? 0)}
				{:else if n.type === 'dir'}{fmtBytes(n.size ?? 0)} · {n.files ?? 0} files{n.collapsed
						? ' (collapsed)'
						: ''}
				{:else}{n.type}{/if}
				{#if n.note}— {n.note}{/if}
			</span>
		</div>
	{/if}
{/snippet}

<style>
	.block {
		margin-bottom: var(--gap);
	}
	.loading,
	.err {
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
	.sub {
		font-size: 12px;
		margin: 4px 0 0;
	}
	.csh {
		font-size: 12px;
		font-weight: 700;
		text-transform: uppercase;
		letter-spacing: 0.03em;
		color: var(--text-secondary);
		margin: 16px 0 6px;
	}
	.ind {
		padding-left: 14px;
	}
	.host {
		color: var(--text-primary);
		font-weight: 600;
	}

	/* pipelines */
	.pipes {
		display: flex;
		flex-direction: column;
		gap: 14px;
	}
	.pipe-name {
		font-size: 12px;
		font-weight: 700;
		text-transform: uppercase;
		letter-spacing: 0.03em;
		color: var(--text-secondary);
		margin-bottom: 6px;
	}
	.flow {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 6px;
	}
	.step {
		background: var(--surface-2);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		padding: 3px 9px;
		font-size: 12px;
	}
	.arrow {
		color: var(--text-muted);
		font-weight: 700;
	}

	/* tables */
	.table-wrap {
		overflow-x: auto;
	}
	table {
		width: 100%;
		border-collapse: collapse;
		font-size: 13px;
	}
	th {
		text-align: left;
		font-size: 11px;
		text-transform: uppercase;
		letter-spacing: 0.03em;
		color: var(--text-muted);
		font-weight: 600;
		padding: 4px 10px 4px 0;
		border-bottom: 1px solid var(--border);
	}
	td {
		padding: 6px 10px 6px 0;
		border-bottom: 1px solid var(--surface-2);
		vertical-align: top;
	}
	.state.ok,
	.ustate.ok {
		color: var(--good);
	}
	.state.bad,
	.ustate.bad {
		color: var(--serious);
		font-weight: 600;
	}

	/* kv */
	.kv {
		display: grid;
		grid-template-columns: max-content 1fr;
		gap: 6px 16px;
		margin: 0;
		font-size: 13px;
	}
	.kv dt {
		color: var(--text-muted);
		font-size: 12px;
	}
	.kv dd {
		margin: 0;
	}

	/* repos */
	.repos {
		display: flex;
		flex-direction: column;
		gap: 12px;
	}
	.repo-head {
		display: flex;
		align-items: baseline;
		gap: 10px;
		flex-wrap: wrap;
	}
	.repo-path {
		font-weight: 600;
		color: var(--text-primary);
	}
	.branch {
		font-size: 11px;
		background: var(--surface-2);
		border: 1px solid var(--border);
		border-radius: 999px;
		padding: 1px 8px;
		color: var(--series-1);
		font-weight: 600;
	}
	.dirty {
		font-size: 11px;
		color: var(--warning);
		font-weight: 600;
	}

	/* lists */
	.plain {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 7px;
		font-size: 13px;
	}
	.unit {
		line-height: 1.4;
	}
	.ustate {
		font-size: 12px;
	}
	.path {
		font-family: ui-monospace, 'SFMono-Regular', Menlo, monospace;
		font-size: 12px;
	}
	.path.danger {
		color: var(--serious);
	}
	.warn-note {
		font-size: 13px;
		color: var(--text-secondary);
		margin: 0 0 10px;
	}
	code {
		background: var(--surface-2);
		padding: 0 4px;
		border-radius: 4px;
	}

	/* tree */
	.tree {
		font-size: 13px;
		line-height: 1.6;
	}
	.tdir > summary {
		cursor: pointer;
		list-style-position: inside;
	}
	.tchildren {
		padding-left: 16px;
		border-left: 1px solid var(--surface-2);
		margin-left: 4px;
	}
	.tleaf {
		padding-left: 18px;
	}
	.tname {
		font-family: ui-monospace, 'SFMono-Regular', Menlo, monospace;
	}
	.tdirname {
		color: var(--text-secondary);
	}
	.tmeta {
		font-size: 11px;
		margin-left: 6px;
	}
</style>
