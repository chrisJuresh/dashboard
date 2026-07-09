<script lang="ts">
	import '../app.css';
	import { onMount } from 'svelte';
	import { isConfigured } from '$lib/api';
	// Import the stores module for its side effect: it subscribes the theme
	// store to document.documentElement[data-theme] and localStorage, so simply
	// loading it here keeps the document root theme in sync from first paint.
	import '$lib/stores';
	import Nav from '$lib/components/Nav.svelte';
	import ConnectScreen from '$lib/components/ConnectScreen.svelte';

	let { children }: { children: import('svelte').Snippet } = $props();

	// Pure client-side SPA (ssr=false); localStorage is available on mount.
	let configured = $state(false);

	onMount(() => {
		configured = isConfigured();
	});
</script>

{#if configured}
	<div class="shell">
		<Nav />
		<main class="content">
			{@render children()}
		</main>
	</div>
{:else}
	<ConnectScreen onConnected={() => (configured = true)} />
{/if}

<style>
	.shell {
		display: flex;
		flex-direction: column;
		min-height: 100vh;
		width: 100%;
		/* Nav wraps its own contents on narrow screens; keep the body from ever
		   scrolling sideways regardless of child content. */
		overflow-x: hidden;
	}
	.content {
		width: 100%;
		max-width: 1100px;
		margin: 0 auto;
		padding: 24px 16px 48px;
		/* allow flex/grid children to shrink instead of forcing horizontal scroll */
		min-width: 0;
	}
	@media (max-width: 640px) {
		.content {
			padding: 16px 12px 40px;
		}
	}
</style>
