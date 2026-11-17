<script lang="ts">
	import { untrack } from "svelte";
	import {
		allNow,
		cachedNow,
		getAlbumList,
		getLastfmRecommendations,
		getRandomSongs,
		getRecentlyPlayedSongs,
		getTodaysHits,
		getTrendingSongs,
		getTrendingPlaylists,
		getYtMusicNewReleases,
		optional,
	} from "../api";
	import AlbumCard from "../components/AlbumCard.svelte";
	import ArtistCard from "../components/ArtistCard.svelte";
	import ErrorState from "../components/ErrorState.svelte";
	import Icon from "../components/Icon.svelte";
	import ProfileButton from "../components/ProfileButton.svelte";
	import PlaylistCard from "../components/PlaylistCard.svelte";
	import Shelf from "../components/Shelf.svelte";
	import SongCard from "../components/SongCard.svelte";
	import TrackList from "../components/TrackList.svelte";
	import { getPlayer } from "../player.svelte";
	import type { Album, Artist, Playlist, Song } from "../types";
	import type { LastfmShelf, Maybe } from "../api";

	const player = getPlayer();

	// Library shelves paint from the on-device copy without waiting on the network.
	// frequent shares Browse's cache key; random gets its own so the two tabs differ.
	// Revisited in the same session, everything below is a plain value, so the page paints
	// its last state in the same frame and only re-renders where the server differs.
	type Shelves = { newest: Album[]; frequent: Album[]; random: Album[] };
	function load(): Maybe<Shelves> {
		const shelves = allNow([
			cachedNow("home:newest", () => getAlbumList("newest", 20), {
				fresh: 0,
				// Patch the resolved value in place: wrapping it in a promise would re-show the spinner.
				refresh: (newest) => {
					const current = data;
					if (current instanceof Promise) void current.then((d) => (data = { ...d, newest }));
					else data = { ...current, newest };
				},
			}),
			optional(cachedNow("browse:frequent", () => getAlbumList("frequent", 20))),
			optional(cachedNow("home:random", () => getAlbumList("random", 20))),
		]);
		const assemble = ([newest, frequent, random]: [Album[], Album[], Album[]]) => ({ newest, frequent, random });
		return shelves instanceof Promise ? shelves.then(assemble) : assemble(shelves);
	}

	let data = $state(load());

	// Recently played and favorites change often: shown from the cached copy at once and
	// swapped when the server differs. Picks are meant to be random every visit. None of
	// these hold back the shelves above.
	let recent = $state(
		optional(
			cachedNow("home:recent:songs:v2", () => getRecentlyPlayedSongs(20), {
				fresh: 0,
				refresh: (list) => (recent = list),
			}),
		),
	);
	let starred = $state(
		optional(
			cachedNow("home:starred", () => getAlbumList("starred", 20), {
				fresh: 0,
				refresh: (list) => (starred = list),
			}),
		),
	);
	// Last visit's picks paint at once (so nothing below shifts when scroll is restored on
	// back); a fresh random set swaps into the same slot once it arrives.
	let picks = $state(
		optional(
			cachedNow("home:picks", () => getRandomSongs(12), {
				fresh: 0,
				refresh: (list) => (picks = list),
			}),
		),
	);

	// Charts come from YouTube Music / Spotify and can be slow on a cold cache, so
	// they load on their own and never hold back (or break) the library shelves.
	const region = /-([A-Z]{2})\b/i.exec(navigator.language)?.[1]?.toUpperCase();
	let trending = $state(
		optional(
			cachedNow(`home:trending:12:${region ?? ""}`, () => getTrendingSongs(12, region), {
				refresh: (list) => (trending = list),
			}),
		),
	);
	let hits = $state(
		optional(cachedNow("home:hits:spotify:12", () => getTodaysHits(12), { refresh: (list) => (hits = list) })),
	);
	let newReleases = $state(
		optional(cachedNow("home:new-releases", () => getYtMusicNewReleases(20), {
			refresh: (list) => (newReleases = list),
		})),
	);
	let popularPlaylists = $state(
		optional(cachedNow("home:popular-playlists", () => getTrendingPlaylists(), {
			refresh: (list) => (popularPlaylists = list),
		})),
	);

	// Personal picks from the user's Last.fm history (empty when not linked). A cold
	// build takes the server a while, so the last copy paints first and swaps when ready.
	let forYou = $state<Maybe<LastfmShelf[]>>(
		optional(
			// "home:lastfm:v2": copies cached as "home:lastfm" are the empty answers of the
			// old server stub, and would stay "fresh" for half an hour.
			cachedNow("home:lastfm:v2", () => getLastfmRecommendations(), {
				fresh: 30 * 60_000,
				refresh: (list) => (forYou = list),
			}),
		),
	);

	// ---- section plan ------------------------------------------------------------
	// Every source resolves on its own; the page is laid out from whatever has arrived,
	// so a missing shelf (Last.fm not linked, an empty chart) never leaves two of the
	// same layout back to back.
	type Reasoned<T> = T & { reason?: string };
	type Section = { key: string; title: string; subtitle?: string } & (
		| { kind: "list"; songs: Song[] }
		| { kind: "recent"; songs: Song[] }
		| { kind: "artists"; artists: Reasoned<Artist>[] }
		| { kind: "albums"; albums: Reasoned<Album>[]; seeAll?: string }
		| { kind: "playlists"; playlists: Playlist[] }
	);

	const ready = $state<{
		recent?: Song[];
		starred?: Album[];
		picks?: Song[];
		trending?: Song[];
		hits?: Song[];
		newReleases?: Album[];
		popularPlaylists?: Playlist[];
		forYou?: LastfmShelf[];
	}>({});

	/** Mirror a cached-or-pending source into `ready` (latest assignment wins). */
	function follow<K extends keyof typeof ready>(key: K, get: () => Maybe<NonNullable<(typeof ready)[K]>>) {
		// A cached copy is a plain value: take it now, so a revisit paints in its first frame.
		const initial = get();
		if (!(initial instanceof Promise)) ready[key] = initial;
		$effect(() => {
			const source = get();
			if (source instanceof Promise) {
				source.then(
					(value) => {
						if (get() === source) ready[key] = value;
					},
					() => {},
				);
			} else ready[key] = source;
		});
	}
	follow("recent", () => recent);
	follow("starred", () => starred);
	follow("picks", () => picks);
	follow("trending", () => trending);
	follow("hits", () => hits);
	follow("newReleases", () => newReleases);
	follow("popularPlaylists", () => popularPlaylists);
	follow("forYou", () => forYou);

	const firstData = untrack(() => data);
	let shelves = $state<Shelves | null>(firstData instanceof Promise ? null : firstData);
	let shelvesError = $state<unknown>(null);
	$effect(() => {
		const source = data;
		shelvesError = null;
		if (source instanceof Promise) {
			source.then(
				(value) => {
					if (data === source) shelves = value;
				},
				(error) => {
					if (data === source) shelvesError = error;
				},
			);
		} else shelves = source;
	});

	/** Track lists vs. card shelves: the two layouts that shouldn't repeat. */
	const isList = (s: Section) => s.kind === "list";

	/**
	 * Highest priority first, but always taking the next section of the other layout
	 * when there is one. With more shelves than lists a repeat is unavoidable; it
	 * then falls at the end, among the least personal shelves.
	 */
	function interleave(sections: Section[]): Section[] {
		const rest = sections.filter(Boolean);
		const out: Section[] = [];
		while (rest.length) {
			const last = out.at(-1);
			let i = last ? rest.findIndex((s) => isList(s) !== isList(last)) : 0;
			if (i < 0) i = 0;
			out.push(...rest.splice(i, 1));
		}
		return out;
	}

	function forYouSection(kind: LastfmShelf["kind"]): Section | null {
		const shelf = ready.forYou?.find((s) => s.kind === kind);
		if (!shelf) return null;
		const base = { key: shelf.key, title: shelf.title, subtitle: shelf.subtitle };
		if (shelf.kind === "song") return shelf.song.length ? { ...base, kind: "list", songs: shelf.song } : null;
		if (shelf.kind === "artist") return shelf.artist.length ? { ...base, kind: "artists", artists: shelf.artist } : null;
		return shelf.album.length ? { ...base, kind: "albums", albums: shelf.album } : null;
	}

	function plan(d: Shelves | null): Section[] {
		const list = (key: string, title: string, songs?: Song[]): Section | null =>
			songs?.length ? { key, title, kind: "list", songs } : null;
		const albums = (key: string, title: string, items?: Album[], seeAll?: string): Section | null =>
			items?.length ? { key, title, kind: "albums", albums: items, seeAll } : null;
		// The library shelves only make sense on a server with a library of its own.
		const library = !!d?.newest.length;
		const candidates: (Section | null)[] = [
			ready.recent?.length ? { key: "recent", title: "Recently Played", kind: "recent", songs: ready.recent } : null,
			forYouSection("song"),
			forYouSection("artist"),
			list("hits", "Today's Hits", ready.hits),
			forYouSection("album"),
			list("trending", "Trending Songs", ready.trending),
			...(library
				? [
						albums("newest", "Recently Added", d!.newest, "#/recent"),
						list("picks", "Top Picks for You", ready.picks),
						albums("frequent", "Heavy Rotation", d!.frequent),
						albums("starred", "Favorite Albums", ready.starred, "#/albums"),
						albums("random", "Rediscover", d!.random, "#/albums"),
					]
				: []),
			ready.popularPlaylists?.length
				? { key: "playlists", title: "Popular Playlists", kind: "playlists", playlists: ready.popularPlaylists }
				: null,
			albums("new-releases", "New Releases", ready.newReleases),
		];
		return interleave(candidates.filter((s): s is Section => !!s));
	}

	function mobilePages(list: Song[]): Song[][] {
		const pages: Song[][] = [];
		for (let i = 0; i < list.length; i += 4) pages.push(list.slice(i, i + 4));
		return pages;
	}
