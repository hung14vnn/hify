// Track-change paths of the Player, driven through a fake <audio> (jsdom has no media
// playback).
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Song } from './types';
import type { AutoMixInfo } from './api';

type LocalCopy = { url: string; revoke: () => void };
const getSession = vi.fn<() => { username: string } | null>();
const getOffline = vi.fn<(_user: string, _id: string) => Promise<LocalCopy | null>>();
const getAutoMixInfo = vi.fn<(_id: string, _token: string, _signal: AbortSignal) => Promise<AutoMixInfo | null>>();
vi.mock('./offline', () => ({ createOfflineTrackUrl: getOffline }));
vi.mock('./api', () => ({
	getPlayQueue: vi.fn(async () => null),
	savePlayQueue: vi.fn(),
	scrobble: vi.fn(),
	streamUrl: (id: string, _bust?: number, _offset?: number, _format?: string, token?: string) =>
		`https://music.test/rest/stream?id=${id}${token ? `&autoMix=1&mixToken=${token}` : ''}`,
	coverUrl: () => undefined,
	getAutoMixInfo,
	getSession
}));

const sleepTimer = { onTrackEnded: vi.fn(() => false), isEndOfTrack: false };
vi.mock('./sleepTimer.svelte', () => ({ sleepTimer }));

class FakeAudio extends EventTarget {
	static last: FakeAudio;
	preload = '';
	paused = true;
	currentTime = 0;
	duration = NaN;
	volume = 1;
	muted = false;
	playbackRate = 1;
	readyState = 0;
	error: { code: number; message: string } | null = null;
	src = '';
	/** Like the media element load algorithm: drop the data and stop. */
	load = vi.fn(() => {
		this.readyState = 0;
		this.error = null;
		this.paused = true;
	});
	play = vi.fn(() => {
		this.paused = false;
		this.dispatchEvent(new Event('play'));
		if (this.readyState < 3) this.dispatchEvent(new Event('waiting'));
		return Promise.resolve();
	});
	pause = vi.fn(() => {
		if (this.paused) return;
		this.paused = true;
		this.dispatchEvent(new Event('pause'));
	});
	autoplay = false;
	ended = false;
	style: Record<string, string> = {};
	remove = vi.fn();
	/** Healthy forward buffer: the prefetch guard skips only a stream that is starving. */
	buffered = {
		length: 1,
		start: () => 0,
		end: () => this.currentTime + 120
	};
	static all: FakeAudio[] = [];
	constructor() {
		super();
		FakeAudio.last = this;
		FakeAudio.all.push(this);
	}
	setAttribute() {}
	getAttribute(name: string) {
		return name === 'src' && this.src ? this.src : null;
	}
	removeAttribute(name: string) {
		if (name === 'src') this.src = '';
	}
	/** The new source has data: `canplay`. */
	ready() {
		this.readyState = 4;
		this.fire('canplay');
	}
	startPlaying() {
		this.ready();
		this.fire('playing');
	}
	fire(type: string) {
		this.dispatchEvent(new Event(type));
	}
}

class FakeAudioSession extends EventTarget {
	type = 'auto';
	state: 'inactive' | 'active' | 'interrupted' = 'active';
	set(state: FakeAudioSession['state']) {
		this.state = state;
		this.dispatchEvent(new Event('statechange'));
	}
}

class FakeWebAudioOutput {
	attach() {}
	setVolume() {}
	setTrim() {}
	quickFadeIn() {}
	silenceFade() {}
	crossfade() {}
	resetFade(_el: HTMLAudioElement) {}
	trackChanged() {}
	resume() {}
	dispose() {}
}

const song = (id: string, duration = 200) => ({ id, title: id, duration }) as Song;
const flush = () => new Promise((r) => setTimeout(r, 0));
/** A response big enough to pass the prefetch's "not an error page" check. */
const audioResponse = () => new Response(new Uint8Array(20_000));
function deferred<T>() {
	let resolve!: (value: T) => void;
	const promise = new Promise<T>((done) => (resolve = done));
	return { promise, resolve };
}

/** Play to `t` seconds on `audio`, as the element's clock would report it. */
function playTo(audio: FakeAudio, t: number) {
	audio.currentTime = t;
	audio.fire('timeupdate');
}

async function freshPlayer() {
	vi.resetModules();
	delete (globalThis as { __musicPlayer?: unknown }).__musicPlayer;
	const { getPlayer } = await import('./player.svelte');
	const player = getPlayer();
	// Finish the constructor's preload before asking Vitest for the same dynamic mock.
	if (getSession()) await vi.dynamicImportSettled();
	return { player, audio: FakeAudio.last };
}

beforeEach(() => {
	localStorage.clear();
	getSession.mockReset().mockReturnValue(null);
	getOffline.mockReset().mockResolvedValue(null);
	getAutoMixInfo.mockReset().mockResolvedValue(null);
	FakeAudio.all = [];
	vi.stubGlobal('Audio', FakeAudio);
	// The player mounts its elements in the document; the fake is not a DOM node.
	vi.spyOn(document.body, 'appendChild').mockImplementation((n) => n);
	vi.stubGlobal('fetch', vi.fn(async () => new Response('')));
	let n = 0;
	URL.createObjectURL = vi.fn(() => `blob:test/${++n}`);
	URL.revokeObjectURL = vi.fn();
	sleepTimer.onTrackEnded.mockReset().mockReturnValue(false);
	sleepTimer.isEndOfTrack = false;
});

afterEach(() => {
	(globalThis as { __musicPlayer?: { destroy(): void } }).__musicPlayer?.destroy();
	vi.unstubAllGlobals();
});

