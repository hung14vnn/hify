<script lang="ts">
	// Apple Music-style Library landing: section rows, then a recently played shelf.
	import {
		allNow,
		cachedNow,
		getAlbumList,
		getRecentlyPlayedSongs,
		optional,
		type Maybe
	} from '../api';
	import AlbumCard from '../components/AlbumCard.svelte';
	import Icon from '../components/Icon.svelte';
	import ProfileButton from '../components/ProfileButton.svelte';
	import type { Album, Song } from '../types';

	const rows = [
		{ path: '/playlists', label: 'Playlists', icon: 'playlist' },
		{ path: '/artists', label: 'Artists', icon: 'mic' },
		{ path: '/albums', label: 'Albums', icon: 'album' },
		{ path: '/songs', label: 'Songs', icon: 'note' },
		{ path: '/downloads', label: 'Downloaded', icon: 'download' }
	] as const;

	const load = () =>
		allNow([
			optional(cachedNow('library:albums', () => getAlbumList('starred', 12))),
			optional(cachedNow('library:recent:40', () => getRecentlyPlayedSongs(40)))
		]);
	let data = $state<Maybe<[Album[], Song[]]>>(load());

	function albumsFromSongs(songs: Song[]): Album[] {
		const seen = new Set<string>();
		const albums: Album[] = [];
		for (const song of songs) {
			if (!song.albumId || !song.album || seen.has(song.albumId)) continue;
			seen.add(song.albumId);
			albums.push({
				id: song.albumId,
				name: song.album,
				artist: song.displayArtist ?? song.artist,
				coverArt: song.coverArt
			});
		}
		return albums;
	}
</script>

<div class="page">
	<div class="head">
		<h1 class="page-title">Library</h1>
		<ProfileButton />
	</div>

	{#await data}
		<div class="spinner"></div>
	{:then loaded}
		{@const [albums, recent] = loaded}
		{@const recentAlbums = albumsFromSongs(recent)}
		{@const shelf = recentAlbums.length ? recentAlbums : albums}

		<ul class="rows">
			{#each rows as row}
				<li>
					<a href="#{row.path}">
						<Icon name={row.icon} size={20} />
						<span>{row.label}</span>
						<Icon name="chevronRight" size={15} class="chev" />
					</a>
				</li>
			{/each}
		</ul>

		{#if shelf.length}
			<section class="recent">
				<h2 class="section-title">{recentAlbums.length ? 'Recently Played' : 'Saved Albums'}</h2>
				<div class="grid recent-grid">
					{#each shelf as album (album.id)}<AlbumCard {album} />{/each}
				</div>
			</section>
		{:else}
			<div class="empty">
				<Icon name="note" size={44} />
				<h3>Nothing saved yet</h3>
				<p>Star albums and songs, or make a playlist, and they appear here.</p>
			</div>
		{/if}
	{:catch}
		<div class="empty">
			<Icon name="note" size={44} />
			<h3>Couldn’t load your library</h3>
			<p>Check the connection and try again.</p>
		</div>
	{/await}
</div>

<style>
	.head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding-right: var(--gutter);
		margin-bottom: 18px;
	}
	.head .page-title {
		margin-bottom: 0;
	}

	.rows {
		list-style: none;
		margin: 0 0 26px;
		padding: 0 0 0 var(--gutter);
	}
	.rows a {
		display: flex;
		align-items: center;
		gap: 12px;
		height: 44px;
		font-size: 17px;
		font-weight: 500;
		border-bottom: 0.5px solid var(--hairline);
		padding-right: var(--gutter);
	}
	.rows a :global(svg:first-child) {
		color: var(--accent);
	}
	.rows span {
		flex: 1;
	}
	.rows :global(.chev) {
		color: var(--text-3);
	}

	.recent {
		margin-bottom: 34px;
	}
	.section-title {
		margin: 0 var(--gutter) 14px;
		font-size: 20px;
		font-weight: 700;
		letter-spacing: -0.01em;
		color: var(--text);
	}
	.recent-grid {
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: 20px 14px;
	}

	.empty {
		display: grid;
		justify-items: center;
		gap: 8px;
		text-align: center;
		padding: 48px var(--gutter);
		color: var(--text-2);
	}
	.empty :global(svg) {
		color: var(--text-3);
	}
	.empty h3 {
		margin: 4px 0 0;
		font-size: 17px;
		color: var(--text);
	}
	.empty p {
		margin: 0;
		font-size: 14px;
		max-width: 280px;
	}
</style>
