<script lang="ts">
	import { page } from '$app/stores';
	import { theme, toggleTheme, poll } from '$lib/stores';
	import { api, type Mode } from '$lib/api';

	const links = [
		{ href: '/', label: 'Overview' },
		{ href: '/server', label: 'Server' },
		{ href: '/disks', label: 'Disks' },
		{ href: '/power', label: 'Power & C‑states' },
		{ href: '/processes', label: 'Processes' },
		{ href: '/overhead', label: 'Overhead' },
		{ href: '/diagnostics', label: 'Diagnostics' },
		{ href: '/settings', label: 'Settings' }
	];

	type Conn = 'connected' | 'unreachable';

	let conn = $state<Conn>('unreachable');
	let mode = $state<Mode | null>(null);

	const connMeta: Record<Conn, { color: string; label: string }> = {
		connected: { color: 'var(--good)', label: 'connected' },
		unreachable: { color: 'var(--critical)', label: 'unreachable' }
	};

	$effect(() => {
		// poll() fires immediately on start, then every 30s while the tab is visible.
		return poll(
			() => api.health(),
			30000,
			(h) => {
				conn = 'connected';
				mode = h.mode;
			},
			() => {
				conn = 'unreachable';
				mode = null;
			}
		);
	});

	const current = $derived($page.url.pathname);
	function isActive(href: string): boolean {
		if (href === '/') return current === '/';
		return current === href || current.startsWith(href + '/');
	}
</script>

<nav class="nav" aria-label="Primary">
	<div class="brand">
		<span class="dot" style:background="var(--series-1)" aria-hidden="true"></span>
		<span class="name">a3watch</span>
	</div>

	<ul class="links">
		{#each links as l}
			<li>
				<a href={l.href} class:active={isActive(l.href)} aria-current={isActive(l.href) ? 'page' : undefined}>
					{l.label}
				</a>
			</li>
		{/each}
	</ul>

	<div class="controls">
		{#if mode}
			<span class="pill" class:diag={mode === 'diagnostic'} title="agent mode">
				{mode === 'diagnostic' ? 'diagnostic' : 'normal'}
			</span>
		{/if}

		<span class="conn" title={`agent ${connMeta[conn].label}`}>
			<span class="dot" style:background={connMeta[conn].color} aria-hidden="true"></span>
			<span class="conn-label muted">{connMeta[conn].label}</span>
		</span>

		<button class="theme" type="button" onclick={toggleTheme} aria-label="Toggle theme">
			{$theme === 'dark' ? '☾' : '☀'}
		</button>

		<!-- Cloudflare Access logout (served on this hostname by the CF edge) -->
		<a class="logout" href="/cdn-cgi/access/logout" title="Sign out">Log out</a>
	</div>
</nav>

<style>
	.nav {
		display: flex;
		align-items: center;
		gap: 16px;
		flex-wrap: wrap;
		padding: 10px 16px;
		background: var(--surface-1);
		border-bottom: 1px solid var(--border);
	}
	.brand {
		display: flex;
		align-items: center;
		gap: 8px;
		font-weight: 700;
		font-size: 15px;
	}
	.brand .dot {
		width: 10px;
		height: 10px;
		border-radius: 999px;
	}
	.links {
		display: flex;
		align-items: center;
		gap: 2px;
		list-style: none;
		margin: 0;
		padding: 0;
		flex-wrap: wrap;
		flex: 1 1 auto;
	}
	.links a {
		display: inline-block;
		padding: 6px 10px;
		border-radius: var(--radius-sm);
		color: var(--text-secondary);
		font-size: 13px;
		font-weight: 550;
		white-space: nowrap;
	}
	.links a:hover {
		color: var(--text-primary);
		background: var(--surface-2);
		text-decoration: none;
	}
	.links a.active {
		color: var(--text-primary);
		background: var(--surface-2);
	}
	.controls {
		display: flex;
		align-items: center;
		gap: 10px;
		margin-left: auto;
	}
	.pill {
		font-size: 11px;
		font-weight: 650;
		letter-spacing: 0.03em;
		text-transform: uppercase;
		padding: 2px 8px;
		border-radius: 999px;
		border: 1px solid var(--border);
		color: var(--text-muted);
	}
	.pill.diag {
		color: var(--warning);
		border-color: var(--warning);
	}
	.conn {
		display: inline-flex;
		align-items: center;
		gap: 6px;
	}
	.conn .dot {
		width: 8px;
		height: 8px;
		border-radius: 999px;
		flex-shrink: 0;
	}
	.conn-label {
		font-size: 12px;
	}
	.theme {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 30px;
		height: 30px;
		border-radius: var(--radius-sm);
		border: 1px solid var(--border);
		background: var(--surface-2);
		color: var(--text-primary);
		font-size: 15px;
		line-height: 1;
	}
	.theme:hover {
		border-color: var(--axis);
	}
	.logout {
		font-size: 12px;
		color: var(--text-secondary);
		padding: 4px 8px;
		border-radius: var(--radius-sm);
		border: 1px solid var(--border);
	}
	.logout:hover {
		color: var(--text-primary);
		background: var(--surface-2);
		text-decoration: none;
	}
	@media (max-width: 640px) {
		.conn-label {
			display: none;
		}
	}
</style>