describe('Player track changes', () => {
	it.each([200, 400])('gives a cold %i-second stream the first request, then prepares its successor', async (duration) => {
		getSession.mockReturnValue({ username: 'listener' });
		const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) =>
			new Response(new Uint8Array(20_000), { headers: { 'content-type': 'audio/flac' } }));
		vi.stubGlobal('fetch', fetchMock);
		const { player, audio } = await freshPlayer();
		audio.play.mockImplementationOnce(() => {
			audio.paused = false;
			audio.fire('play');
			return new Promise(() => {});
		});
		player.playList([song('st-selected', duration), song('yt-next')]);
		await vi.dynamicImportSettled();
		expect(getOffline.mock.calls.map(([, id]) => id)).toEqual(['st-selected', 'yt-next']);
		expect(audio.src).toContain('id=st-selected');
		expect(audio.play).toHaveBeenCalledOnce();
		audio.ready();
		audio.fire('timeupdate');
		expect(fetchMock).not.toHaveBeenCalled();

		audio.fire('playing');
		await flush();
		expect(player.current?.suffix).toBe('flac');
		expect(fetchMock.mock.calls.map(([url, init]) => [new URL(url).searchParams.get('id'), init?.headers])).toEqual([
			['st-selected', { Range: 'bytes=0-1' }],
			['yt-next', duration > 300 ? { Range: 'bytes=0-1' } : undefined]
		]);
		player.pause();
		player.resume();
		audio.fire('playing');
		await flush();
		expect(fetchMock).toHaveBeenCalledTimes(2);
	});

	it('cancels a stale codec result on quality reload and learns the resumed stream when playing is missed', async () => {
		const old = deferred<Response>();
		const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) =>
			new Response('', { headers: { 'content-type': 'audio/flac' } }));
		fetchMock.mockImplementationOnce(() => old.promise);
		vi.stubGlobal('fetch', fetchMock);
		const { player, audio } = await freshPlayer();
		player.playList([song('yt-selected')]);
		await flush();
		audio.ready();
		audio.fire('playing');
		expect(fetchMock).toHaveBeenCalledOnce();
		player.pause();
		player.reloadCurrent();
		await flush();
		expect(fetchMock.mock.calls[0][1]?.signal?.aborted).toBe(true);
		audio.ready();
		audio.fire('playing'); // A queued notification after pause cannot start work.
		expect(fetchMock).toHaveBeenCalledOnce();

		const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true);
		try {
			player.resume(); // An already playable element can fulfill play() without another playing event.
			await flush();
			expect(fetchMock).toHaveBeenCalledTimes(2);
			expect(player.current?.suffix).toBe('flac');
			old.resolve(new Response('', { headers: { 'content-type': 'audio/mpeg' } }));
			await flush();
			expect(player.current?.suffix).toBe('flac');
		} finally {
			hidden.mockRestore();
		}
	});

	it('starts the tapped song: set src, load(), play(), spinner until canplay', async () => {
		const { player, audio } = await freshPlayer();
		// A play() that hasn't settled yet: the source is still loading.
		audio.play.mockImplementationOnce(() => {
			audio.paused = false;
			audio.fire('play');
			audio.fire('waiting');
			return new Promise(() => {});
		});
		player.playList([song('a'), song('b'), song('c')], 1);
		await flush();
		expect(player.current?.id).toBe('b');
		expect(audio.src).toContain('id=b');
		expect(audio.load).toHaveBeenCalled();
		// play() at once keeps the audio session alive while the source loads.
		expect(audio.play).toHaveBeenCalledTimes(1);
		expect(player.buffering).toBe(true);
		expect(player.active).toBe(true);
		audio.ready();
		expect(player.playing).toBe(true);
		expect(player.buffering).toBe(false);
	});

	it('plays on canplay when the early play() did not stick', async () => {
		const { player, audio } = await freshPlayer();
		player.playList([song('a')]);
		await flush();
		audio.paused = true;
		audio.play.mockClear();
		audio.ready();
		expect(audio.play).toHaveBeenCalledTimes(1);
		expect(player.current?.id).toBe('a');
	});

	it('retries a play() refused in the background once the app is visible', async () => {
		const { player, audio } = await freshPlayer();
		player.playList([song('a'), song('b')]);
		await flush();
		audio.ready();
		const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true);
		try {
			audio.play.mockImplementationOnce(() => Promise.reject(new DOMException('blocked', 'NotAllowedError')));
			audio.fire('ended');
			await flush();
			expect(player.current?.id).toBe('b');
			expect(player.active).toBe(false);
			audio.play.mockClear();
			hidden.mockReturnValue(false);
			document.dispatchEvent(new Event('visibilitychange'));
			expect(audio.play).toHaveBeenCalledTimes(1);
		} finally {
			hidden.mockRestore();
		}
	});

	it('downloads the next track in the last five minutes and plays it from memory', async () => {
		const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => audioResponse());
		vi.stubGlobal('fetch', fetchMock);
		const { player, audio } = await freshPlayer();
		player.playList([song('a', 400), song('b')]);
		await flush();
		audio.startPlaying();
		playTo(audio, 40);
		// Playback has started, but the whole-track download must wait for the lead window.
		expect(fetchMock).toHaveBeenCalledTimes(1);
		expect(fetchMock.mock.calls[0][1]).toMatchObject({ headers: { Range: 'bytes=0-1' } });
		fetchMock.mockClear();
		for (const t of [100, 101, 102]) playTo(audio, t);
		expect(fetchMock).toHaveBeenCalledTimes(1);
		expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining('id=b'), expect.anything());
		await flush();
		await flush();
		// The network may be gone by now: the change must not need it.
		audio.fire('ended');
		expect(player.current?.id).toBe('b');
		const active = FakeAudio.all.find((a) => a.src.startsWith('blob:') && !a.paused);
		expect(active).toBeDefined();
	});

	it('learns the codec from a complete successor download on retry and keeps it through the blob handoff', async () => {
		const fetchMock = vi.fn((_url: string, _init?: RequestInit) => Promise.resolve(audioResponse()));
		const headers = { 'content-type': 'audio/flac', 'x-audio-bit-depth': '24', 'x-audio-sample-rate': '96000' };
		fetchMock.mockResolvedValueOnce(new Response(new Uint8Array(20_000), {
			headers: { ...headers, 'content-length': '80000' }
		}));
		fetchMock.mockResolvedValueOnce(new Response(new Uint8Array(20_000), { headers }));
		vi.stubGlobal('fetch', fetchMock);
		const { player, audio } = await freshPlayer();
		vi.useFakeTimers();
		try {
			const original = { suffix: 'opus', bitDepth: 16, sampleRate: 48000 };
			player.playList([song('a'), { ...song('yt-b'), ...original }]);
			await vi.advanceTimersByTimeAsync(0);
			audio.startPlaying();
			await vi.advanceTimersByTimeAsync(0);
			expect(player.nextSong).toMatchObject(original);
			expect(URL.createObjectURL).not.toHaveBeenCalled();
			await vi.advanceTimersByTimeAsync(4_100);
			playTo(audio, 10);
			await vi.advanceTimersByTimeAsync(0);
			const resolved = { suffix: 'flac', bitDepth: 24, sampleRate: 96000 };
			expect(player.nextSong).toMatchObject(resolved);
			audio.fire('ended');
			const standby = FakeAudio.all[1];
			expect(standby.src).toMatch(/^blob:/);
			standby.startPlaying();
			expect(player.current).toMatchObject({ id: 'yt-b', ...resolved });
			expect(fetchMock.mock.calls.map(([url, init]) => [new URL(url).searchParams.get('id'), init?.headers])).toEqual([
				['yt-b', undefined], ['yt-b', undefined]
			]);
		} finally {
			vi.useRealTimers();
		}
	});

	it('ignores the old quality when its successor download finishes after the replacement', async () => {
		let oldBody!: ReadableStreamDefaultController<Uint8Array>;
		const oldStream = new ReadableStream<Uint8Array>({ start(controller) { oldBody = controller; } });
		const fetchMock = vi.fn((_url: string, _init?: RequestInit) => Promise.resolve(audioResponse()));
		fetchMock.mockResolvedValueOnce(new Response(oldStream, {
			headers: { 'content-type': 'audio/flac', 'x-audio-bit-depth': '24', 'x-audio-sample-rate': '96000' }
		}));
		fetchMock.mockResolvedValueOnce(new Response(new Uint8Array(20_000), { headers: { 'content-type': 'audio/mpeg' } }));
		vi.stubGlobal('fetch', fetchMock);
		const { player, audio } = await freshPlayer();
		player.playList([song('a'), { ...song('yt-b'), suffix: 'opus' }]);
		await flush();
		audio.startPlaying();
		await flush();
		expect(player.nextSong?.suffix).toBe('opus');
		player.reloadCurrent();
		expect(fetchMock.mock.calls[0][1]?.signal?.aborted).toBe(true);
		await flush();
		audio.startPlaying();
		await flush();
		oldBody.enqueue(new Uint8Array(20_000));
		oldBody.close();
		await flush();
		expect(URL.createObjectURL).toHaveBeenCalledOnce();
		expect(player.nextSong?.suffix).toBe('mp3');
		expect(player.nextSong?.bitDepth).toBeUndefined();
		expect(player.nextSong?.sampleRate).toBeUndefined();
	});

	it('retries a track that fails in the background after a pause, not at once', async () => {
		vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date', 'performance'] });
		const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true);
		try {
			const { player, audio } = await freshPlayer();
			player.playList([song('a'), song('b'), song('c')]);
			await vi.advanceTimersByTimeAsync(0);
			audio.ready();
			audio.fire('ended');
			await vi.advanceTimersByTimeAsync(0);
			expect(player.current?.id).toBe('b');
			audio.load.mockClear();
			audio.error = { code: 4, message: 'Format error' };
			audio.fire('error');
			await vi.advanceTimersByTimeAsync(0);
			expect(audio.load).not.toHaveBeenCalled();
			await vi.advanceTimersByTimeAsync(2_000);
			expect(player.current?.id).toBe('b');
			expect(audio.load).toHaveBeenCalledTimes(1);
		} finally {
			hidden.mockRestore();
			vi.useRealTimers();
		}
	});

	it('kicks a waiting element that already has enough data', async () => {
		const { player, audio } = await freshPlayer();
		player.playList([song('a')]);
		await flush();
		audio.ready();
		audio.play.mockClear();
		audio.fire('waiting');
		expect(audio.play).toHaveBeenCalledTimes(1);
		expect(player.current?.id).toBe('a');
	});

	it('shows paused, not playing, when the browser refuses play()', async () => {
		const { player, audio } = await freshPlayer();
		audio.play.mockImplementationOnce(() => Promise.reject(new DOMException('blocked', 'NotAllowedError')));
		player.playList([song('a')]);
		await flush();
		expect(player.playing).toBe(false);
		expect(player.buffering).toBe(false);
		expect(player.active).toBe(false);
	});

	it('does not start by itself on canplay after the user paused', async () => {
		const { player, audio } = await freshPlayer();
		player.playList([song('a')]);
		await flush();
		player.pause();
		audio.play.mockClear();
		audio.ready();
		expect(audio.play).not.toHaveBeenCalled();
		expect(player.active).toBe(false);
	});

	it('moves to the next song on ended', async () => {
		const { player, audio } = await freshPlayer();
		player.playList([song('a'), song('b')]);
		await flush();
		audio.fire('ended');
		await flush();
		expect(player.current?.id).toBe('b');
		expect(audio.src).toContain('id=b');
	});

	it('keeps only the latest of two quick track changes', async () => {
		const { player, audio } = await freshPlayer();
		player.playList([song('a'), song('b'), song('c')]);
		player.jumpTo(2);
		await flush();
		expect(player.current?.id).toBe('c');
		expect(audio.src).toContain('id=c');
	});

	it('restarts the same song on repeat-one', async () => {
		const { player, audio } = await freshPlayer();
		player.playList([song('a'), song('b')]);
		await flush();
		player.cycleRepeat();
		player.cycleRepeat();
		expect(player.repeat).toBe('one');
		audio.currentTime = 200;
		audio.play.mockClear();
		audio.fire('ended');
		expect(player.current?.id).toBe('a');
		expect(audio.currentTime).toBe(0);
		expect(audio.play).toHaveBeenCalled();
	});

	it('stops at the end of the queue with repeat off', async () => {
		const { player, audio } = await freshPlayer();
		player.playList([song('a')]);
		await flush();
		audio.fire('ended');
		expect(player.current?.id).toBe('a');
		expect(player.currentTime).toBe(0);
	});

	it('honours the sleep timer ending at the track end', async () => {
		const { player, audio } = await freshPlayer();
		player.playList([song('a'), song('b')]);
		await flush();
		sleepTimer.onTrackEnded.mockReturnValue(true);
		audio.fire('ended');
		await flush();
		expect(player.current?.id).toBe('a');
	});

	it('takes the duration from the media element once known', async () => {
		const { player, audio } = await freshPlayer();
		player.playList([song('a', 200)]);
		await flush();
		audio.duration = 201.5;
		audio.fire('durationchange');
		expect(player.duration).toBe(201.5);
	});
});

