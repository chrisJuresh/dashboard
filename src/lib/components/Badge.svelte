<script lang="ts">
	type Status = 'good' | 'warning' | 'serious' | 'critical' | 'muted';

	let {
		status,
		label,
		icon
	}: { status: Status; label: string; icon?: string } = $props();

	const colors: Record<Status, string> = {
		good: 'var(--good)',
		warning: 'var(--warning)',
		serious: 'var(--serious)',
		critical: 'var(--critical)',
		muted: 'var(--text-muted)'
	};
</script>

<span class="badge" style:color={colors[status]}>
	{#if icon}<span class="icon" aria-hidden="true">{icon}</span>{/if}
	<span>{label}</span>
</span>

<style>
	.icon {
		font-size: 11px;
		line-height: 1;
	}
</style>
