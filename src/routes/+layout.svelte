<script lang="ts">
	import '../app.css';
	import { onMount } from 'svelte';
	import { isConfigured, isSameOrigin, api } from '$lib/api';
	// Import the stores module for its side effect: it keeps the document root
	// theme in sync (data-theme + localStorage) from first paint.
	import '$lib/stores';
	import Nav from '$lib/components/Nav.svelte';
	import ConnectScreen from '$lib/components/ConnectScreen.svelte';
	import Login from '$lib/components/Login.svelte';

	let { children }: { children: import('svelte').Snippet } = $props();

	type Gate = 'loading' | 'app' | 'login' | 'setup' | 'connect';
	let gate = $state<Gate>('loading');

	async function check() {
		// Remote/bearer mode (an absolute API base was configured): use the connect screen.
		if (!isSameOrigin()) {
			gate = isConfigured() ? 'app' : 'connect';
			return;
		}
		// Same-origin mode: the agent serves this page + gates /api via a login session.
		try {
			const s = await api.session();
			gate = s.authenticated ? 'app' : s.login_configured ? 'login' : 'setup';
		} catch {
			gate = 'connect';
		}
	}

	onMount(check);
</script>

{#if gate === 'loading'}
	<div class="center muted">Loading…</div>
{:else if gate === 'app'}
	<div class="shell">
		<Nav />
		<main class="content">
			{@render children()}
		</main>
	</div>
{:else if gate === 'login'}
	<Login onLoggedIn={() => (gate = 'app')} />
{:else if gate === 'setup'}
	<div class="center">
		<div class="card setup">
			<h1>a3watch</h1>
			<p class="secondary">No login is set yet. On the server, run:</p>
			<pre>sudo a3watch set-login --email you@example.com</pre>
			<p class="muted">Then reload this page and sign in.</p>
		</div>
	</div>
{:else}
	<ConnectScreen onConnected={() => (gate = 'app')} />
{/if}

<style>
	.shell {
		display: flex;
		flex-direction: column;
		min-height: 100vh;
		width: 100%;
		overflow-x: hidden;
	}
	.content {
		width: 100%;
		max-width: 1100px;
		margin: 0 auto;
		padding: 24px 16px 48px;
		min-width: 0;
	}
	.center {
		min-height: 100vh;
		display: grid;
		place-items: center;
		padding: 24px;
	}
	.setup {
		max-width: 460px;
	}
	.setup pre {
		background: var(--surface-2);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		padding: 10px 12px;
		overflow-x: auto;
	}
	@media (max-width: 640px) {
		.content {
			padding: 16px 12px 40px;
		}
	}
</style>
