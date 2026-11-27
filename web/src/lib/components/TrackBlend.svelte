<script lang="ts">
	import type { Snippet } from 'svelte';
	import { fadeOnly } from '../motion';
	import { getPlayer } from '../player.svelte';
	import type { Song } from '../types';

	let { children }: { children: Snippet<[Song]> } = $props();
	const player = getPlayer();
	const mix = $derived(player.transition);
	const current = $derived(player.current);
	const tracks = $derived(current && mix && current.id !== mix.song.id
		? [current, mix.song] : current ? [current] : []);
	// Opening the sheet mid-blend joins the fade already playing on the dock.
	const elapsed = $derived(mix ? Math.max(0, performance.now() - mix.startedAt) : 0);

	function arrive(node: Element, id: string) {
		return fadeOnly(node, { duration: mix?.song.id === id ? 0 : 360 });
	}

	function leave(node: HTMLElement) {
		const opacity = Number(getComputedStyle(node).opacity);
		if (!node.classList.contains('mixing')) {
			// Hold the outgoing cover under the arriving one throughout a manual
			// skip. Fading both layers at once shows the dark surface between them.
			return { ...fadeOnly(node, { duration: 360 }), css: () => `opacity:${opacity}` };
		}
		// A cancelled preview returns to the current cover from its visible opacity.
		const fade = fadeOnly(node, { duration: 240 });
		return { ...fade, css: (t: number) => `opacity:${opacity * t}` };
	}
</script>

{#each tracks as track (track.id)}
	<div
		class="track-layer"
		class:mixing={mix?.song.id === track.id && current?.id !== track.id}
		style:--mix-duration={mix ? `${mix.duration}s` : undefined}
		style:--mix-delay={mix ? `${-elapsed}ms` : undefined}
		in:arrive={track.id}
		out:leave
	>
		{@render children(track)}
	</div>
{/each}

<style>
	.track-layer {
		position: absolute;
		inset: 0;
		z-index: 1;
		border-radius: inherit;
		pointer-events: none;
	}
	.mixing {
		z-index: 2;
		animation: dissolve var(--mix-duration) linear var(--mix-delay) both;
	}
	.track-layer:global([inert]):not(.mixing) { z-index: 0; }
	@keyframes dissolve {
		from { opacity: 0; }
		to { opacity: 1; }
	}
	@media (prefers-reduced-motion: reduce) {
		.mixing { animation: none; opacity: 0; }
	}
</style>