describe('AutoMix playback', () => {
	beforeEach(() => {
		vi.doMock('./playback/webAudioOutput', () => ({ WebAudioOutput: FakeWebAudioOutput }));
		localStorage.setItem('music.audio', JSON.stringify({ engine: 'webaudio', crossfade: true, autoMix: true }));
		vi.stubGlobal('fetch', vi.fn(async () => audioResponse()));
	});

	afterEach(() => vi.doUnmock('./playback/webAudioOutput'));

	it.each([
		{ intro: 5, outro: 9, lead: 5, adaptive: true },
		{ intro: 15, outro: 3, lead: 3, adaptive: true },
		{ intro: 50, outro: 40, lead: 12, adaptive: true },
		{ intro: 0.5, outro: 9, lead: 8, adaptive: false },
		{ intro: null, outro: 9, lead: 8, adaptive: false }
	])('uses a $lead-second overlap for an outro of $outro and intro of $intro', async ({ intro, outro, lead, adaptive }) => {
		getAutoMixInfo.mockImplementation(async (id) => id === 'a'
			? { introSeconds: 0, outroSeconds: outro }
			: intro === null ? null : { introSeconds: intro, outroSeconds: 0 });
		const { player, audio } = await freshPlayer();
		player.playList([song('a'), song('b'), song('c')]);
		await flush();
		expect(audio.src).toContain('autoMix=1');
		expect(getAutoMixInfo).not.toHaveBeenCalled();
		audio.startPlaying();
		await flush();
		expect(getAutoMixInfo.mock.calls.map(([id]) => id)).toEqual(['a', 'b']);
		const [outgoing, incoming] = getAutoMixInfo.mock.calls.map(([, token]) => token);
		expect(new URL(audio.src).searchParams.get('mixToken')).toBe(outgoing);
		expect(incoming).not.toBe(outgoing);
		expect(fetch).toHaveBeenCalledWith(`https://music.test/rest/stream?id=b&autoMix=1&mixToken=${incoming}`, expect.anything());
		const standby = FakeAudio.all[1];
		standby.ready();
		playTo(audio, 185);
		await flush();
		playTo(audio, 200 - lead - 0.1);
		expect(standby.play).not.toHaveBeenCalled();
		playTo(audio, 200 - lead);
		expect(standby.play).toHaveBeenCalledOnce();
		expect(player.transition).toBeNull();
		standby.fire('playing');
		expect(player.transition).toMatchObject({ song: { id: 'b' }, autoMix: adaptive });
		expect(player.transition?.duration).toBeCloseTo(lead, 1);
		expect(player.current?.id).toBe('a');
		audio.fire('ended');
		expect(player.transition).toBeNull();
		expect(player.current?.id).toBe('b');
		standby.startPlaying();
		await flush();
		expect(getAutoMixInfo.mock.calls.filter(([id]) => id === 'b').every(([, token]) => token === incoming)).toBe(true);
		expect(getAutoMixInfo.mock.calls.at(-1)?.[0]).toBe('c');
	});

	it('rechecks a missing analysis and ignores a result for a successor removed during its lookup', async () => {
		const old = deferred<AutoMixInfo>();
		let ready = false;
		getAutoMixInfo.mockImplementation(async (id) => {
			if (id === 'a') return { introSeconds: 0, outroSeconds: 11 };
			if (id === 'b') return old.promise;
			return ready ? { introSeconds: 4, outroSeconds: 0 } : null;
		});
		const { player, audio } = await freshPlayer();
		let now = 1_000;
		const clock = vi.spyOn(performance, 'now').mockImplementation(() => now);
		try {
			player.playList([song('a'), song('b')]);
			await flush();
			audio.startPlaying();
			await flush();
			const oldSignal = getAutoMixInfo.mock.calls[1][2];
			player.playNext([song('c')]);
			await flush();
			expect(oldSignal.aborted).toBe(true);
			old.resolve({ introSeconds: 10, outroSeconds: 0 });
			ready = true;
			playTo(audio, 180);
			expect(getAutoMixInfo.mock.calls.map(([id]) => id)).toEqual(['a', 'b', 'c']);
			now += 6_000;
			playTo(audio, 190);
			await flush();
			expect(getAutoMixInfo.mock.calls.map(([id]) => id)).toEqual(['a', 'b', 'c', 'a', 'c']);
			const standby = FakeAudio.all[1];
			playTo(audio, 195.9);
			expect(standby.play).not.toHaveBeenCalled();
			playTo(audio, 196);
			standby.fire('playing');
			expect(player.transition).toMatchObject({ song: { id: 'c' }, duration: 4, autoMix: true });
			player.pause();
			expect(standby.paused).toBe(true);
			expect(player.transition).toBeNull();
		} finally {
			clock.mockRestore();
		}
	});

	it.each(['invalidated', 'timed out', 'still waiting'])('uses the fixed fade when a previously analyzed stream is %s near handoff', async (reason) => {
		const late = deferred<AutoMixInfo>();
		const cue = { introSeconds: 12, outroSeconds: 12 };
		let rangeChanged = false;
		getAutoMixInfo.mockImplementation(async (id) => {
			if (id !== 'a' || !rangeChanged) return cue;
			return reason === 'invalidated' ? null : late.promise;
		});
		const { player, audio } = await freshPlayer();
		player.playList([song('a'), song('b')]);
		await flush();
		expect(getAutoMixInfo).not.toHaveBeenCalled();
		audio.startPlaying();
		await flush();
		const standby = FakeAudio.all[1];
		expect(getAutoMixInfo.mock.calls.map(([id]) => id)).toEqual(['a', 'b']);
		rangeChanged = true;
		vi.useFakeTimers();
		try {
			playTo(audio, 180);
			playTo(audio, 185);
			playTo(audio, 185.2);
			await vi.advanceTimersByTimeAsync(reason === 'still waiting' ? 0 : 2500);
			expect(getAutoMixInfo.mock.calls.map(([id]) => id)).toEqual(['a', 'b', 'a']);
			if (reason === 'timed out') {
				expect(getAutoMixInfo.mock.calls[2][2].aborted).toBe(true);
				late.resolve(cue);
				await vi.advanceTimersByTimeAsync(0);
			}
			playTo(audio, 188);
			expect(standby.play).not.toHaveBeenCalled();
			playTo(audio, 192);
			standby.fire('playing');
			expect(player.transition).toMatchObject({ song: { id: 'b' }, duration: 8, autoMix: false });
			if (reason === 'still waiting') {
				late.resolve(cue);
				await vi.advanceTimersByTimeAsync(0);
				expect(player.transition?.autoMix).toBe(false);
			}
			expect(getAutoMixInfo).toHaveBeenCalledTimes(3);
		} finally {
			vi.useRealTimers();
		}
	});

	it('turns on during playback and retains a prepared stream when the mode changes again', async () => {
		localStorage.setItem('music.audio', JSON.stringify({ engine: 'webaudio' }));
		vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(() => {})));
		const { player, audio } = await freshPlayer();
		const { audioSettings } = await import('./playback/audioSettings.svelte');
		player.playList([song('a'), song('b')]);
		await flush();
		audio.startPlaying();
		await flush();
		const selectedUrl = audio.src;
		expect(selectedUrl).not.toContain('autoMix');
		expect(getAutoMixInfo).not.toHaveBeenCalled();
		audioSettings.setAutoMix(true);
		expect(audioSettings.crossfade).toBe(true);
		expect(audio.src).toBe(selectedUrl);
		expect(getAutoMixInfo).not.toHaveBeenCalled();
		playTo(audio, 160);
		const standby = FakeAudio.all[1];
		standby.ready();
		expect(standby.src).toContain('autoMix=1');
		audioSettings.setCrossfade(false);
		expect(audioSettings.autoMix).toBe(false);
		audioSettings.flush();
		expect(JSON.parse(localStorage.getItem('music.audio')!)).toMatchObject({ crossfade: false, autoMix: false });
		audio.fire('ended');
		expect(player.current?.id).toBe('b');
		expect(standby.play).toHaveBeenCalledOnce();
		expect(standby.load).toHaveBeenCalledOnce();
	});

	it('keeps the standard engine on a cut and omits analysis requests for a saved AutoMix preference', async () => {
		localStorage.setItem('music.audio', JSON.stringify({ engine: 'element', crossfade: true, autoMix: true }));
		const { player, audio } = await freshPlayer();
		player.playList([song('a'), song('b')]);
		await flush();
		audio.startPlaying();
		await flush();
		playTo(audio, 194);
		expect(audio.src).not.toContain('autoMix');
		expect(fetch).toHaveBeenCalledWith('https://music.test/rest/stream?id=b', expect.anything());
		expect(FakeAudio.all[1].play).not.toHaveBeenCalled();
		expect(getAutoMixInfo).not.toHaveBeenCalled();
	});

	it('binds a fresh analysis to a replacement stream with the same song ID', async () => {
		const old = deferred<AutoMixInfo>();
		const { player, audio } = await freshPlayer();
		player.playList([song('a'), song('b')]);
		await flush();
		const first = new URL(audio.src).searchParams.get('mixToken');
		getAutoMixInfo.mockImplementation(async (id, token) => {
			if (id === 'a' && token === first) return old.promise;
			return { introSeconds: 10, outroSeconds: 3 };
		});
		audio.startPlaying();
		await flush();
		player.reloadCurrent();
		await flush();
		const second = new URL(audio.src).searchParams.get('mixToken');
		expect(second).toMatch(/^[a-zA-Z0-9_-]{1,96}$/);
		expect(second).not.toBe(first);
		expect(getAutoMixInfo.mock.calls.some(([, token]) => token === second)).toBe(false);
		audio.startPlaying();
		old.resolve({ introSeconds: 8, outroSeconds: 10 });
		await flush();
		expect(getAutoMixInfo.mock.calls.filter(([id]) => id === 'a').map(([, token]) => token)).toEqual([first, second]);
		const standby = FakeAudio.all[1];
		playTo(audio, 185);
		await flush();
		playTo(audio, 193);
		expect(standby.play).not.toHaveBeenCalled();
		playTo(audio, 197);
		standby.fire('playing');
		expect(player.transition).toMatchObject({ autoMix: true, song: { id: 'b' } });
	});

	it.each([2, 9])('bounds an unanalyzed fallback using the decoded %i-second successor', async (decoded) => {
		const { player, audio } = await freshPlayer();
		player.playList([song('a'), song('b', 200)]);
		await flush();
		audio.startPlaying();
		await flush();
		const standby = FakeAudio.all[1];
		standby.duration = decoded;
		standby.ready();
		playTo(audio, 193);
		expect(standby.play).not.toHaveBeenCalled();
		playTo(audio, 197);
		if (decoded === 2) {
			expect(standby.play).not.toHaveBeenCalled();
			audio.fire('ended');
			expect(player.transition).toBeNull();
			expect(player.current?.id).toBe('b');
		} else {
			standby.fire('playing');
			expect(player.transition).toMatchObject({ autoMix: false, song: { id: 'b' } });
			expect(player.transition?.duration).toBeCloseTo(3, 1);
		}
	});

	it('withdraws a silent preroll when the successor turns out to be shorter than its metadata', async () => {
		const { player, audio } = await freshPlayer();
		player.playList([song('a'), song('b')]);
		await flush();
		audio.startPlaying();
		await flush();
		const standby = FakeAudio.all[1];
		playTo(audio, 193);
		expect(standby.play).toHaveBeenCalledOnce();
		standby.duration = 2;
		standby.startPlaying();
		expect(standby.paused).toBe(true);
		expect(player.transition).toBeNull();
		expect(player.current?.id).toBe('a');
	});

	it('uses the fallback for a locally stored successor with no captured source token', async () => {
		getSession.mockReturnValue({ username: 'listener' });
		getOffline.mockImplementation(async (_user, id) => id === 'b' ? { url: 'blob:offline-b', revoke: vi.fn() } : null);
		getAutoMixInfo.mockResolvedValue({ introSeconds: 12, outroSeconds: 12 });
		const { player, audio } = await freshPlayer();
		player.playList([song('a'), song('b')]);
		await flush();
		audio.startPlaying();
		await flush();
		const standby = FakeAudio.all[1];
		expect(standby.src).toBe('blob:offline-b');
		playTo(audio, 191);
		expect(standby.play).not.toHaveBeenCalled();
		playTo(audio, 192);
		standby.fire('playing');
		expect(player.transition).toMatchObject({ autoMix: false, song: { id: 'b' } });
		expect(getAutoMixInfo.mock.calls.map(([id]) => id)).toEqual(['a', 'a']);
	});

	it('respects repeat-one, end-of-track sleep, seeks and a manual skip with an analyzed pair', async () => {
		getAutoMixInfo.mockResolvedValue({ introSeconds: 6, outroSeconds: 8 });
		const { player, audio } = await freshPlayer();
		player.playList([song('a'), song('b'), song('c')]);
		await flush();
		audio.duration = 200;
		audio.startPlaying();
		await flush();
		const standby = FakeAudio.all[1];
		player.cycleRepeat();
		player.cycleRepeat();
		playTo(audio, 196);
		expect(player.repeat).toBe('one');
		expect(standby.play).not.toHaveBeenCalled();
		sleepTimer.isEndOfTrack = true;
		player.cycleRepeat();
		await flush();
		playTo(audio, 197);
		expect(standby.play).not.toHaveBeenCalled();

		sleepTimer.isEndOfTrack = false;
		player.seek(180);
		playTo(audio, 180);
		await flush();
		playTo(audio, 185);
		await flush();
		playTo(audio, 195);
		standby.fire('playing');
		expect(player.transition).toMatchObject({ song: { id: 'b' }, autoMix: true });
		player.seek(100);
		expect(standby.paused).toBe(true);
		expect(player.transition).toBeNull();
		playTo(audio, 196);
		standby.fire('playing');
		player.next();
		expect(player.current?.id).toBe('b');
		expect(audio.paused).toBe(true);
		expect(player.transition).toBeNull();
	});
});

