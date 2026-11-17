<script lang="ts">
	// Liked songs: the Songs row in Library opens only the starred tracks.
	import { cachedNow, getStarred } from '../api';
	import ErrorState from '../components/ErrorState.svelte';
	import Icon from '../components/Icon.svelte';
	import TrackList from '../components/TrackList.svelte';
	import { getPlayer } from '../player.svelte';
	import { router } from '../router.svelte';

	const player = getPlayer();
	const load = () =>
		cachedNow('library:starred', getStarred, { fresh: 0, refresh: (s) => (data = s) });
	let data = $state(load());
</script>

<div class="page">
	<div class="head">
		<button class="back" onclick={() => router.go('/library')}>
			<Icon name="chevronLeft" size={18} />Library
		</button>
	</div>
	<div class="title-row">
		<h1 class="page-title">Songs</h1>
		{#await data then s}
			{#if s.songs.length}
				<div class="actions">
					<button class="btn secondary" onclick={() => player.playList(s.songs)}>
						<Icon name="play" size={14} />Play
					</button>
					<button
						class="btn secondary"
						onclick={() => player.playList(s.songs, 0, { shuffle: true })}
					>
						<Icon name="shuffle" size={14} />Shuffle
					</button>
				</div>
			{/if}
		{/await}
	</div>

	{#await data}
		<div class="spinner"></div>
	{:then s}
		{#if !s.songs.length}
			<div class="empty-state">
				<Icon name="note" size={44} />
				<h3>No liked songs yet</h3>
				<p>Tap the heart on songs you love and they appear here.</p>
			</div>
		{:else}
			<div class="pad"><TrackList songs={s.songs} showLove={false} /></div>
		{/if}
	{:catch error}
		<ErrorState {error} retry={() => (data = load())} />
	{/await}
</div>

<style>
	.head {
		display: flex;
		align-items: center;
		padding: 0 var(--gutter);
		margin-bottom: 8px;
	}
	.back {
		display: inline-flex;
		align-items: center;
		gap: 2px;
		color: var(--accent);
		font-size: 14px;
	}
	.title-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
		padding-right: var(--gutter);
	}
	.title-row .page-title {
		margin-bottom: 12px;
	}
	.actions {
		display: flex;
		gap: 8px;
		margin-bottom: 12px;
	}
	.actions .btn {
		min-width: 0;
		height: 28px;
		padding: 0 12px;
		font-size: 13px;
		flex: none;
	}
</style>
