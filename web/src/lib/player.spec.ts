// Track-change paths of the Player, driven through a fake <audio> (jsdom has no media
// playback).
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Song } from './types';

type LocalCopy = { url: string; revoke: () => void };
const getSession = vi.fn<() => { username: string } | null>();
const getOffline = vi.fn<(_user: string, _id: string) => Promise<LocalCopy | null>>();
vi.mock('./offline', () => ({ createOfflineTrackUrl: getOffline }));
vi.mock('./api', () => ({
	getPlayQueue: vi.fn(async () => null),
	savePlayQueue: vi.fn(),
	scrobble: vi.fn(),
	streamUrl: (id: string) => `https://music.test/rest/stream?id=${id}`,
	coverUrl: () => undefined,
	getSession
}));

const sleepTimer = { onTrackEnded: vi.fn(() => false) };
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
	FakeAudio.all = [];
	vi.stubGlobal('Audio', FakeAudio);
	// The player mounts its elements in the document; the fake is not a DOM node.
	vi.spyOn(document.body, 'appendChild').mockImplementation((n) => n);
	vi.stubGlobal('fetch', vi.fn(async () => new Response('')));
	let n = 0;
	URL.createObjectURL = vi.fn(() => `blob:test/${++n}`);
	URL.revokeObjectURL = vi.fn();
	sleepTimer.onTrackEnded.mockReset().mockReturnValue(false);
});

afterEach(() => {
	(globalThis as { __musicPlayer?: { destroy(): void } }).__musicPlayer?.destroy();
	vi.unstubAllGlobals();
});

describe('Player track changes', () => {
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
		audio.ready();
		playTo(audio, 40);
		// The load already probed the next stream (Range warm-start); the whole-track
		// download must still wait for the lead window.
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
	async function withStandby() {
		vi.stubGlobal('fetch', vi.fn(async () => audioResponse()));
		const { player, audio } = await freshPlayer();
		player.playList([song('a', 200), song('b'), song('c')]);
		await flush();
		audio.ready();
		audio.fire('playing');
		playTo(audio, 100);
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
			audio.ready(); // No timeupdate before ended.
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
			audio.ready();
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
		audio.ready();
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

	it('iOS: never streams on standby', async () => {
		const userAgent = vi
			.spyOn(navigator, 'userAgent', 'get')
			.mockReturnValue('Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)');
		vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(() => {})));
		try {
			const { player, audio } = await freshPlayer();
			player.playList([song('a', 200), song('b')]);
			await flush();
			audio.ready();
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
		audio.ready();
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
		player.playList([song('a'), song('b')]);
		await flush();
		expect(requests[0].signal.aborted).toBe(false);
		audio.ready();
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
		audio.ready();
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
		audio.ready();
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
		const resetFade = vi.fn();
		vi.doMock('./playback/webAudioOutput', () => ({
			WebAudioOutput: class {
				attach() {}
				setVolume() {}
				setTrim() {}
				quickFadeIn() {}
				silenceFade() {}
				crossfade() {}
				resetFade = resetFade;
				trackChanged() {}
				resume() {}
				dispose() {}
			}
		}));
		localStorage.setItem('music.audio', JSON.stringify({ engine: 'webaudio', crossfade: true }));
		vi.stubGlobal('fetch', vi.fn(async () => audioResponse()));
		try {
			const { player, audio } = await freshPlayer();
			player.playList([song('a'), song('b'), song('c')]);
			await flush();
			audio.ready();
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