describe('Player platform fixes', () => {
	it('ends at the server length when iOS reports about double, advancing once', async () => {
		const { player, audio } = await freshPlayer();
		player.playList([song('a', 200), song('b'), song('c')]);
		await flush();
		audio.ready();
		audio.duration = 400;
		audio.fire('durationchange');
		expect(player.duration).toBe(200);
		audio.currentTime = 199.8;
		audio.fire('timeupdate');
		await flush();
		expect(player.current?.id).toBe('b');
		// No pause in between: on a hidden iOS page it ends the audio session and the
		// next track never loads.
		expect(audio.pause).not.toHaveBeenCalled();
		// A stale native ended for the old source must not skip 'b'.
		audio.fire('ended');
		await flush();
		expect(player.current?.id).toBe('c');
	});

	it('reloads at the current position when the clock freezes while playing', async () => {
		vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date', 'performance'] });
		try {
			const { player, audio } = await freshPlayer();
			player.playList([song('a')]);
			await vi.advanceTimersByTimeAsync(0);
			audio.ready();
			audio.currentTime = 42;
			audio.fire('timeupdate');
			audio.load.mockClear();
			await vi.advanceTimersByTimeAsync(10_000);
			expect(audio.load).not.toHaveBeenCalled();
			// 15 s without progress, then the 2 s error delay.
			await vi.advanceTimersByTimeAsync(10_000);
			expect(audio.load).toHaveBeenCalled();
			expect(player.current?.id).toBe('a');
			expect(player.currentTime).toBe(42);
		} finally {
			vi.useRealTimers();
		}
	});

	it('freezes the UI clock while hidden but keeps the real position for recovery', async () => {
		vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date', 'performance'] });
		const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(false);
		try {
			const { player, audio } = await freshPlayer();
			player.playList([song('a')]);
			await vi.advanceTimersByTimeAsync(0);
			audio.ready();
			audio.currentTime = 5;
			audio.fire('timeupdate');
			expect(player.currentTime).toBe(5);

			hidden.mockReturnValue(true);
			document.dispatchEvent(new Event('visibilitychange'));
			audio.currentTime = 30;
			audio.fire('timeupdate');
			// No reactive UI work with the screen off...
			expect(player.currentTime).toBe(5);
			// ...but the engine still knows where playback is, e.g. for the snapshot
			// written when the hidden page is closed.
			expect(player.position).toBe(30);
			window.dispatchEvent(new PageTransitionEvent('pagehide', { persisted: false }));
			expect(JSON.parse(localStorage.getItem('music.queue')!).time).toBe(30);

			hidden.mockReturnValue(false);
			document.dispatchEvent(new Event('visibilitychange'));
			expect(player.currentTime).toBe(30);

		} finally {
			hidden.mockRestore();
			vi.useRealTimers();
		}
	});

	it('keeps playing through long steady progress, then still catches a freeze', async () => {
		vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date', 'performance'] });
		try {
			const { player, audio } = await freshPlayer();
			player.playList([song('a')]);
			await vi.advanceTimersByTimeAsync(0);
			audio.ready();
			audio.load.mockClear();
			// A minute of normal playback: the self-rescheduling watchdog must not fire.
			for (let t = 1; t <= 60; t++) {
				audio.currentTime = t;
				audio.fire('timeupdate');
				await vi.advanceTimersByTimeAsync(1_000);
			}
			expect(audio.load).not.toHaveBeenCalled();
			// Then the clock freezes: 15 s quiet (+ up to one 2 s re-check) + the 2 s error delay.
			await vi.advanceTimersByTimeAsync(20_000);
			expect(audio.load).toHaveBeenCalled();
			expect(player.currentTime).toBe(60);
		} finally {
			vi.useRealTimers();
		}
	});

	it('does not count hidden time as a stall when the app comes back', async () => {
		vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date', 'performance'] });
		const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(false);
		try {
			const { player, audio } = await freshPlayer();
			player.playList([song('a')]);
			await vi.advanceTimersByTimeAsync(0);
			audio.ready();
			audio.currentTime = 10;
			audio.fire('timeupdate');
			audio.load.mockClear();
			// No progress while hidden (WebKit may suspend loading), shown again just
			// before the watchdog's next check: resuming re-arms it (play() resets the
			// clock), so the hidden stretch must not count towards the 15 s.
			hidden.mockReturnValue(true);
			document.dispatchEvent(new Event('visibilitychange'));
			await vi.advanceTimersByTimeAsync(14_000);
			hidden.mockReturnValue(false);
			document.dispatchEvent(new Event('visibilitychange'));
			await vi.advanceTimersByTimeAsync(5_000);
			expect(audio.load).not.toHaveBeenCalled();
			expect(player.current?.id).toBe('a');
		} finally {
			hidden.mockRestore();
			vi.useRealTimers();
		}
	});

	it('ignores a stall callback iOS queued before the app was hidden', async () => {
		vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date', 'performance'] });
		const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(false);
		const nativeSetTimeout = globalThis.setTimeout;
		const stalledCallbacks: (() => void)[] = [];
		const setTimeoutSpy = vi.spyOn(globalThis, 'setTimeout').mockImplementation(((callback: TimerHandler, delay?: number, ...args: unknown[]) => {
			if (delay === 15_000 && typeof callback === 'function') stalledCallbacks.push(callback as () => void);
			return nativeSetTimeout(callback, delay, ...args);
		}) as typeof setTimeout);
		try {
			const { player, audio } = await freshPlayer();
			player.playList([song('a')]);
			await vi.advanceTimersByTimeAsync(0);
			audio.ready();
			expect(stalledCallbacks).toHaveLength(1);

			hidden.mockReturnValue(true);
			document.dispatchEvent(new Event('visibilitychange'));
			hidden.mockReturnValue(false);
			document.dispatchEvent(new Event('visibilitychange'));
			audio.pause.mockClear();
			// Safari may deliver this callback even after clearTimeout() if it had
			// already queued it while the app was being backgrounded.
			stalledCallbacks[0]();
			expect(audio.pause).not.toHaveBeenCalled();
			expect(player.current?.id).toBe('a');
		} finally {
			setTimeoutSpy.mockRestore();
			hidden.mockRestore();
			vi.useRealTimers();
		}
	});

	it('reloads a next track that never reaches canplay', async () => {
		vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date', 'performance'] });
		try {
			const { player, audio } = await freshPlayer();
			player.playList([song('a'), song('b')]);
			await vi.advanceTimersByTimeAsync(0);
			audio.ready();
			audio.fire('ended');
			await vi.advanceTimersByTimeAsync(0);
			audio.load.mockClear();
			await vi.advanceTimersByTimeAsync(10_000);
			expect(audio.load).not.toHaveBeenCalled();
			await vi.advanceTimersByTimeAsync(15_000);
			expect(player.current?.id).toBe('b');
			expect(audio.load).toHaveBeenCalled();
		} finally {
			vi.useRealTimers();
		}
	});

	it('still reloads a stalled source when the watchdog pause aborts its pending play()', async () => {
		vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date', 'performance'] });
		try {
			const { player, audio } = await freshPlayer();
			// A cold stream: play() stays pending until the watchdog's pause aborts it,
			// which is what WebKit does with a play() that never got data.
			audio.play.mockImplementationOnce(() => {
				audio.paused = false;
				audio.fire('play');
				audio.fire('waiting');
				return new Promise((_, reject) => {
					audio.addEventListener('pause', () => reject(new DOMException('aborted', 'AbortError')));
				});
			});
			player.playList([song('a')]);
			await vi.advanceTimersByTimeAsync(0);
			audio.load.mockClear();
			await vi.advanceTimersByTimeAsync(15_100);
			expect(audio.pause).toHaveBeenCalled();
			// The aborted play() must not cancel the reload queued for the 2 s error delay.
			await vi.advanceTimersByTimeAsync(2_100);
			expect(audio.load).toHaveBeenCalled();
			expect(player.current?.id).toBe('a');
		} finally {
			vi.useRealTimers();
		}
	});

	it('reloads on Play after giving up on a stalled stream, instead of spinning forever', async () => {
		vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date', 'performance'] });
		try {
			const { player, audio } = await freshPlayer();
			// A dead stream: never reaches canplay, and (unlike a decode error) no audio.error.
			player.playList([song('a')]);
			await vi.advanceTimersByTimeAsync(0);
			// Stall → retry once (2 s later) → stall again → nothing to skip to: give up.
			await vi.advanceTimersByTimeAsync(15_100 + 2_100 + 15_100);
			expect(player.error).toBeTruthy();
			expect(player.buffering).toBe(false);
			expect(audio.error).toBeNull();

			audio.load.mockClear();
			player.resume();
			await vi.advanceTimersByTimeAsync(0);
			// A fresh request, not play() on the dead one.
			expect(audio.load).toHaveBeenCalled();
			expect(player.error).toBeNull();
		} finally {
			vi.useRealTimers();
		}
	});

	it('does not treat a slow start as a stall the moment it starts playing', async () => {
		vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date', 'performance'] });
		try {
			const { player, audio } = await freshPlayer();
			// A cold stream: play() is accepted but nothing plays for ~15 s.
			audio.play.mockImplementation(() => {
				audio.paused = false;
				audio.fire('play');
				audio.fire('waiting');
				return new Promise(() => {});
			});
			player.playList([song('a')]);
			await vi.advanceTimersByTimeAsync(14_900);
			audio.ready();
			audio.fire('playing');
			audio.load.mockClear();
			await vi.advanceTimersByTimeAsync(5_000);
			expect(audio.load).not.toHaveBeenCalled();
			expect(player.current?.id).toBe('a');
		} finally {
			vi.useRealTimers();
		}
	});

	it('does not reload while paused for a call', async () => {
		vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date', 'performance'] });
		const session = new FakeAudioSession();
		Object.defineProperty(navigator, 'audioSession', { value: session, configurable: true });
		try {
			const { player, audio } = await freshPlayer();
			player.playList([song('a')]);
			await vi.advanceTimersByTimeAsync(0);
			audio.ready();
			session.set('interrupted');
			audio.load.mockClear();
			await vi.advanceTimersByTimeAsync(30_000);
			expect(audio.load).not.toHaveBeenCalled();
			expect(audio.paused).toBe(true);
			expect(player.current?.id).toBe('a');
		} finally {
			delete (navigator as { audioSession?: unknown }).audioSession;
			vi.useRealTimers();
		}
	});

	it('pauses for a call and resumes afterwards', async () => {
		const session = new FakeAudioSession();
		Object.defineProperty(navigator, 'audioSession', { value: session, configurable: true });
		try {
			const { player, audio } = await freshPlayer();
			expect(session.type).toBe('playback');
			player.playList([song('a')]);
			await flush();
			audio.ready();
			session.set('interrupted');
			expect(audio.paused).toBe(true);
			audio.play.mockClear();
			session.set('active');
			expect(audio.play).toHaveBeenCalledTimes(1);
		} finally {
			delete (navigator as { audioSession?: unknown }).audioSession;
		}
	});

	it('stays paused after the call when the user paused during it', async () => {
		const session = new FakeAudioSession();
		Object.defineProperty(navigator, 'audioSession', { value: session, configurable: true });
		try {
			const { player, audio } = await freshPlayer();
			player.playList([song('a')]);
			await flush();
			audio.ready();
			session.set('interrupted');
			player.pause();
			audio.play.mockClear();
			session.set('active');
			expect(audio.play).not.toHaveBeenCalled();
		} finally {
			delete (navigator as { audioSession?: unknown }).audioSession;
		}
	});

	it('changes track synchronously while hidden instead of awaiting IndexedDB', async () => {
		const { player, audio } = await freshPlayer();
		player.playList([song('a'), song('b')]);
		await flush();
		const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true);
		try {
			audio.fire('ended');
			// No await: the src must already be set.
			expect(audio.src).toContain('id=b');
		} finally {
			// Restore only this spy: vi.restoreAllMocks() also rewinds the timer
			// globals that earlier fake-timer tests installed, leaving setTimeout
			// frozen for every later test.
			hidden.mockRestore();
		}
	});
});

