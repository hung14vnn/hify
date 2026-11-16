<script lang="ts">
	import { cachedNow, getGenres } from '../api';
	import ErrorState from '../components/ErrorState.svelte';
	import GenreTiles from '../components/GenreTiles.svelte';

	function load() {
		return cachedNow('genres', () => getGenres());
	}
	let genres = $state(load());
</script>

<div class="page">
	<h1 class="page-title">Browse</h1>
	{#await genres}
		<div class="spinner"></div>
	{:then list}
		<GenreTiles genres={list} />
	{:catch error}
		<ErrorState {error} retry={() => (genres = load())} />
	{/await}
</div>
