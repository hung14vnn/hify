<script lang="ts">
	// Round user avatar: initials on a neutral Apple-style disc.
	let { username, size = 32 }: { username?: string; size?: number } = $props();

	const initials = $derived(
		(username ?? '?')
			.split(/[\s._-]+/)
			.filter(Boolean)
			.slice(0, 2)
			.map((w) => w[0].toUpperCase())
			.join('') || '?'
	);
</script>

<span class="avatar" style:--size="{size}px">
	<span class="initials" aria-hidden="true">{initials}</span>
</span>

<style>
	.avatar {
		width: var(--size);
		height: var(--size);
		flex-shrink: 0;
		border-radius: 50%;
		overflow: hidden;
		display: grid;
		place-items: center;
		background: #3a3a3c;
		box-shadow: inset 0 0 0 0.5px rgba(255, 255, 255, 0.14);
	}
	.initials {
		color: #fff;
		font-weight: 600;
		font-size: calc(var(--size) * 0.4);
		letter-spacing: 0.02em;
		user-select: none;
	}
</style>