describe('Player background track changes', () => {
	/** Plays 'a' until the next track 'b' is downloaded and loaded into the standby element. */
	async function withStandby(duration = 200) {
		vi.stubGlobal('fetch', vi.fn(async () => audioResponse()));
		const { player, audio } = await freshPlayer();
		player.playList([song('a', duration), song('b'), song('c')]);
		await flush();
		audio.startPlaying();
		playTo(audio, duration / 2);
		await flush();
		await flush();
		return { player, audio };
	}

	it('Android: starts the next track on a standby element before the current one ends', async () => {
		const { player, audio } = await withStandby();
		expect(FakeAudio.all).toHaveLength(2);
		const standby = FakeAudio.all[1];
		expect(standby.src).toMatch(/^blob:test\//);
		expect(standby.paused).toBe(true);

		playTo(audio, 199.6);
		expect(player.current?.id).toBe('b');
		expect(standby.play).toHaveBeenCalled();
		// The old element plays on until the new one actually plays: never a silent moment.
		expect(audio.paused).toBe(false);
		standby.ready();
		standby.fire('playing');
		expect(audio.paused).toBe(true);
		expect(audio.src).toBe('');
	});

	it('Android: ignores the old element after the handoff', async () => {
		const { player, audio } = await withStandby();
		playTo(audio, 199.6);
		// Its own late `ended` must not skip 'b'.
		audio.fire('ended');
		await flush();
		expect(player.current?.id).toBe('b');
	});

	it.each([
		{ engine: 'element', crossfade: false },
		{ engine: 'element', crossfade: true },
		{ engine: 'webaudio', crossfade: false }
	])('hidden Android, $engine with saved crossfade=$crossfade: hands off within the final second and cuts on playing', async (settings) => {
		const ua = vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue('Android');
		const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true);
		vi.doMock('./playback/webAudioOutput', () => ({ WebAudioOutput: FakeWebAudioOutput }));
		localStorage.setItem('music.audio', JSON.stringify(settings));
		try {
			const { player, audio } = await withStandby();
			const standby = FakeAudio.all[1];
			standby.ready();
			playTo(audio, 198.99);
			expect(standby.play).not.toHaveBeenCalled();
			playTo(audio, 199);
			expect(standby.play).toHaveBeenCalledOnce();
			expect(audio.paused).toBe(false);
			standby.fire('waiting');
			standby.ready();
			expect(audio.paused).toBe(false);
			standby.fire('playing');
			expect(audio.paused).toBe(true);
			expect(audio.src).toBe('');
			audio.fire('ended');
			expect(player.current?.id).toBe('b');
		} finally {
			vi.doUnmock('./playback/webAudioOutput');
			hidden.mockRestore();
			ua.mockRestore();
		}
	});

	it.each([
		{ platform: 'Android', hidden: false },
		{ platform: 'Desktop', hidden: true }
	])('keeps the usual element handoff on $platform with hidden=$hidden', async (settings) => {
		const ua = vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue(settings.platform);
		const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(settings.hidden);
		try {
			const { audio } = await withStandby();
			const standby = FakeAudio.all[1];
			standby.ready();
			playTo(audio, 199.39);
			expect(standby.play).not.toHaveBeenCalled();
			playTo(audio, 199.41);
			expect(standby.play).toHaveBeenCalledOnce();
		} finally {
			hidden.mockRestore();
			ua.mockRestore();
		}
	});

	it.each([
		{ duration: 200, lead: 8, blends: true },
		{ duration: 20, lead: 0.6, blends: false }
	])('hidden Android, Web Audio crossfade on: keeps the $lead-second transition for a $duration-second track', async ({ duration, lead, blends }) => {
		const ua = vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue('Android');
		const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true);
		vi.doMock('./playback/webAudioOutput', () => ({ WebAudioOutput: FakeWebAudioOutput }));
		localStorage.setItem('music.audio', JSON.stringify({ engine: 'webaudio', crossfade: true }));
		try {
			const { audio, player } = await withStandby(duration);
			const standby = FakeAudio.all[1];
			standby.ready();
			playTo(audio, duration - lead - 0.01);
			expect(standby.play).not.toHaveBeenCalled();
			playTo(audio, duration - lead + 0.01);
			expect(standby.play).toHaveBeenCalledOnce();
			standby.fire('playing');
			expect(player.crossfading).toBe(blends);
			expect(audio.paused).toBe(!blends);
			expect(player.current?.id).toBe(blends ? 'a' : 'b');
		} finally {
			vi.doUnmock('./playback/webAudioOutput');
			hidden.mockRestore();
			ua.mockRestore();
		}
	});

	it('hidden Android: a refused early play lets the old track finish and retries its successor on return', async () => {
		const ua = vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue('Android');
		const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true);
		try {
			const { player, audio } = await withStandby();
			const standby = FakeAudio.all[1];
			standby.ready();
			standby.play.mockRejectedValueOnce(new DOMException('blocked', 'NotAllowedError'));
			playTo(audio, 199);
			await flush();
			expect(standby.play).toHaveBeenCalledOnce();
			expect(audio.paused).toBe(false);
			audio.fire('ended');
			expect(audio.paused).toBe(true);
			expect(player.current?.id).toBe('b');
			hidden.mockReturnValue(false);
			document.dispatchEvent(new Event('visibilitychange'));
			expect(standby.play).toHaveBeenCalledTimes(2);
			expect(standby.paused).toBe(false);
		} finally {
			hidden.mockRestore();
			ua.mockRestore();
		}
	});

	it('iOS: keeps a single element and swaps its source', async () => {
		const userAgent = vi
			.spyOn(navigator, 'userAgent', 'get')
			.mockReturnValue('Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)');
		try {
			const { player, audio } = await withStandby();
			expect(FakeAudio.all).toHaveLength(1);
			audio.fire('ended');
			expect(player.current?.id).toBe('b');
			expect(audio.src).toMatch(/^blob:test\//);
		} finally {
			userAgent.mockRestore();
		}
	});

	it('Android screen off: uses the active element if standby never buffered, even without a final timeupdate', async () => {
		const ua = vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue('Android');
		const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true);
		try {
			vi.stubGlobal('fetch', vi.fn(async () => audioResponse()));
			const { player, audio } = await freshPlayer();
			player.playList([song('a'), song('b')]);
			await flush();
			audio.startPlaying(); // No timeupdate before ended.
			await flush();
			const standby = FakeAudio.all[1];
			expect(standby.src).toMatch(/^blob:/);
			expect(standby.readyState).toBe(0);
			const copy = standby.src;
			audio.fire('ended');
			expect(audio.src).toBe(copy);
			expect(standby.play).not.toHaveBeenCalled();
			expect(standby.src).toBe('');
		} finally {
			hidden.mockRestore();
			ua.mockRestore();
		}
	});

	it.each([true, false])('Android screen off (document.hidden=%s): starts an online successor while its local read is still pending', async (pageHidden) => {
		const ua = vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue('Android');
		const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(pageHidden);
		const pending = deferred<LocalCopy | null>();
		getSession.mockReturnValue({ username: 'listener' });
		getOffline.mockImplementation(async (_user, id) => id === 'b' ? pending.promise : null);
		try {
			const { player, audio } = await freshPlayer();
			player.playList([song('a'), song('b')]);
			await vi.dynamicImportSettled();
			expect(audio.src).toContain('id=a');
			audio.fire('ended');
			expect(audio.src).toContain('id=b');
			const revoke = vi.fn();
			pending.resolve({ url: 'blob:late', revoke });
			await flush();
			expect(revoke).toHaveBeenCalledOnce();
		} finally {
			hidden.mockRestore();
			ua.mockRestore();
		}
	});

	it('Android: uses a local copy that finished preparing before the incoming track starts', async () => {
		const ua = vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue('Android');
		const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(false);
		getSession.mockReturnValue({ username: 'listener' });
		const revoke = vi.fn();
		getOffline.mockImplementation(async (_user, id) => id === 'b' ? { url: 'blob:saved-b', revoke } : null);
		try {
			const { player, audio } = await freshPlayer();
			player.playList([song('a'), song('b')]);
			await vi.dynamicImportSettled();
			const standby = FakeAudio.all[1];
			expect(standby.src).toBe('blob:saved-b');
			standby.ready();
			audio.fire('ended');
			expect(standby.play).toHaveBeenCalledOnce();
			expect(fetch).not.toHaveBeenCalled();
			expect(revoke).not.toHaveBeenCalled();
		} finally {
			hidden.mockRestore();
			ua.mockRestore();
		}
	});

	it('Android: waits for a pending offline copy when the device is offline at track end', async () => {
		const ua = vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue('Android');
		const online = vi.spyOn(navigator, 'onLine', 'get').mockReturnValue(false);
		getSession.mockReturnValue({ username: 'listener' });
		const local = deferred<LocalCopy | null>();
		getOffline.mockImplementation(async (_user, id) => id === 'b' ? local.promise : null);
		try {
			const { player, audio } = await freshPlayer();
			player.playList([song('a'), song('b')]);
			await vi.dynamicImportSettled();
			audio.fire('ended');
			expect(audio.src).toContain('id=a');
			local.resolve({ url: 'blob:saved-b', revoke: vi.fn() });
			await flush();
			expect(audio.src).toBe('blob:saved-b');
			expect(fetch).not.toHaveBeenCalled();
		} finally {
			online.mockRestore();
			ua.mockRestore();
		}
	});

	it('Android screen off: retries an incoming track with no data, and leaves advancing native playback alone', async () => {
		vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date', 'performance'] });
		const ua = vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue('Android');
		const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true);
		try {
			const { player, audio } = await freshPlayer();
			player.playList([song('a'), song('b')]);
			await vi.advanceTimersByTimeAsync(0);
			audio.play.mockImplementationOnce(() => {
				audio.paused = false;
				audio.fire('waiting');
				return new Promise(() => {});
			});
			audio.fire('ended');
			expect(player.buffering).toBe(true);
			audio.load.mockClear();
			await vi.advanceTimersByTimeAsync(17_100);
			expect(audio.load).toHaveBeenCalledTimes(1);
			expect(audio.src).toContain('id=b');
			audio.currentTime = 4; // Native playback is progressing without timeupdate.
			await vi.advanceTimersByTimeAsync(35_000);
			expect(audio.load).toHaveBeenCalledTimes(1);
		} finally {
			hidden.mockRestore();
			ua.mockRestore();
			vi.useRealTimers();
		}
	});

	it('Android screen off: a throttled watchdog retries a stationary stream while its offline read is suspended', async () => {
		const ua = vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue('Android');
		const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true);
		getSession.mockReturnValue({ username: 'listener' });
		getOffline.mockImplementation(async (_user, id) => id === 'b' ? new Promise(() => {}) : null);
		try {
			const { player, audio } = await freshPlayer();
			vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date', 'performance'] });
			const clock = performance.now.bind(performance);
			let throttled = 0;
			vi.spyOn(performance, 'now').mockImplementation(() => clock() + throttled);
			player.playList([song('a'), song('b')]);
			await vi.advanceTimersByTimeAsync(0);
			expect(getOffline.mock.calls.map(([, id]) => id)).toEqual(['a', 'b']);
			audio.play.mockImplementationOnce(() => {
				audio.paused = false;
				audio.fire('waiting');
				return new Promise(() => {});
			});
			audio.fire('ended');
			expect(audio.src).toContain('id=b');
			expect(player.buffering).toBe(true);
			audio.load.mockClear();
			// The OS held the startup watchdog well past its due time.
			throttled = 45_000;
			await vi.advanceTimersByTimeAsync(17_100);
			expect(audio.load).toHaveBeenCalledTimes(1);
			expect(getOffline.mock.calls.map(([, id]) => id)).toEqual(['a', 'b']);
			audio.currentTime = 2;
			throttled += 45_000;
			await vi.advanceTimersByTimeAsync(17_100);
			expect(audio.load).toHaveBeenCalledTimes(1);
		} finally {
			hidden.mockRestore();
			ua.mockRestore();
			vi.useRealTimers();
		}
	});

	it.each([
		{ source: 'prefetched', pageHidden: true },
		{ source: 'saved', pageHidden: true },
		{ source: 'prefetched', pageHidden: false }
	])('Android screen off (document.hidden=$pageHidden): retries a failed $source blob from its original source', async ({ source, pageHidden }) => {
		const ua = vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue('Android');
		const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(pageHidden);
		getSession.mockReturnValue({ username: 'listener' });
		getOffline.mockImplementation(async (_user, id) =>
			source === 'saved' && id === 'b' ? { url: 'blob:saved-b', revoke: vi.fn() } : null
		);
		vi.stubGlobal('fetch', vi.fn(async () => audioResponse()));
		try {
			const { player, audio } = await freshPlayer();
			vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date', 'performance'] });
			player.playList([song('a'), song('b')]);
			await vi.advanceTimersByTimeAsync(0);
			audio.startPlaying();
			await vi.advanceTimersByTimeAsync(0);
			audio.fire('ended');
			const incoming = FakeAudio.all.find((el) => el.src.startsWith('blob:') && !el.paused)!;
			expect(incoming.src).toMatch(source === 'saved' ? /^blob:saved-b$/ : /^blob:test\//);
			getOffline.mockClear();
			getOffline.mockImplementation(async () => source === 'saved'
				? { url: 'blob:reopened-b', revoke: vi.fn() }
				: new Promise(() => {}));
			incoming.fire('error');
			await vi.advanceTimersByTimeAsync(2_100);
			if (source === 'saved') {
				expect(getOffline).toHaveBeenCalledWith('listener', 'b');
				expect(incoming.src).toBe('blob:reopened-b');
			} else {
				expect(getOffline).not.toHaveBeenCalled();
				expect(incoming.src).toContain('id=b');
			}
		} finally {
			hidden.mockRestore();
			ua.mockRestore();
			vi.useRealTimers();
		}
	});

	it('Android: streams the next track on standby when its download is too slow', async () => {
		let signal: AbortSignal | undefined;
		vi.stubGlobal(
			'fetch',
			vi.fn((_url: string, init?: RequestInit) => {
				signal = init?.signal ?? undefined;
				return new Promise<Response>(() => {});
			})
		);
		const { player, audio } = await freshPlayer();
		player.playList([song('a', 200), song('b'), song('c')]);
		await flush();
		audio.startPlaying();
		playTo(audio, 100);
		expect(FakeAudio.all).toHaveLength(1);
		playTo(audio, 160);
		// The download is dropped and the stream goes on standby instead.
		expect(signal?.aborted).toBe(true);
		const standby = FakeAudio.all[1];
		expect(standby.src).toContain('id=b');
		expect(standby.paused).toBe(true);
		playTo(audio, 199.5);
		expect(player.current?.id).toBe('b');
		expect(standby.play).toHaveBeenCalled();
		expect(audio.paused).toBe(false);
		standby.ready();
		standby.fire('playing');
		expect(audio.paused).toBe(true);
	});

	it('prepares the following song after a playing standby stream takes over on a hidden page', async () => {
		const fetchMock = vi.fn((_url: string, _init?: RequestInit) => new Promise<Response>(() => {}));
		vi.stubGlobal('fetch', fetchMock);
		vi.doMock('./playback/webAudioOutput', () => ({ WebAudioOutput: FakeWebAudioOutput }));
		localStorage.setItem('music.audio', JSON.stringify({ engine: 'webaudio', crossfade: true }));
		const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(false);
		try {
			const { player, audio } = await freshPlayer();
			player.playList([song('a'), song('yt-b', 400), song('c')]);
			await flush();
			audio.startPlaying();
			playTo(audio, 160); // The download is still pending, so put the stream on standby.
			const standby = FakeAudio.all[1];
			expect(standby.src).toContain('id=yt-b');
			standby.ready();
			playTo(audio, 194);
			standby.fire('playing');
			expect(player.crossfading).toBe(true);
			fetchMock.mockClear();
			hidden.mockReturnValue(true);
			document.dispatchEvent(new Event('visibilitychange'));
			audio.fire('ended');
			await flush(); // No new playing event for the already audible standby.
			expect(player.current?.id).toBe('yt-b');
			expect(standby.paused).toBe(false);
			expect(fetchMock.mock.calls.map(([url]) => new URL(url).searchParams.get('id'))).toEqual(['yt-b', 'c']);
		} finally {
			hidden.mockRestore();
			vi.doUnmock('./playback/webAudioOutput');
		}
	});

	it('iOS: never streams on standby', async () => {
		const userAgent = vi
			.spyOn(navigator, 'userAgent', 'get')
			.mockReturnValue('Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)');
		vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(() => {})));
		try {
			const { player, audio } = await freshPlayer();
			player.playList([song('a', 200), song('b')]);
			await flush();
			audio.startPlaying();
			playTo(audio, 199.5);
			expect(FakeAudio.all).toHaveLength(1);
		} finally {
			userAgent.mockRestore();
		}
	});

	it('cancels the next-track download when that track starts streaming first', async () => {
		let signal: AbortSignal | undefined;
		vi.stubGlobal(
			'fetch',
			vi.fn((_url: string, init?: RequestInit) => {
				signal = init?.signal ?? undefined;
				return new Promise<Response>(() => {});
			})
		);
		const { player, audio } = await freshPlayer();
		player.playList([song('a', 200), song('b')]);
		await flush();
		audio.startPlaying();
		playTo(audio, 100);
		expect(signal?.aborted).toBe(false);
		audio.fire('ended');
		expect(player.current?.id).toBe('b');
		expect(signal?.aborted).toBe(true);
	});

	it('resumes when the app is opened after the system paused a track that just started', async () => {
		const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true);
		try {
			const { player, audio } = await freshPlayer();
			player.playList([song('a'), song('b')]);
			await flush();
			audio.ready();
			audio.fire('playing');
			// Not requested by the page: Android refusing audio focus.
			audio.pause();
			expect(player.playing).toBe(false);
			audio.play.mockClear();
			hidden.mockReturnValue(false);
			document.dispatchEvent(new Event('visibilitychange'));
			expect(audio.play).toHaveBeenCalledTimes(1);
		} finally {
			hidden.mockRestore();
		}
	});

	it('stays paused after a later system pause, like headphones unplugged', async () => {
		const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true);
		const now = vi.spyOn(performance, 'now');
		try {
			const { player, audio } = await freshPlayer();
			player.playList([song('a'), song('b')]);
			await flush();
			audio.ready();
			now.mockReturnValue(1_000);
			audio.fire('playing');
			now.mockReturnValue(61_000);
			audio.pause();
			audio.play.mockClear();
			hidden.mockReturnValue(false);
			document.dispatchEvent(new Event('visibilitychange'));
			expect(audio.play).not.toHaveBeenCalled();
			expect(player.playing).toBe(false);
		} finally {
			hidden.mockRestore();
			now.mockRestore();
		}
	});
});