</script>

{#snippet songs(title: string, list: Song[], subtitle?: string)}
	{#if list.length}
		<section class="picks">
			<div class="picks-head">
				<div class="picks-heading">
					<h2 class="section-title">{title}</h2>
					{#if subtitle}<p class="picks-subtitle">{subtitle}</p>{/if}
				</div>
				<button
					class="btn secondary"
					onclick={() => player.playList(list)}
					><Icon name="play" size={14} />Play</button
				>
			</div>
			<div class="pad picks-list desktop-picks">
				<TrackList songs={list} showAlbum={false} />
			</div>
			<div class="pad mobile-picks">
				<div class="mobile-picks-scroller">
					{#each mobilePages(list) as page, i}
						<div class="mobile-picks-page" class:last={i === mobilePages(list).length - 1}>
							<TrackList songs={page} showAlbum={false} />
						</div>
					{/each}
				</div>
			</div>
		</section>
	{/if}
{/snippet}

{#snippet section(s: Section)}
	{#if s.kind === "list"}
		{@render songs(s.title, s.songs, s.subtitle)}
	{:else if s.kind === "recent"}
		<Shelf title={s.title} size="lg">
			{#each s.songs as song, i (song.id)}
				<SongCard {song} queue={s.songs} index={i} />
			{/each}
		</Shelf>
	{:else if s.kind === "artists"}
		<Shelf title={s.title} subtitle={s.subtitle} size="artist">
			{#each s.artists as artist (artist.id)}
				<div class="reasoned">
					<ArtistCard {artist} />
					{#if artist.reason}<span class="reason">{artist.reason}</span>{/if}
				</div>
			{/each}
		</Shelf>
	{:else if s.kind === "albums"}
		<Shelf title={s.title} subtitle={s.subtitle} seeAll={s.seeAll}>
			{#each s.albums as album (album.id)}
				{#if album.reason}
					<div class="reasoned album">
						<AlbumCard {album} />
						<span class="reason">{album.reason}</span>
					</div>
				{:else}
					<AlbumCard {album} />
				{/if}
			{/each}
		</Shelf>
	{:else}
		<Shelf title={s.title}>
			{#each s.playlists as playlist (playlist.id)}<PlaylistCard {playlist} />{/each}
		</Shelf>
	{/if}
{/snippet}

<div class="page">
	<div class="head">
		<h1 class="page-title">Home</h1>
		<div class="head-actions">
			<ProfileButton />
		</div>
	</div>
	<p class="greet muted"></p>

	{#if shelvesError}
		<ErrorState error={shelvesError} retry={() => (data = load())} />
	{:else if !shelves}
		<div class="spinner"></div>
	{/if}
	{#if shelves || shelvesError}
		{#each plan(shelves) as s (s.key)}
			{@render section(s)}
		{/each}
	{/if}
</div>

<style>
	.head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding-right: var(--gutter);
	}
	.head .page-title {
		margin-bottom: 0;
	}
	.head-actions {
		display: flex;
		align-items: center;
		gap: 10px;
	}
	.greet {
		margin: 2px var(--gutter) 22px;
		font-size: 15px;
	}
	.picks {
		margin-bottom: 34px;
	}
	@media (max-width: 699px) {
		.picks {
			margin-bottom: 26px;
		}
	}
	.picks-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding-right: var(--gutter);
		margin-bottom: 12px;
	}
	.picks-head :global(.section-title) {
		margin-bottom: 0;
	}
	.picks-heading {
		min-width: 0;
	}
	.picks-subtitle {
		margin: 3px var(--gutter) 0;
		font-size: 13px;
		line-height: 1.35;
		color: var(--text-2);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	/* Apple Music-style caption under a recommended artist: why it's there. */
	/* stretch, not center: the card must fill the column or its artwork shrinks
	   to the width of the name below it */
	.reasoned {
		display: flex;
		flex-direction: column;
		align-items: stretch;
		gap: 2px;
		min-width: 0;
	}
	.reason {
		max-width: 100%;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: 12px;
		color: var(--text-2);
		text-align: center;
	}
	/* Album card titles are left-aligned: keep the caption under them. */
	.reasoned.album .reason {
		text-align: left;
	}
	.picks-head .btn {
		min-width: 0;
		height: 28px;
		padding: 0 12px;
		font-size: 13px;
	}
	.mobile-picks {
		display: none;
	}
	/* two columns of rows on wide screens, like Apple Music's song shelves */
	@media (min-width: 1100px) {
		.picks-list :global(.tracks) {
			display: grid;
			grid-template-columns: 1fr 1fr;
			column-gap: 24px;
		}
	}

	@media (max-width: 699px) {
		.desktop-picks {
			display: none;
		}

		.mobile-picks {
			display: block;
			overflow: hidden;
		}

		.mobile-picks-scroller {
			display: flex;
			gap: 12px;
			overflow-x: auto;
			scroll-snap-type: x mandatory;
			scrollbar-width: none;
			overscroll-behavior-x: contain;
		}

		.mobile-picks-scroller::-webkit-scrollbar {
			display: none;
		}

		.mobile-picks-page {
			/* Leave a small preview of the next four-song column visible. */
			flex: 0 0 calc(100% - 28px);
			scroll-snap-align: start;
		}

		.mobile-picks-page.last {
			flex-basis: 100%;
		}
	}
</style>
