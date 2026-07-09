<script lang="ts">
	import '../app.css';
	// Import the stores module for its side effect: it keeps the document root
	// theme in sync (data-theme + localStorage) from first paint.
	import '$lib/stores';
	import Nav from '$lib/components/Nav.svelte';

	let { children }: { children: import('svelte').Snippet } = $props();

	// The agent serves this SPA behind Cloudflare Access, so anyone who reaches
	// this page is already authenticated at the edge — no client-side gate needed.
	// Each page handles its own API errors (EmptyState) if the agent is unreachable.
</script>

<div class="shell">
	<Nav />
	<main class="content">
		{@render children()}
	</main>
</div>

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
	@media (max-width: 640px) {
		.content {
			padding: 16px 12px 40px;
		}
	}
</style>