describe('Player media errors', () => {
	it('Android: advances past a failed retry while the following track’s offline lookup is suspended and the page reports visible', async () => {
		const ua = vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue('Android');
		const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(false);
		getSession.mockReturnValue({ username: 'listener' });
		const local = deferred<LocalCopy | null>();
		getOffline.mockImplementation(async (_user, id) => id === 'c' ? local.promise : null);
		try {
			const { player, audio } = await freshPlayer();
			vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date', 'performance'] });
			player.playList([song('a'), song('b'), song('c')], 1);
			await vi.advanceTimersByTimeAsync(0);
			expect(getOffline.mock.calls.map(([, id]) => id)).toEqual(['b', 'c']);
			audio.fire('error');
			await vi.advanceTimersByTimeAsync(2_000);
			expect(audio.src).toContain('id=b');
			audio.fire('error');
			await vi.advanceTimersByTimeAsync(2_000);
			expect(player.current?.id).toBe('c');
			expect(audio.src).toContain('id=c');
			expect(audio.paused).toBe(false);
			const revoke = vi.fn();
			local.resolve({ url: 'blob:late-c', revoke });
			await vi.advanceTimersByTimeAsync(0);
			expect(revoke).toHaveBeenCalledOnce();
		} finally {
			hidden.mockRestore();
			ua.mockRestore();
			vi.useRealTimers();
		}
	});

	it('reloads a failed track once, then skips to the next', async () => {
		vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date', 'performance'] });
		try {
			const { player, audio } = await freshPlayer();
			player.playList([song('a'), song('b')]);
			await vi.advanceTimersByTimeAsync(0);
			audio.load.mockClear();
			audio.fire('error');
			await vi.advanceTimersByTimeAsync(2_000);
			expect(player.current?.id).toBe('a');
			expect(audio.load).toHaveBeenCalledTimes(1);
			audio.fire('error');
			await vi.advanceTimersByTimeAsync(2_000);
			expect(player.current?.id).toBe('b');
			expect(audio.src).toContain('id=b');
		} finally {
			vi.useRealTimers();
		}
	});

	it('stops after a few unplayable tracks in a row', async () => {
		vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date', 'performance'] });
		try {
			const { player, audio } = await freshPlayer();
			player.repeat = 'all';
			player.playList([song('a'), song('b')]);
			await vi.advanceTimersByTimeAsync(0);
			for (let i = 0; i < 20; i++) {
				audio.error = { code: 4, message: 'Format error' };
				audio.fire('error');
				await vi.advanceTimersByTimeAsync(2_500);
			}
			expect(audio.load.mock.calls.length).toBeLessThan(12);
			expect(player.error).not.toBeNull();
			expect(player.active).toBe(false);
		} finally {
			vi.useRealTimers();
		}
	});
});

describe('Player unload', () => {
	it('releases the source on sign-out', async () => {
		const { player, audio } = await freshPlayer();
		player.playList([song('a')]);
		await flush();
		player.reset();
		expect(audio.paused).toBe(true);
		expect(audio.src).toBe('');
		expect(player.current).toBeNull();
	});
});

describe('Player queue edits', () => {
	it('starts looking up a newly appended successor once and discards an obsolete local result', async () => {
		getSession.mockReturnValue({ username: 'listener' });
		const b = deferred<LocalCopy | null>();
		const d = deferred<LocalCopy | null>();
		getOffline.mockImplementation(async (_user, id) => id === 'b' ? b.promise : id === 'd' ? d.promise : null);
		const { player, audio } = await freshPlayer();
		player.playList([song('a')]);
		await vi.dynamicImportSettled();
		player.addToQueue([song('b')]);
		player.addToQueue([song('c')]);
		audio.ready();
		playTo(audio, 180);
		await vi.dynamicImportSettled();
		expect(getOffline.mock.calls.map(([, id]) => id)).toEqual(['a', 'b']);
		expect(fetch).not.toHaveBeenCalled();
		player.playNext([song('d')]);
		await vi.dynamicImportSettled();
		d.resolve({ url: 'blob:offline-d', revoke: vi.fn() });
		await flush();
		const revokeB = vi.fn();
		b.resolve({ url: 'blob:offline-b', revoke: revokeB });
		await flush();
		expect(revokeB).toHaveBeenCalledOnce();
		const standby = FakeAudio.all[1];
		expect(standby.src).toBe('blob:offline-d');
		expect(fetch).not.toHaveBeenCalled();
		audio.fire('ended');
		expect(player.current?.id).toBe('d');
		expect(standby.play).toHaveBeenCalledOnce();
	});

	it('reuses a pending offline lookup when its track is selected', async () => {
		getSession.mockReturnValue({ username: 'listener' });
		const pending = deferred<LocalCopy | null>();
		getOffline.mockImplementation(async (_user, id) => id === 'b' ? pending.promise : null);
		const { player, audio } = await freshPlayer();
		player.playList([song('a'), song('b')]);
		await vi.dynamicImportSettled();
		expect(audio.src).toContain('id=a');
		player.next();
		const revoke = vi.fn();
		pending.resolve({ url: 'blob:saved-b', revoke });
		await flush();
		expect(audio.src).toBe('blob:saved-b');
		expect(getOffline.mock.calls.map(([, id]) => id)).toEqual(['a', 'b']);
		expect(revoke).not.toHaveBeenCalled();
	});

	it('honours a pause while the selected track is waiting for its local copy', async () => {
		getSession.mockReturnValue({ username: 'listener' });
		const pending = deferred<LocalCopy | null>();
		getOffline.mockImplementation(async (_user, id) => id === 'b' ? pending.promise : null);
		const { player, audio } = await freshPlayer();
		player.playList([song('a'), song('b')]);
		await vi.dynamicImportSettled();
		player.next();
		player.pause();
		audio.play.mockClear();
		pending.resolve({ url: 'blob:saved-b', revoke: vi.fn() });
		await flush();
		audio.ready();
		expect(audio.src).toBe('blob:saved-b');
		expect(audio.autoplay).toBe(false);
		expect(audio.play).not.toHaveBeenCalled();
		expect(player.active).toBe(false);
	});

	it('aborts previous work on queue edits and does not reuse an obsolete completion for the same ID', async () => {
		const requests: Array<{ id: string; signal: AbortSignal; result: ReturnType<typeof deferred<Response>> }> = [];
		vi.stubGlobal('fetch', vi.fn((url: string, init: RequestInit) => {
			const result = deferred<Response>();
			requests.push({ id: new URL(url).searchParams.get('id')!, signal: init.signal!, result });
			return result.promise;
		}));
		const { player, audio } = await freshPlayer();
		player.playList([song('a', 400), song('b')]);
		await flush();
		audio.startPlaying();
		expect(requests[0].signal.aborted).toBe(false);
		playTo(audio, 100);
		expect(requests[0].signal.aborted).toBe(true);
		const first = requests[1];
		player.addToQueue([song('c')]);
		expect(requests).toHaveLength(2);
		player.removeAt(1);
		expect(first.signal.aborted).toBe(true);
		await flush();
		player.playNext([song('b')]);
		await flush();
		expect(requests.map(({ id }) => id)).toEqual(['b', 'b', 'c', 'b']);
		expect(requests[2].signal.aborted).toBe(true);
		first.result.resolve(audioResponse());
		await flush();
		expect(URL.createObjectURL).not.toHaveBeenCalled();
		requests[3].result.resolve(audioResponse());
		await flush();
		const url = FakeAudio.all[1].src;
		expect(url).toMatch(/^blob:/);
		player.clearUpNext();
		expect(FakeAudio.all[1].src).toBe('');
		expect(URL.revokeObjectURL).toHaveBeenCalledWith(url);
	});

	it.each<ResponseInit>([
		{ status: 206, headers: { 'content-range': 'bytes 0-19999/80000' } },
		{ status: 200, headers: { 'content-length': '80000' } },
		{ status: 200, headers: { 'content-length': '80000', 'content-encoding': 'identity' } }
	])('streams instead of promoting an incomplete $status response', async (init) => {
		vi.stubGlobal('fetch', vi.fn(async () => new Response(new Uint8Array(20_000), init)));
		const { player, audio } = await freshPlayer();
		player.playList([song('a'), song('b')]);
		await flush();
		audio.startPlaying();
		await flush();
		expect(URL.createObjectURL).not.toHaveBeenCalled();
		playTo(audio, 175);
		expect(FakeAudio.all[1].src).toContain('id=b');
	});

	it('repeat one releases standby, avoids speculative requests and lets the sleep timer stop playback', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => audioResponse()));
		const { player, audio } = await freshPlayer();
		player.playList([song('a'), song('b')]);
		await flush();
		audio.startPlaying();
		await flush();
		const standby = FakeAudio.all[1];
		const url = standby.src;
		player.cycleRepeat();
		player.cycleRepeat();
		expect(standby.src).toBe('');
		expect(URL.revokeObjectURL).toHaveBeenCalledWith(url);
		vi.mocked(fetch).mockClear();
		playTo(audio, 199.8);
		expect(fetch).not.toHaveBeenCalled();
		sleepTimer.onTrackEnded.mockReturnValueOnce(true);
		audio.play.mockClear();
		audio.fire('ended');
		expect(sleepTimer.onTrackEnded).toHaveBeenCalledOnce();
		expect(audio.play).not.toHaveBeenCalled();
		expect(player.current?.id).toBe('a');
	});

	it('immediately stops an audible crossfade when its successor is removed', async () => {
		const resetFade = vi.spyOn(FakeWebAudioOutput.prototype, 'resetFade');
		vi.doMock('./playback/webAudioOutput', () => ({ WebAudioOutput: FakeWebAudioOutput }));
		localStorage.setItem('music.audio', JSON.stringify({ engine: 'webaudio', crossfade: true }));
		vi.stubGlobal('fetch', vi.fn(async () => audioResponse()));
		try {
			const { player, audio } = await freshPlayer();
			player.playList([song('a'), song('b'), song('c')]);
			await flush();
			audio.startPlaying();
			await flush();
			playTo(audio, 194);
			const standby = FakeAudio.all[1];
			standby.fire('playing');
			expect(player.crossfading).toBe(true);
			player.removeAt(1);
			expect(player.crossfading).toBe(false);
			expect(standby.paused).toBe(true);
			expect(standby.src).toBe('');
			expect(resetFade).toHaveBeenCalledWith(audio);
			expect(player.current?.id).toBe('a');
		} finally {
			vi.doUnmock('./playback/webAudioOutput');
			resetFade.mockRestore();
		}
	});

	it('keeps the current song when removing and moving others', async () => {
		const { player } = await freshPlayer();
		player.playList([song('a'), song('b'), song('c'), song('d')], 2);
		await flush();
		player.removeAt(0);
		expect(player.current?.id).toBe('c');
		player.moveUpNext(2, 0);
		expect(player.queue.map((s) => s.id)).toEqual(['d', 'b', 'c']);
		expect(player.current?.id).toBe('c');
	});

	it('restores the original order when shuffle is turned off', async () => {
		const { player } = await freshPlayer();
		player.playList([song('a'), song('b'), song('c'), song('d')], 1);
		await flush();
		player.toggleShuffle();
		expect(player.queue[0].id).toBe('b');
		player.toggleShuffle();
		expect(player.queue.map((s) => s.id)).toEqual(['a', 'b', 'c', 'd']);
		expect(player.current?.id).toBe('b');
	});
});
