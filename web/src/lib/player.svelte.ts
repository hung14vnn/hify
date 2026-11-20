import { getAlbum, getKaraoke, getPlayQueue, getSession, releaseKaraoke, savePlayQueue, scrobble, streamUrl } from './api';
import { build, buildLabel } from './build';
import { isPlaybackLogEnabled, logPlayback } from './playback/debugLog';
import { audioSettings } from './playback/audioSettings.svelte';
import { isLossless, type EnhancerSource } from './playback/cutoffDetector';
import { InterruptionGuard } from './playback/interruptions';
import { clearMediaSession, setMediaMetadata, setMediaPosition, setupMediaSession } from './playback/mediaSession';
import {
	cycleRepeat,
	indexAfterUnshuffle,
	interleaveEvenly,
	moveItem,
	nextIndex,
	planPlayOrder,
	removeAt,
	shuffleAround,
	type Repeat
} from './playback/queue';
import { pickOutput, watchRemotePlayback, type CastState } from './playback/remotePlayback';
import { gainModeFor, replayGainFactor } from './playback/replayGain';
import { SingleTab } from './playback/singleTab';
import { WebAudioOutput } from './playback/webAudioOutput';
import { clearSnapshot, readSnapshot, writeSnapshot } from './playback/snapshot';
import { sleepTimer } from './sleepTimer.svelte';
import type { Song } from './types';

export type { Repeat };

/** The codec actually being served, learned from the stream's Content-Type. */
/**
 * Streams the browser can't byte-seek (lossless remuxes, Spotify bridges): a seek
 * re-requests the stream with `timeOffset` instead of setting currentTime.
 */
function seeksOnServer(song: Song): boolean {
	return isLossless(song) || song.suffix === 'flac' || song.id.startsWith('st-');
}

/**
 * The body as a Blob, or null once it grows past `max` bytes. Content-Length alone
 * can't cap it: piped streams (ffmpeg remuxes of hi-res FLAC, 100-250 MB) send none.
 */
async function readCapped(res: Response, max: number): Promise<Blob | null> {
	if (!res.body) return res.blob();
	const reader = res.body.getReader();
	const parts: Uint8Array[] = [];
	let size = 0;
	for (;;) {
		const { done, value } = await reader.read();
		if (done) break;
		size += value.byteLength;
		if (size > max) {
			void reader.cancel();
			return null;
		}
		parts.push(value);
	}
	return new Blob(parts as BlobPart[], { type: res.headers.get('content-type') ?? '' });
}

function suffixFromContentType(value: string | null): string {
	const type = (value ?? '').toLowerCase();
	if (type.includes('flac')) return 'flac';
	if (type.includes('webm')) return 'webm';
	if (type.includes('ogg')) return 'opus';
	if (type.includes('mpeg')) return 'mp3';
	if (type.includes('mp4') || type.includes('aac')) return 'm4a';
	if (type.includes('wav')) return 'wav';
	return '';
}

interface ObjectUrl {
	url: string;
	revoke: () => void;
}

/** A downloaded (or fully prefetched) track's blob URL, tagged with the song it plays. */
interface OfflineCopy {
	id: string;
	copy: ObjectUrl;
	prefetched?: boolean;
}

interface NextPreparation {
	id: string;
	/** Transferred to load() if this track starts before the local read finishes. */
	lookup: Promise<ObjectUrl | null> | null;
	warm: AbortController;
}

interface BlobJob {
	id: string;
	abort: AbortController;
	/** Aborted by our own timeout (a failure), as opposed to superseded or unloaded. */
	timedOut: boolean;
}

interface BlobFailure {
	id: string;
	count: number;
	at: number;
	/** Not worth retrying (e.g. the file is too big to hold in memory). */
	final: boolean;
}

/** The Web Audio engine when the settings ask for it; plain <audio> if it can't start. */
function createOutput(
	wantsSound: () => boolean,
	onSource: (source: EnhancerSource) => void
): WebAudioOutput | null {
	if (audioSettings.activeEngine !== 'webaudio') return null;
	try {
		return new WebAudioOutput(wantsSound, onSource);
	} catch (e) {
		logPlayback('webaudio-unavailable', String(e));
		return null;
	}
}

function createManagedAudio(): HTMLAudioElement {
	const audio = new Audio();
	audio.setAttribute('aria-hidden', 'true');
	audio.style.position = 'fixed';
	audio.style.width = '1px';
	audio.style.height = '1px';
	audio.style.opacity = '0';
	audio.style.pointerEvents = 'none';
	document.body.appendChild(audio);
	return audio;
}

/** Park a native stream after 15 seconds without progress. */
const STALL_TIMEOUT_MS = 15_000;
/** Shortest wait between two stall checks (also the re-check while system-paused). */
const STALL_CHECK_MS = 2_000;
/**
 * A stall check that fires this much later than due was held by the OS (iOS releases
 * timers held in the background the moment the page is shown again), so its idea of
 * "no progress" is meaningless and it must not act on it.
 */
const STALL_LATE_MS = 5_000;

/**
 * Download the whole next track into RAM this early. Non-YouTube tracks on high-bitrate
 * formats can be 10 MB+; in iOS background networking is throttled, so give them ample
 * time (up to 5 min) before the current track ends.
 */
const BLOB_LEAD_S = 300;
/**
 * Same, for YouTube tracks (`yt-` ids), whose cold resolve is slow and whose first
 * attempt may fail at a proxy timeout (~80 s observed), so they need room for retries.
 */
const BLOB_LEAD_YT_S = 300;
/** A prefetched blob smaller than this is an error page or truncated, not audio. */
const MIN_BLOB_BYTES = 16 * 1024;
/** Longer files (lossless, long mixes) just stream instead of filling RAM. */
const MAX_BLOB_BYTES = 60 * 1024 * 1024;
/** A prefetch that takes longer than this is treated as failed. */
const BLOB_FETCH_TIMEOUT_MS = 150_000;
/** A failed prefetch is retried after this pause, up to MAX_BLOB_ATTEMPTS times. */
const BLOB_RETRY_GAP_MS = 4_000;
const MAX_BLOB_ATTEMPTS = 4;

/** HTMLMediaElement.HAVE_FUTURE_DATA. */
const HAVE_FUTURE_DATA = 3;

/**
 * An element "playing", with data to play, whose clock hasn't moved for this long is
 * frozen: iOS can resume one after a call (or Siri) without its sound coming back.
 */
const FROZEN_MS = 6_000;

/** Unplayable tracks skipped in a row before giving up. */
const MAX_ERROR_SKIPS = 2;
/** Pause before retrying or skipping a failed track, so a dead server isn't hammered. */
const ERROR_RETRY_DELAY_MS = 2_000;
/** Minimum gap between diagnostic probes of a failing source. */
const PROBE_MIN_GAP_MS = 10_000;
/** After this much successful playback, a later error counts as a new incident. */
const ERROR_REARM_AFTER_S = 10;

/** Media element events written to the playback log (timeupdate is too chatty). */
const LOGGED_EVENTS = ['play', 'pause', 'waiting', 'playing', 'canplay', 'stalled', 'ended', 'error', 'emptied', 'abort'];

/** A browser-reported length this much over the server's means the browser misread it. */
const DURATION_MISMATCH_RATIO = 1.25;

/**
 * Non-iOS: start the next track on the standby element this close to the end of the
 * current one, so some element is playing at every moment (see `swapToStandby`).
 */
const HANDOFF_LEAD_S = 0.6;

/**
 * Crossfade (Web Audio): the next song starts this long before the end, and the two
 * overlap for what's left once it is actually playing (see `startPreroll`). Songs
 * shorter than three times this just cut over.
 */
const CROSSFADE_S = 8;

/**
 * Non-iOS: when the next track isn't downloaded this close to the end (YouTube can
 * deliver a whole file slower than real time), load its stream into the standby element
 * instead, so the handoff still happens. The server has already resolved it for the
 * download, so buffering starts quickly.
 */
const STREAM_STANDBY_LEAD_S = 45;

/** A pause the page didn't ask for this soon after a track started is the system refusing it. */
const SYSTEM_PAUSE_WINDOW_MS = 2_000;

/** Lead time (seconds before the end) at which the whole-track prefetch of `id` starts. */
function blobLeadFor(id: string): number {
	return id.startsWith('yt-') || id.startsWith('ytv-') ? BLOB_LEAD_YT_S : BLOB_LEAD_S;
}

/** iPadOS reports a Mac user agent; touch support tells them apart. */
function isIOSDevice(): boolean {
	return (
		/iPhone|iPad|iPod/i.test(navigator.userAgent) ||
		(navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1)
	);
}

/** The downloaded copy of `id`, if the signed-in user has one. */
async function lookupOffline(id: string): Promise<ObjectUrl | null> {
	const session = getSession();
	if (!session?.username) return null;
	try {
		const { createOfflineTrackUrl } = await import('./offline');
		return await createOfflineTrackUrl(session.username, id);
	} catch {
		return null;
	}
}

/**
 * Single audio engine, modelled on Navidrome's web player: one <audio> element whose
 * src is swapped and load()ed on every track change, then played. The queue is the
 * play order; when shuffle is on, the original order is kept in `unshuffled` so
 * turning shuffle off restores it.
 *
 * Background note (iOS, and Android Chrome for slow YouTube streams): once audio
 * stops, the page may be frozen or throttled. So the track change in `ended` must be
 * fully synchronous: the next track is downloaded into a blob: URL while the current
 * one still plays (`prefetchNextBlob`), and `load()` takes it from `nextReady`
 * without any await. A cold server-side resolve (YouTube) is additionally triggered
 * as soon as the current track starts (`warmNext`), so that even if the blob is not
 * ready in time, the stream request at `ended` is answered quickly instead of leaving
 * a silent gap in which iOS suspends the page.
 *
 * Android: Chrome gives up audio focus while no element is playing, and after a few
 * minutes in the background Android refuses to grant it again, so a track started from
 * `ended` is paused by the system right away. So off iOS the prefetched next track is
 * loaded into a second, standby element and started just before the current one ends
 * (`swapToStandby`). iOS keeps the single element: it ties background playback to the
 * element that got the user's tap.
 */
class Player {
	queue = $state<Song[]>([]);
	index = $state(-1);
	playing = $state(false);
	buffering = $state(false);
	/**
	 * Playback position for the UI. While the page is hidden it is left alone (the
	 * reactive graph would otherwise re-run ~4×/s with the screen off) and caught
	 * up on `visibilitychange`. Engine logic uses `time`, which is always current.
	 */
	currentTime = $state(0);
	/** Always-current position (recovery reloads, snapshots, lock screen, seeking). */
	private time = 0;
	duration = $state(0);
	volume = $state(1);
	muted = $state(false);
	/** Web Audio: what the Sound Enhancer knows about the playing track (format, measured cutoff). */
	enhancerSource = $state<EnhancerSource | null>(null);
	shuffle = $state(false);
	repeat = $state<Repeat>('off');
	/** Karaoke: a karaoke mix is playing in place of `original` (raw: compared by id). */
	karaoke = $state.raw<{ original: Song; version: Song } | null>(null);
	karaokeLoading = $state(false);
	/** Waiting on the server's AI stems; the quick vocal cut may already be playing. */
	karaokeWaiting = $state(false);
	/** The karaoke stream playing now is the vocal cut, not the AI stems yet. */
	karaokePreview = $state(false);
	karaokeError = $state<string | null>(null);
	error = $state<string | null>(null);
	/** Remote Playback API (Chromecast etc. on Chrome/Android; AirPlay picker on Safari). */
	castAvailable = $state(false);
	castState = $state<CastState>('disconnected');
	/** Stuck 'connecting': the audio has left the device but nothing plays it. */
	castStalled = $state(false);

	/** What the play/pause button shows: playing, or loading towards it (with a spinner). */
	active = $derived(this.playing || this.buffering);
	current = $derived(this.index >= 0 ? (this.queue[this.index] ?? null) : null);
	/** Tracks already passed in this play session, for the queue's History section. */
	history = $derived(this.index > 0 ? this.queue.slice(0, this.index) : []);
	upNext = $derived(this.queue.slice(this.index + 1));
	/** Next track in queue that will play when the current track ends. */
	nextSong = $derived(this.index < 0 || this.repeat === 'one' ? undefined :
		this.queue[nextIndex(this.queue.length, this.index, this.repeat)]);
	/** The song lyrics are for: the original while its karaoke version plays. */
	lyricsSong = $derived(
		this.karaoke && this.current?.id === this.karaoke.version.id ? this.karaoke.original : this.current
	);
	/** Hardware sample rate of the Web Audio output graph, if active. */
	get sampleRate(): number | undefined {
		return this.output?.sampleRate;
	}

	private audio: HTMLAudioElement;
	private readonly ios = isIOSDevice();
	private readonly android = /Android/i.test(navigator.userAgent);
	/** Native position at startup, until the new Android source makes progress. */
	private androidStartTime: number | null = null;
	/**
	 * Web Audio engine (Settings › Playback), or null for plain <audio>. Chosen once per
	 * page load: an element joined to a graph can't leave it, so a change needs a restart.
	 */
	private readonly output: WebAudioOutput | null = createOutput(
		() => this.playRequested,
		(source) => (this.enhancerSource = source)
	);
	/** Non-iOS: second element with the next track (`nextReady`) loaded and paused. */
	private standby: HTMLAudioElement | null = null;
	/** URL loaded into `standby`, or null when it holds nothing usable. */
	private standbyFor: string | null = null;
	/**
	 * The previous element after a handoff, still playing its last moments until the new
	 * one plays. `listeners` aborts the two one-shot handoff listeners once either has fired.
	 */
	private retiring: { el: HTMLAudioElement; copy: OfflineCopy | null; listeners: AbortController } | null = null;
	/** When the current element last fired `playing`. */
	private playingSince = -Infinity;
	/** Non-iOS: next track whose stream (not a downloaded copy) goes into the standby element. */
	private standbyStreamId: string | null = null;
	private unshuffled: Song[] | null = null;
	private scrobbled = false;
	private pendingSeek = 0;
	/** Invalidates a load still waiting on the offline lookup when another one starts. */
	private loadToken = 0;
	/** Invalidates a karaoke start/poll loop that was turned off or followed another track. */
	private karaokeRun = 0;
	/** Server session keeping this song's stems in its memory while we play them. */
	private karaokeSession: string | null = null;
	/** Bumped when the same `kr-` id starts serving the AI stems instead of the vocal cut. */
	private karaokeBust = 0;
	/** Cache buster for stream URL reloads on seek or recovery. */
	private streamBust = 0;
	/** Time offset in seconds when streaming an unseekable/transcoded stream from a seek position. */
	private streamOffset = 0;
	/** A server-side seek (`timeOffset`) was just requested: the first real progress
	 *  tells whether the stream restarts its clock at 0 or keeps the original timestamp. */
	private streamOffsetUnverified = false;
	/** load() is awaiting the offline lookup: the outgoing element is still the active
	 *  one, but its time/end/error belong to the previous track and must be ignored. */
	private switching = false;
	/** Offline/prefetched copy now playing, revoked on the next change. */
	private playingOffline: OfflineCopy | null = null;
	/**
	 * The upcoming track, ready to start without the network: either explicitly
	 * downloaded for offline use, or prefetched into a blob while the current track plays.
	 */
	private nextReady: OfflineCopy | null = null;
	private preparation: NextPreparation | null = null;
	/** In-flight whole-track prefetch, and the last failure (for bounded retries). */
	private blobJob: BlobJob | null = null;
	private blobFailure: BlobFailure | null = null;
	/**
	 * Server length that playback ends at, or 0. iOS Safari misreads fragmented m4a
	 * (YouTube Music streams), often at about double, then plays silence up to it.
	 */
	private durationCap = 0;
	/** Playback is wanted: set by a play request, cleared by a pause that isn't a track change. */
	private playRequested = false;
	/** Playback failed while the page was hidden: retry once it is visible again. */
	private resumeWhenVisible = false;
	/** Track already reloaded once after a media error. */
	private errorRetriedFor: string | null = null;
	/** Tracks skipped in a row for errors; stops a dead server from cycling the queue. */
	private errorSkips = 0;
	private errorTimer: ReturnType<typeof setTimeout> | undefined;
	/** Queue index where the current run of errors started, to return to when giving up. */
	private errorOriginIndex = -1;
	/** loadToken whose error was already handled (one failure can raise several signals). */
	private errorHandledToken = -1;
	private lastProbeAt = 0;
	private stopDiagnostics: () => void = () => {};
	private stallTimer: ReturnType<typeof setTimeout> | undefined;
	/** Bumped whenever the watchdog is disarmed; a callback from an older generation is void. */
	private stallGen = 0;
	/** When the pending stall check is due (performance.now() scale), to detect late callbacks. */
	private stallDueAt = 0;
	private lastProgressAt = 0;
	private lastProgressTime = -1;
	/** 5 s slot of the last MediaSession position sync (see onTime). */
	private lastPositionSlot = -1;
	private saveTimer: ReturnType<typeof setTimeout> | undefined;
	private destroyed = false;
	private singleTab: SingleTab;
	private interruptions: InterruptionGuard;
	private stopRemoteWatch: () => void = () => {};
	private stopAudioSettings: () => void = () => {};
	/**
	 * Crossfade: the standby element already playing the next song under the end of
	 * this one (`fading` once its fade-in has begun).
	 */
	private preroll: { el: HTMLAudioElement; listeners: AbortController; fading: boolean } | null = null;
	/** Whether the player is currently crossfading between tracks. */
	crossfading = $state(false);

	constructor() {
		// Marks where each page load starts in the log, and which build it ran.
		logPlayback('app-start', `build=${buildLabel} built=${build.time} ua=${navigator.userAgent}`);
		// Remote Playback and audio-output selection are only exposed reliably for
		// media elements that belong to the document, not detached `new Audio()`
		// elements.
		this.audio = this.newElement();
		this.audio.preload = 'auto';
		this.singleTab = new SingleTab(() => this.pause());
		this.interruptions = new InterruptionGuard(this.audio, () => this.resumeAfterInterruption());

		this.bindElement(this.audio);
		this.bindActiveAudio(this.audio);

		// Have the offline chunk loaded before the page can be hidden, so a background
		// track change never has to fetch a JS chunk.
		void import('./offline').catch(() => {});

		this.stopDiagnostics = this.startDiagnostics();
		// Sound Check switched mid-song: apply it to the song playing.
		if (this.output) this.stopAudioSettings = audioSettings.onChange(() => this.applyReplayGain(true));

		window.addEventListener('pagehide', this.onPageHide);
		document.addEventListener('visibilitychange', this.onVisibilityChange);
		this.restore();
	}

	/**
	 * Playback log lines that show what the OS does to the page in the background:
	 * `freeze`/`resume` (Chrome freezing the tab), `net` (offline/online), and
	 * `timer-gap` (a 5 s timer that fired much later than due, i.e. timers were throttled).
	 * Together with the `probe` line on a media error they tell apart a throttled page
	 * or a cut network from a server/credential rejection. Returns a cleanup function.
	 */
	private startDiagnostics(): () => void {
		const onFreeze = () => logPlayback('lifecycle', 'freeze');
		const onResume = () => logPlayback('lifecycle', 'resume');
		const onOffline = () => logPlayback('net', 'offline');
		const onOnline = () => logPlayback('net', 'online');
		document.addEventListener('freeze', onFreeze);
		document.addEventListener('resume', onResume);
		window.addEventListener('offline', onOffline);
		window.addEventListener('online', onOnline);
		let lastBeat = performance.now();
		// The heartbeat only feeds the log, so don't wake the CPU every 5 s (audible
		// pages are exempt from background timer throttling) when logging is off.
		// Turning the log on takes effect from the next app start.
		const beat = isPlaybackLogEnabled() ? setInterval(() => {
			const now = performance.now();
			const gap = now - lastBeat;
			lastBeat = now;
			if (gap > 15_000) {
				logPlayback('timer-gap', `${Math.round(gap / 1000)}s hidden=${document.hidden} playing=${this.playing}`);
			}
		}, 5_000) : undefined;
		return () => {
			document.removeEventListener('freeze', onFreeze);
			document.removeEventListener('resume', onResume);
			window.removeEventListener('offline', onOffline);
			window.removeEventListener('online', onOnline);
			clearInterval(beat);
		};
	}

	/**
	 * Event flow follows Navidrome's player: a track change sets src and load()s,
	 * `canplay` (re)starts playback that is wanted, and a `waiting` element that
	 * already has enough buffered is kicked with play() again.
	 */
	private bindElement(a: HTMLAudioElement) {
		a.addEventListener('play', () => {
			if (this.audio !== a) return;
			this.playing = true;
			this.resumeWhenVisible = false;
			this.singleTab.announcePlaying();
		});
		a.addEventListener('pause', () => {
			if (this.audio !== a) return;
			this.playing = false;
			// Paused by the system right after a track started in the background, not by
			// us (our pauses clear playRequested first) and not a natural end: Android
			// refusing audio focus. Remember the request so opening the app resumes it.
			// Later pauses (headphones out, a call) are left alone.
			if (
				this.playRequested &&
				!a.ended &&
				document.hidden &&
				performance.now() - this.playingSince < SYSTEM_PAUSE_WINDOW_MS
			) {
				logPlayback('system-pause', `${Math.round(performance.now() - this.playingSince)}ms after start`);
				this.resumeWhenVisible = true;
			}
			// A pause that isn't part of a track change (end of track, headphones out)
			// drops the request, so a later `canplay` won't start playback by itself.
			if (!this.buffering) this.playRequested = false;
			this.persist(true);
		});
		a.addEventListener('waiting', () => {
			if (this.audio !== a) return;
			this.buffering = true;
			// If the active track is streaming and starving for data, cancel the next track's
			// background blob download so the active stream gets all available bandwidth.
			if (!this.playingOffline && this.blobJob) {
				logPlayback('blob-cancel', `id=${this.blobJob.id} active stream waiting`);
				this.blobJob.abort.abort();
				this.blobJob = null;
			}
			if (this.playRequested && a.readyState >= HAVE_FUTURE_DATA) this.play();
		});
		a.addEventListener('playing', () => {
			if (this.audio !== a) return;
			this.playingSince = performance.now();
			// Starting to play is progress: a slow start (the clock still at the load
			// position) must not be taken for a stall the moment it finally plays.
			this.lastProgressAt = this.playingSince;
			this.buffering = false;
			this.errorSkips = 0;
			this.errorOriginIndex = -1;
			this.watchFrozen(a);
		});
		a.addEventListener('canplay', () => {
			if (this.audio !== a) return;
			this.buffering = false;
			if (this.playRequested && a.paused) this.play();
			if (this.playRequested && !this.switching) this.prefetchInWindow();
		});
		a.addEventListener('loadedmetadata', () => {
			if (this.audio !== a) return;
			if (this.pendingSeek) {
				a.currentTime = this.pendingSeek;
				this.pendingSeek = 0;
			}
			this.applyDuration();
		});
		a.addEventListener('durationchange', () => {
			if (this.audio === a) this.applyDuration();
		});
		a.addEventListener('timeupdate', () => {
			if (this.audio === a && !this.switching) this.onTime();
		});
		a.addEventListener('seeked', () => {
			if (this.audio === a) {
				this.onTime();
				this.updatePosition();
			}
		});
		a.addEventListener('ended', () => {
			if (this.audio === a && !this.switching) this.onEnded();
		});
		a.addEventListener('error', () => {
			if (this.audio === a && !this.switching) this.onError();
		});
		for (const type of LOGGED_EVENTS) {
			a.addEventListener(type, () => {
				if (this.audio === a) {
					logPlayback(type, `t=${a.currentTime.toFixed(1)} rs=${a.readyState} ns=${a.networkState} paused=${a.paused} vol=${a.volume} muted=${a.muted}`);
				}
			});
		}
		a.addEventListener('volumechange', () => {
			// Web Audio: the element stays at full volume; the graph's gain is the volume.
			if (this.audio !== a || this.output) return;
			this.volume = a.volume;
			this.muted = a.muted;
		});
	}

	/** Rebind platform playback integrations whenever the active audio changes. */
	private bindActiveAudio(a: HTMLAudioElement) {
		this.interruptions.setAudio(a);
		this.stopRemoteWatch();
		this.stopRemoteWatch = watchRemotePlayback(a, {
			availability: (available) => (this.castAvailable = available),
			state: (state, stalled) => {
				this.castState = state;
				this.castStalled = stalled;
			}
		});
		setupMediaSession(a, {
			play: () => this.resume(),
			pause: () => this.pause(),
			previous: () => this.previous(),
			next: () => this.next(),
			seek: (seconds) => this.seek(seconds),
			currentTime: () => this.time
		});
	}

	// ---- queue control -------------------------------------------------------

	playList(songs: Song[], start = 0, opts: { shuffle?: boolean } = {}) {
		if (!songs.length) return;
		const order = planPlayOrder(songs, start, this.shuffle, !!opts.shuffle);
		this.shuffle = order.shuffle;
		this.unshuffled = order.unshuffled;
		this.queue = order.queue;
		this.load(order.start, true);
	}

	playNext(songs: Song[]) {
		if (!this.current) return this.playList(songs);
		this.queue.splice(this.index + 1, 0, ...songs);
		this.unshuffled?.push(...songs);
		this.queueChanged();
	}

	addToQueue(songs: Song[]) {
		if (!this.current) return this.playList(songs);
		this.queue.push(...songs);
		this.unshuffled?.push(...songs);
		this.queueChanged();
	}

	/** Spread songs through the not-yet-played part of the queue (Smart Discover). */
	interleaveUpNext(songs: Song[], limit?: number) {
		if (!songs.length) return;
		if (!this.current) return this.playList(songs);
		const start = this.index + 1;

		let targetSection: Song[];
		let restSection: Song[];
		if (limit) {
			targetSection = this.queue.slice(start, start + limit);
			restSection = this.queue.slice(start + limit);
		} else {
			targetSection = this.queue.slice(start);
			restSection = [];
		}

		this.queue = [
			...this.queue.slice(0, start),
			...interleaveEvenly(targetSection, songs),
			...restSection
		];
		this.unshuffled?.push(...songs);
		this.queueChanged();
	}

	removeAt(i: number) {
		const index = removeAt(this.queue, this.index, i);
		if (index === null) return;
		this.index = index;
		this.queueChanged();
	}

	clearUpNext(keepCount = 0) {
		if (this.index + 1 + keepCount >= this.queue.length) return;
		this.queue.splice(this.index + 1 + keepCount);
		this.queueChanged();
	}

	moveUpNext(from: number, to: number) {
		this.index = moveItem(this.queue, this.index, from, to);
		this.queueChanged();
	}

	private queueChanged() {
		this.prepareNext();
		this.persist();
	}

	jumpTo(i: number) {
		this.load(i, true);
	}

	// ---- transport -----------------------------------------------------------

	toggle() {
		if (!this.current) return;
		if (this.playRequested) this.pause();
		else this.resume();
	}

	resume() {
		if (!this.current) return;
		// A source that errored can't just be played again; reload it. This is also
		// how the user restarts playback after the player gave up on a dead server.
		// A stall leaves no `audio.error`, so also reload whenever this load's failure
		// was already handled: play() on the dead request would spin forever, because
		// onError ignores a second failure of the same load. (Giving up may also have
		// moved `index` back to the first failed track, away from the loaded src.)
		if (!this.audio.src || this.audio.error || this.errorHandledToken === this.loadToken) {
			this.errorRetriedFor = null;
			this.errorSkips = 0;
			this.load(this.index, true, this.time);
		} else this.play();
	}

	/** Re-resolve the current track at the same position (e.g. new quality). */
	reloadCurrent() {
		if (!this.current) return;
		this.clearPreparation();
		if (this.playingOffline?.prefetched) this.releaseOffline();
		void this.load(this.index, this.playRequested, this.time);
	}

	pause() {
		logPlayback('pause-request');
		this.playRequested = false;
		this.resumeWhenVisible = false;
		this.buffering = false;
		this.interruptions.userPause();
		this.cancelPreroll();
		if (this.retiring) this.finishRetire(this.retiring.el);
		this.audio.pause();
	}

	next(automatic = false) {
		const i = nextIndex(this.queue.length, this.index, this.repeat);
		if (i >= 0) this.load(i, true, 0, automatic ? 'advance' : 'selected');
		else {
			this.pause();
			this.seek(0);
		}
	}

	previous() {
		// Like every music app: restart the song unless we're within the first 3 seconds.
		if (this.time > 3 || this.index <= 0) this.seek(0);
		else this.load(this.index - 1, true);
	}

	seek(seconds: number) {
		// Back from the last seconds: the next song isn't due yet (onTime starts it again).
		// Seeking must not change play/pause: `next()` at the end of the queue pauses
		// and then seeks to 0, which used to restart server-seek tracks forever.
		const resume = this.playRequested;
		this.cancelPreroll();
		this.setTime(seconds);
		const a = this.audio;
		if (!a.src) {
			// Nothing loaded yet (restored queue): resume() loads from this.time.
			this.updatePosition();
			return;
		}
		const isOfflineOrBlob = this.playingOffline || (a.src && a.src.startsWith('blob:'));

		if (isOfflineOrBlob) {
			if (a.src) {
				try {
					a.currentTime = seconds;
				} catch (e) {
					logPlayback('seek-error', String(e));
				}
			} else {
				this.pendingSeek = seconds;
			}
			this.updatePosition();
			return;
		}

		// A lossless or piped stream cannot be byte-seeked by the browser; seek via server timeOffset.
		// A piped stream with no known length can't be byte-seeked either.
		const serverSeek =
			this.streamOffset > 0 || (this.current && seeksOnServer(this.current)) || !Number.isFinite(a.duration) || a.duration <= 0;

		if (serverSeek && this.current) {
			logPlayback('stream-seek', `id=${this.current.id} at=${seconds.toFixed(1)}`);
			this.audio.autoplay = resume;
			this.restartStreamAt(seconds);
			if (resume) this.play();
		} else {
			if (a.src) {
				try {
					a.currentTime = seconds;
				} catch (e) {
					logPlayback('seek-error', String(e));
				}
			} else {
				this.pendingSeek = seconds;
			}
		}
		this.updatePosition();
	}

	/** Re-request the current track's stream from `seconds` (server-side seek). */
	private restartStreamAt(seconds: number) {
		const song = this.current;
		if (!song) return;
		this.streamOffset = seconds;
		this.streamOffsetUnverified = seconds > 0;
		this.pendingSeek = 0;
		// A fresh request: its own failure must reach onError even if an earlier
		// one of this load was already handled.
		this.errorHandledToken = -1;
		this.audio.src = streamUrl(song.id, ++this.streamBust, seconds);
		this.audio.load();
	}

	setVolume(v: number) {
		if (this.output) {
			this.volume = Math.max(0, Math.min(1, v));
			if (v > 0) this.muted = false;
			this.output.setVolume(this.volume, this.muted);
			return;
		}
		this.audio.volume = Math.max(0, Math.min(1, v));
		if (v > 0) this.audio.muted = false;
	}

	toggleMute() {
		if (this.output) {
			this.muted = !this.muted;
			this.output.setVolume(this.volume, this.muted);
			return;
		}
		this.audio.muted = !this.audio.muted;
	}

	/** Sound goes through Web Audio: volume works on iOS and the equalizer applies. */
	get webAudio() {
		return this.output !== null;
	}

	/** Volume onto a newly active element: its own, or (Web Audio) the graph's. */
	private applyVolume(el: HTMLAudioElement) {
		if (this.output) {
			el.volume = 1;
			el.muted = false;
			this.output.setVolume(this.volume, this.muted);
			return;
		}
		el.volume = this.volume;
		el.muted = this.muted;
	}

	/** A document-attached element, joined to the Web Audio graph when that engine is on. */
	private newElement(): HTMLAudioElement {
		const el = createManagedAudio();
		if (this.output) {
			// Without CORS a cross-origin stream reaches the graph as silence.
			el.crossOrigin = 'anonymous';
			this.output.attach(el);
		}
		return el;
	}

	/** Replace the active audio element with a clean one if the current one hit a fatal decoder error. */
	private replaceActiveElement(): HTMLAudioElement {
		const old = this.audio;
		old.pause();
		old.removeAttribute('src');
		old.load();
		old.remove();
		const next = this.newElement();
		this.bindElement(next);
		this.bindActiveAudio(next);
		this.audio = next;
		return next;
	}

	toggleShuffle() {
		const cur = this.current;
		if (!this.shuffle) {
			this.shuffle = true;
			this.unshuffled = [...this.queue];
			if (cur) {
				const order = shuffleAround(this.queue, this.index);
				this.queue = order.queue;
				this.index = order.index;
			}
		} else {
			this.shuffle = false;
			if (this.unshuffled) {
				this.queue = this.unshuffled;
				this.index = indexAfterUnshuffle(this.queue, cur);
			}
			this.unshuffled = null;
		}
		this.applyReplayGain(true);
		this.queueChanged();
	}

	/**
	 * Karaoke on: the server cuts the vocals out of the playing local track and plays
	 * that vocal cut straight away, then swaps in the AI stems when they finish. Off:
	 * back to the original. Returns why it couldn't start, or null.
	 */
	async toggleKaraoke(): Promise<string | null> {
		if (this.karaoke || this.karaokeWaiting || this.karaokeLoading) {
			const original = this.karaoke?.original ?? null;
			this.leaveKaraoke();
			if (original) this.replaceCurrent(original);
			return null;
		}
		const song = this.current;
		if (!song) return null;
		if (song.karaokeOf || song.id.startsWith('yt-') || song.id.startsWith('ytv-') || song.id.startsWith('st-')) {
			return 'Karaoke works with local library tracks only.';
		}
		const run = ++this.karaokeRun;
		const original = $state.snapshot(song) as Song;
		this.karaokeLoading = true;
		this.karaokeWaiting = true;
		this.karaokeError = null;
		let versionId: string | null = null;
		try {
			for (;;) {
				const state = await getKaraoke(song, this.karaokeSession);
				const currentId = this.current?.id;
				if (run !== this.karaokeRun || (currentId !== song.id && currentId !== versionId)) {
					// Turned off or moved on while the server was working.
					if (state.sessionId && state.sessionId !== this.karaokeSession) {
						void releaseKaraoke(state.sessionId);
					}
					return null;
				}
				this.karaokeLoading = false;
				this.karaokeSession = state.sessionId ?? this.karaokeSession;
				if (state.streamReady && state.song) {
					if (!versionId) {
						const version: Song = {
							...state.song,
							coverArt: song.coverArt ?? state.song.coverArt,
							karaokeOf: song.id
						};
						versionId = version.id;
						logPlayback('karaoke', `id=${song.id} → ${version.id} preview=${state.preview}`);
						this.karaoke = { original, version };
						this.karaokePreview = state.preview;
						this.replaceCurrent(version);
					} else if (!state.preview) {
						// The same `kr-` id now serves the AI stems: reload in place.
						this.karaokeBust++;
						this.karaokePreview = false;
						void this.load(this.index, this.playRequested || this.playing, this.time);
					}
				}
				if (state.status === 'failed' || (state.status === 'not_generated' && !state.streamReady)) {
					throw new Error(state.error ?? 'Karaoke is unavailable for this track.');
				}
				if (versionId && !state.preview) {
					this.karaokeWaiting = false;
					this.karaokeError = null;
					return null;
				}
				await new Promise((resolve) => setTimeout(resolve, 1200));
			}
		} catch (e) {
			if (run !== this.karaokeRun) return null;
			const message = e instanceof Error && e.message ? e.message : 'Karaoke generation failed';
			logPlayback('karaoke-failed', message);
			this.karaokeLoading = false;
			if (versionId) {
				// The vocal cut keeps playing; the sheet explains the AI part failed.
				this.karaokeError = message;
				return null;
			}
			this.leaveKaraoke();
			return message;
		}
	}

	/** Hides the karaoke sheet; the server keeps working and the stems swap in when ready. */
	dismissKaraokeWait() {
		this.karaokeWaiting = false;
	}

	/** Ends karaoke, putting the original back wherever its karaoke mix sits. */
	private leaveKaraoke() {
		this.cancelKaraoke();
		const k = this.karaoke;
		if (!k) return;
		this.karaoke = null;
		const swap = (list: Song[]) => list.map((s) => (s.id === k.version.id ? k.original : s));
		this.queue = swap(this.queue);
		if (this.unshuffled) this.unshuffled = swap(this.unshuffled);
	}

	/** Stops the karaoke loop and frees the server session; the queue is left alone. */
	private cancelKaraoke() {
		this.karaokeRun++;
		this.karaokeLoading = false;
		this.karaokeWaiting = false;
		this.karaokePreview = false;
		this.karaokeError = null;
		const session = this.karaokeSession;
		this.karaokeSession = null;
		if (session) void releaseKaraoke(session);
	}

	/** Plays `song` in the current song's place, from the same moment. */
	private replaceCurrent(song: Song) {
		const from = this.current;
		if (!from) return;
		const queue = [...this.queue];
		queue[this.index] = song;
		this.queue = queue;
		if (this.unshuffled) this.unshuffled = this.unshuffled.map((s) => (s.id === from.id ? song : s));
		const at = song.duration && this.time > song.duration - 5 ? 0 : this.time;
		void this.load(this.index, this.playRequested || this.playing, at);
	}

	cycleRepeat() {
		this.repeat = cycleRepeat(this.repeat);
		this.queueChanged();
	}

	/** Opens the system device picker; false when this browser has none. */
	pickOutput(): Promise<boolean> {
		return pickOutput(this.audio);
	}

	// ---- track changes -------------------------------------------------------

	/**
	 * Start track `i`, from a downloaded/prefetched copy when there is one.
	 *
	 * Everything up to the first `await` runs synchronously. When the track is
	 * `playingOffline`, `nextReady`, or known to have no offline copy, there is no
	 * await at all, which is what lets a track change from `ended` work on a hidden page.
	 */
	private async load(i: number, autoplay: boolean, startAt = 0, reason: 'selected' | 'advance' | 'stream-retry' = 'selected') {
		// Karaoke is per song: moving to another puts the original back in the queue.
		const picked = this.queue[i];
		if ((this.karaoke || this.karaokeWaiting) && picked && picked.id !== this.karaoke?.version.id) {
			this.leaveKaraoke();
		}
		const song = this.queue[i];
		if (!song) return;
		const incoming = this.preparation?.id === song.id ? this.preparation : null;
		// A preroll only carries on into the song it was started for, from its start.
		if (this.preroll && (!incoming || !autoplay || startAt !== 0)) this.cancelPreroll();
		// A handoff still finishing ends with the next change.
		if (this.retiring) this.finishRetire(this.retiring.el);
		const token = ++this.loadToken;
		this.index = i;
		this.androidStartTime = null;
		this.error = null;
		this.scrobbled = false;
		this.durationCap = 0;
		this.setTime(startAt);
		this.duration = song.duration ?? 0;
		this.pendingSeek = startAt;
		this.lastProgressAt = performance.now();
		this.lastProgressTime = -1;
		this.lastPositionSlot = -1;
		this.playing = false;
		this.playRequested = autoplay;
		this.buffering = autoplay;
		if (autoplay) this.armStallCheck();
		// Publish the next track before any IndexedDB lookup or stream setup.
		// Those awaits may be suspended by iOS in the background; leaving the
		// previous metadata active makes the lock screen show the wrong song and
		// duration even after the queue has advanced.
		setMediaMetadata(song);
		setMediaPosition(this.duration, startAt, this.audio.playbackRate);

		if (!song.coverArt && song.albumId) {
			void getAlbum(song.albumId).then((album) => {
				if (album?.coverArt && this.queue[this.index]?.id === song.id) {
					this.queue[this.index] = { ...this.queue[this.index], coverArt: album.coverArt };
					if (this.current?.id === song.id) {
						setMediaMetadata(this.current);
					}
				}
			}).catch(() => {});
		}

		// Android can suspend IndexedDB while the media session runs and the page
		// still reports visible. Keep an automatic handoff or stream retry synchronous
		// when online. A local copy that finished preparing always wins below.
		const useStream = this.android && navigator.onLine &&
			(reason === 'stream-retry' || (!!incoming && (reason === 'advance' || document.hidden)));
		let offline: OfflineCopy | null = null;
		if (this.playingOffline?.id === song.id) {
			// Restarting the same track: keep its URL.
			offline = this.playingOffline;
			this.playingOffline = null;
		} else if (this.nextReady?.id === song.id) {
			offline = this.nextReady;
			this.nextReady = null;
		} else if ((!incoming || incoming.lookup) && !useStream) {
			// Take ownership of any local read already underway. A completed miss
			// skips this await entirely, including during a background handoff.
			const lookup = incoming?.lookup ?? lookupOffline(song.id);
			if (incoming) incoming.lookup = null;
			this.switching = true;
			this.prepareNext();
			const copy = await lookup;
			// The user moved on to another track while we were looking.
			if (this.destroyed || token !== this.loadToken) {
				copy?.revoke();
				return;
			}
			if (copy) offline = { id: song.id, copy };
		}
		this.switching = false;
		const wantsPlayback = this.playRequested;
		const outgoing = this.playingOffline;
		this.playingOffline = offline;
		const serverSeek = !offline && seeksOnServer(song);
		this.streamOffset = startAt > 0 && serverSeek ? startAt : 0;
		this.streamOffsetUnverified = this.streamOffset > 0;
		// The server already starts the stream at startAt: don't seek again on metadata.
		if (this.streamOffset) this.pendingSeek = 0;
		const src = offline?.copy.url ?? streamUrl(song.id, song.karaokeOf ? this.karaokeBust : 0, this.streamOffset);
		const handoff = wantsPlayback && startAt === 0 && this.standbyHolds(src);

		logPlayback(
			'load',
			`id=${song.id} "${song.title}" at=${startAt.toFixed(1)} ${offline ? 'ready (offline/prefetched)' : 'stream'}${handoff ? ' handoff' : ''}`
		);
		if (handoff) {
			// The outgoing copy stays valid while its element finishes; it is revoked on retire.
			this.swapToStandby(outgoing);
		} else {
			// Not taking over the prerolled element after all: stop it, or both would play.
			this.cancelPreroll();
			outgoing?.copy.revoke();
			this.output?.quickFadeIn(this.audio, 0.22);
			this.audio.autoplay = wantsPlayback;
			this.applyVolume(this.audio);
			this.audio.src = src;
			this.audio.load();
			if (startAt > 0 && !serverSeek) this.audio.currentTime = startAt;
		}
		this.output?.trackChanged(isLossless(song));
		this.applyReplayGain();
		void this.learnCodec(song, Boolean(offline));
		if (wantsPlayback) {
			if (this.android) this.androidStartTime = this.audio.currentTime;
			// Unlike Navidrome, play() right away rather than waiting for `canplay`: a cold
			// YouTube track takes seconds to arrive, and Android/iOS refuse to start audio
			// from a background page that has been silent that long. `canplay` still retries.
			this.play();
			if (!song.karaokeOf) scrobble(song.id, false, song.coverArt);
		}
		this.persist();
		this.prepareNext();
	}

	/**
	 * Whether the standby element is used: always off iOS (see the class comment). On
	 * iOS only to crossfade, and only while the app is on screen: in the background iOS
	 * keeps to the single element that got the user's tap.
	 */
	private standbyAllowed(): boolean {
		return !this.ios || !!this.preroll || (this.crossfadeFor(Infinity) > 0 && !document.hidden);
	}

	/** Seconds to crossfade a song of `dur` seconds into the next one; 0 to cut. */
	private crossfadeFor(dur: number): number {
		// An end-of-track sleep timer stops at the very end, so no early handoff.
		if (!this.output || !audioSettings.crossfade || sleepTimer.isEndOfTrack) return 0;
		return dur >= CROSSFADE_S * 3 ? CROSSFADE_S : 0;
	}

	/** Sound Check: the playing song's ReplayGain onto its element (unity when off). */
	private applyReplayGain(smooth = false) {
		if (!this.output) return;
		const song = this.current;
		const gain = this.replayGainAt(this.index);
		this.output.setTrim(this.audio, gain, smooth);
		if (audioSettings.soundCheck && !smooth) logPlayback('replaygain', `id=${song?.id} ${(20 * Math.log10(gain)).toFixed(1)} dB`);
	}

	/** Sound Check gain for the song at `index` (unity when off). */
	private replayGainAt(index: number): number {
		if (!audioSettings.soundCheck) return 1;
		return replayGainFactor(this.queue[index], gainModeFor(this.queue, index, this.shuffle));
	}

	/**
	 * Crossfade: start the next song on the standby element, silent until it is
	 * actually playing, then fading in under this song's last `remaining` seconds
	 * while this one fades out. The switch itself (title, queue, lyrics, lock screen)
	 * still waits for this song's `ended`, so what's shown is the song ending; by then
	 * the next one is already playing and simply takes over (`swapToStandby`).
	 */
	private startPreroll(remaining: number) {
		const el = this.standby;
		const output = this.output;
		if (!el || !output) return;
		const nextIndex = this.queue[this.index + 1] ? this.index + 1 : 0;
		const endsAt = performance.now() + remaining * 1000;
		const preroll = { el, listeners: new AbortController(), fading: false };
		this.preroll = preroll;
		output.silenceFade(el);
		output.setTrim(el, this.replayGainAt(nextIndex));
		// Not before it plays: a slow start would otherwise use up the fade, leaving
		// this song faded out and the next one jumping in near full.
		el.addEventListener(
			'playing',
			() => {
				const seconds = Math.max(0.5, (endsAt - performance.now()) / 1000);
				preroll.fading = true;
				this.crossfading = true;
				logPlayback('crossfade', `${seconds.toFixed(1)}s`);
				output.crossfade(this.audio, el, seconds);
			},
			{ once: true, signal: preroll.listeners.signal }
		);
		logPlayback('preroll', `id=${this.queue[nextIndex]?.id} ${remaining.toFixed(1)}s left`);
		el.play().catch((e: unknown) => {
			logPlayback('preroll-rejected', String(e));
			if (this.preroll === preroll) this.cancelPreroll();
		});
	}

	/** Stops a preroll that won't become the next song after all (pause, seek, skip, queue change). */
	private cancelPreroll() {
		const preroll = this.preroll;
		if (!preroll) return;
		this.preroll = null;
		this.crossfading = false;
		preroll.listeners.abort();
		preroll.el.pause();
		try {
			preroll.el.currentTime = 0;
		} catch {
			/* nothing loaded yet */
		}
		this.output?.resetFade(preroll.el);
		this.output?.resetFade(this.audio);
		logPlayback('preroll-cancel');
	}

	/** The standby element has `url` loaded and can take over playback now. */
	private standbyHolds(url: string): boolean {
		return (
			!!this.standby &&
			this.standbyFor === url &&
			!this.standby.error &&
			// Android may suppress preload with the screen locked. Keep using the
			// active media element when the second one still has nothing to play.
			(!this.android || !document.hidden || this.standby.readyState >= HAVE_FUTURE_DATA) &&
			!this.retiring &&
			this.standbyAllowed() &&
			// Casting follows the element that started it; a swap would drop the session.
			this.castState === 'disconnected'
		);
	}

	/**
	 * What the standby element should hold for the upcoming track: its downloaded copy
	 * when there is one, else its stream once `standbyStreamId` asks for it.
	 */
	private standbySource(): { id: string; url: string; kind: 'copy' | 'stream' } | null {
		const next = this.nextSong;
		if (!next || next.id === this.current?.id) return null;
		if (this.nextReady?.id === next.id) return { id: next.id, url: this.nextReady.copy.url, kind: 'copy' };
		if (this.standbyStreamId === next.id) return { id: next.id, url: streamUrl(next.id), kind: 'stream' };
		return null;
	}

	/**
	 * Non-iOS: load the next track into the standby element, paused, so the handoff
	 * needs no fresh element (and, for a downloaded copy, no network). Clears it when the
	 * next track changes. Waits while a handoff is still finishing on that element.
	 */
	private primeStandby() {
		if (this.retiring || this.destroyed) return;
		const source = this.standbyAllowed() ? this.standbySource() : null;
		const url = source?.url ?? null;
		if (url === this.standbyFor) return;
		// The next song changed (queue edit) while it was prerolling.
		this.cancelPreroll();
		this.standbyFor = url;
		if (!source) {
			if (this.standby?.getAttribute('src')) {
				this.standby.removeAttribute('src');
				this.standby.load();
			}
			return;
		}
		if (!this.standby) {
			this.standby = this.newElement();
			this.bindElement(this.standby);
		}
		this.standby.autoplay = false;
		this.standby.preload = 'auto';
		this.standby.src = source.url;
		this.standby.load();
		logPlayback('standby-ready', `id=${source.id} ${source.kind}`);
	}

	/**
	 * Non-iOS, near the end of a track whose successor isn't downloaded: put the
	 * successor's stream on standby, and drop the download, which would now only compete
	 * with both streams for bandwidth.
	 */
	private standbyNextStream() {
		const next = this.nextSong;
		if (!this.standbyAllowed() || !next || this.preparation?.id !== next.id || this.preparation.lookup) return;
		if (this.nextReady?.id === next.id || this.standbyStreamId === next.id) return;
		this.standbyStreamId = next.id;
		this.preparation.warm.abort();
		if (this.blobJob?.id === next.id) {
			logPlayback('blob-cancel', `id=${next.id} streaming on standby instead`);
			this.blobJob.abort.abort();
			this.blobJob = null;
		}
		this.primeStandby();
	}

	/**
	 * Make the standby element (already holding the track being loaded) the active one.
	 * The old element is left to play its last moments and is only stopped once the new
	 * one is playing, so there is never a moment without a playing element. After a
	 * crossfade preroll the new one is already playing and the old one has ended.
	 */
	private swapToStandby(outgoing: OfflineCopy | null) {
		const old = this.audio;
		const next = this.standby!;
		this.audio = next;
		this.standby = old;
		this.standbyFor = null;
		this.bindActiveAudio(next);
		this.applyVolume(next);
		// Its loadedmetadata fired while it was the standby (ignored then) and won't
		// fire again: take the real length now, or the metadata guess sticks.
		if (next.readyState >= 1) this.applyDuration();
		const preroll = this.preroll;
		if (preroll) {
			this.preroll = null;
			this.crossfading = false;
			preroll.listeners.abort();
			// It never got to play under the old song, or a skip cut the fade short:
			// full level now.
			if (!preroll.fading || !old.ended) this.output?.resetFade(next);
			old.autoplay = false;
			old.pause();
			old.removeAttribute('src');
			old.load();
			this.output?.resetFade(old);
			outgoing?.copy.revoke();
			return;
		}
		// Whichever fires first finishes the handoff; the abort then removes the other,
		// which `once` alone would leave attached for good (one per track change).
		const listeners = new AbortController();
		this.retiring = { el: old, copy: outgoing, listeners };
		const finish = () => this.finishRetire(old);
		old.addEventListener('ended', finish, { once: true, signal: listeners.signal });
		this.output?.resetFade(next);
		next.addEventListener('playing', finish, { once: true, signal: listeners.signal });
	}

	private finishRetire(old: HTMLAudioElement) {
		const retiring = this.retiring;
		if (!retiring || retiring.el !== old) return;
		this.retiring = null;
		retiring.listeners.abort();
		old.autoplay = false;
		old.pause();
		old.removeAttribute('src');
		old.load();
		retiring.copy?.copy.revoke();
		this.primeStandby();
	}

	private clearPreparation() {
		this.preparation?.warm.abort();
		this.preparation = null;
		this.blobJob?.abort.abort();
		this.blobJob = null;
		this.blobFailure = null;
		this.cancelPreroll();
		this.standbyStreamId = null;
		const ready = this.nextReady;
		this.nextReady = null;
		this.primeStandby();
		ready?.copy.revoke();
	}

	private prepareNext() {
		const next = this.nextSong;
		const id = next?.id !== this.current?.id ? next?.id : undefined;
		if (this.preparation?.id === id) return;
		this.clearPreparation();
		if (!next || !id || this.switching || !this.audio.src || this.destroyed) return;
		const lookup = lookupOffline(id);
		const preparation: NextPreparation = { id, lookup, warm: new AbortController() };
		this.preparation = preparation;
		void lookup.then((copy) => {
			if (!preparation.lookup) return; // load() owns the result
			preparation.lookup = null;
			if (this.preparation !== preparation) {
				copy?.revoke();
				return;
			}
			if (!copy) {
				if (this.playRequested) this.prefetchInWindow();
				if (!this.blobJob) void this.warmNext(next, preparation.warm.signal);
				return;
			}
			this.nextReady = { id: next.id, copy };
			this.primeStandby();
		});
	}

	/**
	 * Ask the server for two bytes of the next track as soon as the current one starts.
	 * A cold YouTube track can take over a minute to resolve on the server; doing it here,
	 * while audio is playing and the page is still running, means the later whole-track
	 * download or stream request is answered from the server's cache.
	 */
	private async warmNext(next: Song, signal: AbortSignal) {
		const started = performance.now();
		logPlayback('warm-start', `id=${next.id}`);
		try {
			const res = await fetch(streamUrl(next.id), { headers: { Range: 'bytes=0-1' }, cache: 'no-store', signal });
			void res.body?.cancel();
			if (signal.aborted) return;
			if (res.ok) {
				const suffix = suffixFromContentType(res.headers.get('content-type'));
				if (suffix && next.suffix !== suffix) next.suffix = suffix;
				const depth = Number(res.headers.get('x-audio-bit-depth')) || undefined;
				const rate = Number(res.headers.get('x-audio-sample-rate')) || undefined;
				if (depth && next.bitDepth !== depth) next.bitDepth = depth;
				if (rate && next.sampleRate !== rate) next.sampleRate = rate;
			}
			logPlayback('warm-done', `id=${next.id} status=${res.status} ${Math.round(performance.now() - started)}ms`);
		} catch (e) {
			if (signal.aborted) return;
			logPlayback('warm-failed', `id=${next.id} ${Math.round(performance.now() - started)}ms ${e instanceof Error ? e.message : String(e)}`);
		}
	}

	private async learnCodec(song: Song, offline: boolean) {
		if (offline) return;
		if (!song.id.startsWith('st-') && !song.id.startsWith('yt-') && !song.id.startsWith('ytv-')) return;
		try {
			// The same two-byte probe `warmNext` uses; it also resolves a cold YouTube
			// track on the server, so it is not wasted work.
			const res = await fetch(streamUrl(song.id), { headers: { Range: 'bytes=0-1' }, cache: 'no-store' });
			const suffix = res.ok ? suffixFromContentType(res.headers.get('content-type')) : '';
			const depth = res.ok ? Number(res.headers.get('x-audio-bit-depth')) || undefined : undefined;
			const rate = res.ok ? Number(res.headers.get('x-audio-sample-rate')) || undefined : undefined;
			if (res.ok) void res.body?.cancel();
			if (!suffix) return;
			const target = this.queue[this.index];
			if (target && target.id === song.id) {
				if (target.suffix !== suffix) target.suffix = suffix;
				if (depth && target.bitDepth !== depth) target.bitDepth = depth;
				if (rate && target.sampleRate !== rate) target.sampleRate = rate;
			}
		} catch {
			/* the badge simply stays as it was */
		}
	}

	/** Seconds of audio buffered ahead of the current playback position. */
	private forwardBuffer(): number {
		const a = this.audio;
		const t = a.currentTime;
		try {
			for (let i = 0; i < a.buffered.length; i++) {
				if (t >= a.buffered.start(i) - 0.5 && t <= a.buffered.end(i)) {
					return Math.max(0, a.buffered.end(i) - t);
				}
			}
		} catch {
			/* element unloaded or invalid state */
		}
		return 0;
	}

	private prefetchInWindow() {
		const next = this.nextSong;
		const duration = this.duration || this.current?.duration || 0;
		if (next && duration > 0 && duration - this.time <= blobLeadFor(next.id)) void this.prefetchNextBlob();
	}

	/**
	 * Download the whole next track into memory while the current one still plays,
	 * so the `ended` handler can start it with no await and no network.
	 *
	 * Failures are retried a few times, spaced out, because the lead is long enough
	 * for a flaky or slow server to recover before the track ends.
	 */
	private async prefetchNextBlob() {
		const next = this.nextSong;
		if (!next || this.preparation?.id !== next.id || this.preparation.lookup) return;
		// Already streaming into the standby element: a download would only compete with it.
		if (this.standbyStreamId === next.id) return;
		if (this.nextReady?.id === next.id || this.blobJob?.id === next.id) return;

		// If the current track is streaming over HTTP (not playing from a local blob/offline copy),
		// don't compete with it if it is currently buffering or running low on forward buffer.
		if (!this.playingOffline && this.audio.src && !this.audio.src.startsWith('blob:')) {
			if (this.buffering || this.audio.readyState < HAVE_FUTURE_DATA) return;
			const dur = this.duration || this.current?.duration || 0;
			const remaining = Math.max(0, dur - this.time);
			const neededBuffer = Math.min(15, Math.max(0, remaining - 2));
			if (this.forwardBuffer() < neededBuffer) return;
		}

		const failure = this.blobFailure?.id === next.id ? this.blobFailure : null;
		if (
			failure &&
			(failure.final ||
				failure.count >= MAX_BLOB_ATTEMPTS ||
				performance.now() - failure.at < BLOB_RETRY_GAP_MS)
		) {
			return;
		}

		this.preparation.warm.abort();
		const job: BlobJob = { id: next.id, abort: new AbortController(), timedOut: false };
		this.blobJob = job;
		const timeout = setTimeout(() => {
			job.timedOut = true;
			job.abort.abort();
		}, BLOB_FETCH_TIMEOUT_MS);
		const attempt = (failure?.count ?? 0) + 1;
		logPlayback('blob-start', `id=${next.id} "${next.title}" attempt=${attempt}`);
		try {
			const res = await fetch(streamUrl(next.id), { signal: job.abort.signal });
			if (this.blobJob !== job) {
				void res.body?.cancel();
				return;
			}
			if (!res.ok) {
				void res.body?.cancel();
				throw new Error(`HTTP ${res.status}`);
			}
			// Some upstreams answer even an unrestricted GET with a partial range.
			// Only a range covering the entire file can be promoted to a playable blob.
			const range = res.headers.get('content-range');
			const complete = range?.match(/^bytes 0-(\d+)\/(\d+)$/i);
			if ((res.status === 206 || range) && (!complete || Number(complete[1]) + 1 !== Number(complete[2]))) {
				void res.body?.cancel();
				throw new Error('partial track');
			}
			const length = Number(res.headers.get('content-length') ?? 0);
			const encoding = res.headers.get('content-encoding')?.trim().toLowerCase();
			const expected = complete ? Number(complete[2]) : encoding && encoding !== 'identity' ? 0 : length;
			if (Math.max(length, expected) > MAX_BLOB_BYTES) {
				void res.body?.cancel();
				this.blobFailure = { id: next.id, count: attempt, at: performance.now(), final: true };
				logPlayback('blob-skipped', `id=${next.id} too large (${length} bytes), will stream`);
				return;
			}
			const blob = await readCapped(res, MAX_BLOB_BYTES);
			if (this.blobJob !== job) return;
			if (job.timedOut) throw new Error('timeout');
			if (!blob || blob.size > MAX_BLOB_BYTES) {
				this.blobFailure = { id: next.id, count: attempt, at: performance.now(), final: true };
				logPlayback('blob-skipped', `id=${next.id} too large (no length, > ${MAX_BLOB_BYTES} bytes), will stream`);
				return;
			}
			if (blob.size < MIN_BLOB_BYTES) throw new Error(`too small (${blob.size} bytes)`);
			if (expected && blob.size !== expected) throw new Error(`incomplete track (${blob.size}/${expected} bytes)`);
			const url = URL.createObjectURL(blob);
			this.nextReady?.copy.revoke();
			this.nextReady = { id: next.id, copy: { url, revoke: () => URL.revokeObjectURL(url) }, prefetched: true };
			this.primeStandby();
			this.blobFailure = null;
			logPlayback('blob-ready', `id=${next.id} bytes=${blob.size} type=${blob.type}`);
		} catch (e) {
			// Superseded by a newer job or by unload(): not a failure.
			if (this.blobJob !== job) return;
			this.blobFailure = { id: next.id, count: attempt, at: performance.now(), final: false };
			logPlayback(
				'blob-failed',
				`id=${next.id} attempt=${attempt} ${job.timedOut ? 'timeout' : e instanceof Error ? e.message : String(e)}`
			);
		} finally {
			clearTimeout(timeout);
			if (this.blobJob === job) this.blobJob = null;
		}
	}

	private play() {
		this.playRequested = true;
		this.output?.resume();
		this.armStallCheck();
		const token = this.loadToken;
		this.audio.play().then(() => {
			// A resolved promise means playback was accepted; reflect that immediately
			// in the controls even if no new canplay/playing event follows.
			if (token === this.loadToken && !this.audio.paused) {
				this.playing = true;
				this.buffering = false;
			}
		}).catch((e: unknown) => {
			logPlayback('play-rejected', e instanceof DOMException ? `${e.name}: ${e.message}` : String(e));
			// A track change interrupted this play(); the new source's `canplay` owns it now.
			if (token !== this.loadToken) return;
			// The source itself is unplayable: that is a track error, not a refused
			// autoplay. Send it through the retry/skip flow so a queue left playing with
			// the screen off keeps going.
			if (e instanceof DOMException && e.name === 'NotSupportedError') {
				this.onError();
				return;
			}
			// Aborted by a pause: the stall watchdog pauses a dead stream right before
			// queueing its reload, and clearing the request here would cancel that reload.
			// A user pause has already cleared the request itself.
			if (e instanceof DOMException && e.name === 'AbortError') return;
			// Refused (e.g. autoplay blocked): show it as paused rather than playing in silence.
			this.playRequested = false;
			this.playing = false;
			this.buffering = false;
			if (document.hidden) this.resumeWhenVisible = true;
		});
	}

	/** iOS may end another app's audio interruption while this page is hidden. */
	private resumeAfterInterruption() {
		if (document.hidden) {
			this.resumeWhenVisible = true;
			return;
		}
		this.play();
	}

	private readonly onVisibilityChange = () => {
		logPlayback('visibility', document.visibilityState);
		if (document.hidden) {
			// Invalidate the watchdog now, including a callback iOS may hold and release
			// on the next foreground.
			this.disarmStallCheck();
			if (this.playRequested && this.androidStartTime !== null) this.armStallCheck();
			return;
		}
		this.output?.resume();
		this.currentTime = this.time; // the UI copy was frozen while hidden
		if (!this.current) return;
		// "Playing" with no progress for a while: frozen since a call, or WebKit just held
		// loading in the background. Either way playback is wanted (after a call iOS
		// restarts the element itself, without our playRequested): play() below, then
		// make sure the clock moves (`watchFrozen` reloads it if not).
		const quiet = performance.now() - this.lastProgressAt;
		const stuck =
			!this.audio.paused &&
			this.audio.readyState >= HAVE_FUTURE_DATA &&
			quiet > FROZEN_MS;
		const stalledInBackground =
			(!this.audio.paused || this.playRequested || this.resumeWhenVisible) &&
			(this.buffering || this.audio.readyState < HAVE_FUTURE_DATA) &&
			quiet > STALL_TIMEOUT_MS;
		if (stuck) {
			logPlayback('stuck-on-visible', `t=${this.time.toFixed(1)}`);
			this.resumeWhenVisible = true;
		}
		if (stalledInBackground) {
			logPlayback('stalled-on-visible', `t=${this.time.toFixed(1)} quiet=${Math.round(quiet)}ms rs=${this.audio.readyState}`);
			this.resumeWhenVisible = true;
		}
		// Background time is not stalled time: the 15 s window starts now. (The play()
		// or load() below arms the watchdog again.)
		this.lastProgressAt = performance.now();
		// play() rejected in the background clears playRequested but sets
		// resumeWhenVisible, so both count as "playback was wanted".
		if (!this.playRequested && !this.resumeWhenVisible) return;
		this.playRequested = true;
		this.resumeWhenVisible = false;
		// A source that failed while the network was cut can't just be played again.
		// Nor can one this load already gave up on (see resume()).
		if (this.audio.error || !this.audio.src || stalledInBackground || this.errorHandledToken === this.loadToken) {
			this.errorRetriedFor = null;
			this.errorSkips = 0;
			this.load(this.index, true, this.time);
		} else {
			this.play();
			if (stuck) this.watchFrozen(this.audio);
		}
	};

	/**
	 * Checks, a moment after `playing`, that the clock actually moves. After a call iOS
	 * can resume the element into "playing" with no sound and a frozen clock, and the
	 * stall watchdog is off in the background, so nothing else notices. Visible: reload
	 * it where it was. Hidden: a reload there is refused, so `onVisibilityChange` does it.
	 */
	private watchFrozen(a: HTMLAudioElement) {
		const from = a.currentTime;
		const token = this.loadToken;
		setTimeout(() => {
			if (this.audio !== a || token !== this.loadToken || a.paused || a.currentTime !== from) return;
			// Still waiting for data: the stall watchdog's case, not this one.
			if (a.readyState < HAVE_FUTURE_DATA) return;
			logPlayback('frozen', `t=${from.toFixed(1)}`);
			if (document.hidden) this.resumeWhenVisible = true;
			else this.recoverFrozen();
		}, FROZEN_MS);
	}

	/** Reloads the current song where it froze and plays it. */
	private recoverFrozen() {
		this.errorRetriedFor = null;
		this.errorSkips = 0;
		void this.load(this.index, true, this.time);
	}

	/** Current playback position, even while the page is hidden. */
	get position() {
		return this.time;
	}

	private setTime(seconds: number) {
		this.time = seconds;
		this.currentTime = seconds;
	}

	private onTime() {
		const a = this.audio;
		if (a.seeking) return;
		if (this.streamOffsetUnverified) {
			// Nothing decoded yet from the new request: hold the clock at the seek target
			// (old element times or a 0 before the first frame must not move it).
			if (a.currentTime <= 0.05) {
				this.time = this.streamOffset;
				if (!document.hidden) this.currentTime = this.time;
				return;
			}
			// Decide once, on the first progress. The server re-encodes seeked streams so
			// they restart at ~0, but an older server (stream copy) keeps the original
			// timestamp and reports ~offset. Checking on every timeupdate instead would
			// mistake a restarted stream for a preserved one after (offset - 3) s of play.
			this.streamOffsetUnverified = false;
			if (a.currentTime >= this.streamOffset / 2) this.streamOffset = 0;
		}
		this.time = this.streamOffset + a.currentTime;
		if (!document.hidden) this.currentTime = this.time;
		const song = this.current;
		const dur = this.duration || song?.duration || 0;
		// Last.fm rule: scrobble after half the track or 4 minutes, whichever first.
		if (song && !this.scrobbled && dur > 30 && (this.time >= dur / 2 || this.time >= 240)) {
			this.scrobbled = true;
			// Singing along isn't listening to the song: karaoke versions aren't scrobbled.
			if (!song.karaokeOf) scrobble(song.id, true, song.coverArt);
		}
		if (a.currentTime !== this.lastProgressTime) {
			this.lastProgressTime = a.currentTime;
			this.lastProgressAt = performance.now();
		}
		if (this.androidStartTime !== null && a.currentTime !== this.androidStartTime) this.androidStartTime = null;
		// It has played for a while: a later failure is a new incident and gets its own retry.
		if (this.time > ERROR_REARM_AFTER_S) this.errorRetriedFor = null;
		// Resync the lock-screen position once per 5 s slot. (A `% 5` check stays true
		// for a whole second, i.e. ~4 timeupdates, each a round trip to the OS.)
		const slot = Math.floor(this.time / 5);
		if (slot !== this.lastPositionSlot) {
			this.lastPositionSlot = slot;
			this.updatePosition();
		}
		this.prefetchInWindow();
		// The real audio is over; don't sit through the silence up to the misread length.
		// Switch straight to the next track without pausing: on a hidden iOS page a pause
		// ends the audio session, and the next track then never gets past its metadata.
		// load() clears durationCap synchronously, so this can't fire twice.
		if (this.durationCap && !a.paused && this.time >= this.durationCap - 0.25) {
			logPlayback('capped-end', `cap=${this.durationCap} media=${a.duration}`);
			this.onEnded();
			return;
		}
		if (dur <= 0 || a.paused || this.repeat === 'one' || sleepTimer.isEndOfTrack) return;
		const remaining = dur - this.time;
		if (remaining <= STREAM_STANDBY_LEAD_S) this.standbyNextStream();
		const source = this.standbySource();
		const ready = !!source && this.standbyHolds(source.url);
		// Crossfade: start the next song under this one's last seconds. The switch to it
		// waits for `ended`, which also keeps some element playing throughout.
		const fade = this.crossfadeFor(dur);
		if (fade && ready && !this.preroll && remaining <= fade) this.startPreroll(remaining);
		// Otherwise hand over to the standby element just before the end, while this
		// one still plays.
		if (!this.preroll && ready && remaining <= HANDOFF_LEAD_S) {
			logPlayback(
				'handoff-early',
				`t=${a.currentTime.toFixed(2)} dur=${dur.toFixed(2)} ${source!.kind} standby rs=${this.standby?.readyState}`
			);
			this.onEnded();
		}
	}

	private applyDuration() {
		const d = this.audio.duration;
		if (!Number.isFinite(d) || d <= 0) return;
		const reported = this.current?.duration ?? 0;
		if (this.streamOffset > 0 && reported > 0) {
			this.duration = reported;
			return;
		}
		if (reported > 0 && d > reported * DURATION_MISMATCH_RATIO) {
			this.duration = reported;
			this.durationCap = reported;
		} else {
			this.duration = d;
			this.durationCap = 0;
		}
	}

	// ---- stalled streams -----------------------------------------------------
	// A stream that stops delivering (network drop, Wi-Fi/4G switch, a load that never
	// reaches `canplay`) leaves playback wanted but the clock frozen; only a fresh
	// request at the current position recovers it.

	// A self-rescheduling timeout, not a fixed 2 s interval: while playback is
	// progressing it only wakes about once per STALL_TIMEOUT_MS (instead of ~30
	// times a minute for the whole session, screen off included). `onTime` keeps
	// `lastProgressAt` fresh, so nothing has to be re-armed per timeupdate.
	//
	// Only Android startup is watched while hidden. WebKit may hold a timer until
	// the screen unlocks and refuses a background reload, so it recovers on visible.
	private armStallCheck() {
		this.lastProgressAt = performance.now();
		if (document.hidden && this.androidStartTime === null) return;
		if (this.stallTimer === undefined) this.scheduleStallCheck(STALL_TIMEOUT_MS);
	}

	private scheduleStallCheck(ms: number) {
		const delay = Math.max(STALL_CHECK_MS, ms);
		const gen = this.stallGen;
		this.stallDueAt = performance.now() + delay;
		this.stallTimer = setTimeout(() => this.checkStall(gen), delay);
	}

	private disarmStallCheck() {
		clearTimeout(this.stallTimer);
		this.stallTimer = undefined;
		// Void a callback the browser may already have queued (clearTimeout can't recall it).
		this.stallGen++;
	}

	private readonly checkStall = (gen: number) => {
		if (gen !== this.stallGen) return;
		this.stallTimer = undefined;
		if (!this.playRequested || !this.current) return;
		if (document.hidden) {
			if (this.androidStartTime === null) return;
			// Screen-off timeupdate events can be throttled while native playback
			// continues. Only a genuinely stationary incoming element needs a retry.
			if (this.audio.currentTime !== this.androidStartTime || (this.audio.paused && !this.buffering)) {
				this.androidStartTime = null;
				return;
			}
		}
		// A late visible callback may have been held by the OS. During hidden Android
		// startup the native clock check above already confirmed the source is still stuck.
		const late = performance.now() - this.stallDueAt;
		if (!document.hidden && late > STALL_LATE_MS) {
			logPlayback('stall-check-late', `${Math.round(late)}ms, ignored`);
			this.lastProgressAt = performance.now();
			return this.scheduleStallCheck(STALL_TIMEOUT_MS);
		}
		// Paused by the system (a call) rather than stuck loading: not a stall.
		// Keep the old 2 s cadence so a stall right after it resumes is still caught.
		if (this.audio.paused && !this.buffering) return this.scheduleStallCheck(STALL_CHECK_MS);
		const quiet = performance.now() - this.lastProgressAt;
		// Still progressing: check again when the timeout could next be reached.
		if (quiet < STALL_TIMEOUT_MS) return this.scheduleStallCheck(STALL_TIMEOUT_MS - quiet);
		// Stop the dead request and let the higher-level error flow retry once,
		// then fallback or skip the track.
		// Still loading as far as the request goes: the `pause` handler keeps
		// playRequested while buffering, so the retry below isn't cancelled.
		this.buffering = true;
		if (!document.hidden) this.audio.pause();
		logPlayback('stall-timeout', `t=${this.time.toFixed(1)} rs=${this.audio.readyState}`);
		// Already handled for this load (onError would ignore it): never leave the
		// spinner up with nothing re-armed; stop and show the error instead.
		if (this.errorHandledToken === this.loadToken) return this.giveUp();
		this.onError();
	};

	/**
	 * The source failed (often the server couldn't resolve a YouTube track). Reload it
	 * once, then skip to the next track so a queue left playing with the screen off
	 * keeps going. Every step waits a moment, and if several different tracks fail in
	 * a row the player gives up on the first failed one instead of cycling the queue.
	 */
	private onError() {
		const a = this.audio;
		const song = this.current;
		if (!a.src || !song) return;
		// One failure can arrive as an `error` event, a rejected play() and a stall
		// timeout. Handle it once per load, or retry and skip would both fire.
		if (this.errorHandledToken === this.loadToken) return;
		this.errorHandledToken = this.loadToken;
		logPlayback('media-error', `id=${song.id} code=${a.error?.code} ${a.error?.message ?? ''}`);
		void this.probeSource(a.src);
		let retryStream = !a.src.startsWith('blob:');
		if (this.playingOffline?.id === song.id && this.playingOffline.copy.url === a.src) {
			// Re-fetch a failed network prefetch. An explicitly saved track can be
			// reopened from its on-device copy.
			retryStream = this.playingOffline.prefetched === true;
			this.releaseOffline();
		}
		// If WebKit destroyed the element's hardware decoder (MEDIA_ERR_DECODE),
		// replace the element itself before retrying, as reusing a broken element
		// often yields silence or frozen playback.
		if (a.error?.code === 3) {
			logPlayback('replace-audio', 'recreating element after MEDIA_ERR_DECODE');
			this.replaceActiveElement();
		}
		if (this.playRequested && this.errorRetriedFor !== song.id) {
			this.errorRetriedFor = song.id;
			this.afterErrorDelay(() => this.load(this.index, true, this.time, retryStream ? 'stream-retry' : 'selected'));
			return;
		}
		this.error = `Couldn't play “${song.title}”`;
		if (this.playRequested && nextIndex(this.queue.length, this.index, this.repeat) >= 0 && this.errorSkips < MAX_ERROR_SKIPS) {
			if (this.errorSkips === 0) this.errorOriginIndex = this.index;
			this.errorSkips++;
			this.afterErrorDelay(() => this.next(true));
			return;
		}
		this.giveUp();
	}

	/**
	 * Stop retrying. Several different tracks failed in a row: that is the connection
	 * or the session, not the tracks. Go back to the first failed track instead of
	 * burning through the queue, and resume it when the app is visible again.
	 */
	private giveUp() {
		if (!this.error && this.current) this.error = `Couldn't play “${this.current.title}”`;
		if (this.errorOriginIndex >= 0 && this.errorOriginIndex !== this.index && this.queue[this.errorOriginIndex]) {
			this.index = this.errorOriginIndex;
			this.setTime(0);
			this.duration = this.current?.duration ?? 0;
			setMediaMetadata(this.current);
		}
		this.errorOriginIndex = -1;
		this.clearPreparation();
		const wanted = this.playRequested;
		// A failed element can still report "not paused": stop it so the button shows play.
		this.pause();
		this.playing = false;
		// pause() clears the flag; set it afterwards so onVisibilityChange can recover.
		this.resumeWhenVisible = wanted;
	}

	/** Run a retry/skip after a short pause, unless the user moved on in the meantime. */
	private afterErrorDelay(fn: () => void) {
		clearTimeout(this.errorTimer);
		const token = this.loadToken;
		this.errorTimer = setTimeout(() => {
			if (this.destroyed || token !== this.loadToken || !this.playRequested) return;
			fn();
		}, ERROR_RETRY_DELAY_MS);
	}

	/**
	 * <audio> only says "Format error" for any HTTP failure. Ask the same URL for two
	 * bytes and log the status (and the start of an error body), so 401/403 (expired
	 * credential), 429 (rate limit) and 5xx (server) can be told apart in the log.
	 */
	private async probeSource(src: string) {
		if (src.startsWith('blob:')) return;
		const now = performance.now();
		if (now - this.lastProbeAt < PROBE_MIN_GAP_MS) return;
		this.lastProbeAt = now;
		try {
			const res = await fetch(src, { headers: { Range: 'bytes=0-1' }, cache: 'no-store' });
			let detail = '';
			if (res.ok) void res.body?.cancel();
			else detail = ` body=${(await res.text()).slice(0, 160).replace(/\s+/g, ' ')}`;
			logPlayback(
				'probe',
				`status=${res.status} type=${res.headers.get('content-type')} len=${res.headers.get('content-length')} range=${res.headers.get('content-range')}${detail}`
			);
		} catch (e) {
			logPlayback('probe-failed', e instanceof Error ? e.message : String(e));
		}
	}

	private onEnded() {
		if (sleepTimer.onTrackEnded()) {
			logPlayback('sleep-timer-stop');
			return;
		}
		if (this.repeat === 'one') {
			// A crossfade already under way would keep the next song playing unseen.
			this.cancelPreroll();
			this.scrobbled = false;
			// An offline copy rewinds in place; only a live server-seek stream is re-requested.
			if (!this.playingOffline && this.current && seeksOnServer(this.current)) {
				this.restartStreamAt(0);
			} else {
				this.streamOffset = 0;
				this.streamOffsetUnverified = false;
				this.audio.currentTime = 0;
			}
			this.play();
			return;
		}
		this.next(true);
	}

	private updatePosition() {
		setMediaPosition(this.duration, this.time, this.audio.playbackRate);
	}

	private releaseOffline() {
		this.playingOffline?.copy.revoke();
		this.playingOffline = null;
	}

	/** Unload the element so it stops fetching and releases the stream. */
	private unload() {
		this.cancelPreroll();
		this.loadToken++;
		this.androidStartTime = null;
		this.playRequested = false;
		this.resumeWhenVisible = false;
		this.buffering = false;
		this.audio.pause();
		this.audio.removeAttribute('src');
		this.audio.load();
		this.disarmStallCheck();
		clearTimeout(this.errorTimer);
		this.errorOriginIndex = -1;
		this.clearPreparation();
		this.releaseOffline();
		this.retiring?.copy?.copy.revoke();
		this.retiring?.listeners.abort();
		this.retiring = null;
		this.standbyFor = null;
		this.standbyStreamId = null;
		if (this.standby) {
			this.standby.autoplay = false;
			this.standby.pause();
			this.standby.removeAttribute('src');
			this.standby.load();
		}
	}

	private readonly onPageHide = (e: PageTransitionEvent) => {
		logPlayback('pagehide', `persisted=${e.persisted}`);
		if (e.persisted) return;
		this.saveSnapshot();
		clearMediaSession();
	};

	// ---- persistence ---------------------------------------------------------

	private saveSnapshot() {
		writeSnapshot({
			queue: this.queue,
			index: this.index,
			time: this.time,
			shuffle: this.shuffle,
			repeat: this.repeat,
			unshuffled: this.unshuffled
		});
	}

	/** Local snapshot always; the server copy (cross-device resume) on pause and track change. */
	private persist(toServer = false) {
		// A replaced instance must never write over its successor's snapshot.
		if (this.destroyed) return;
		clearTimeout(this.saveTimer);
		this.saveTimer = setTimeout(() => this.saveSnapshot(), 300);
		if (toServer || !this.playing) {
			savePlayQueue(
				this.queue.map((s) => s.id),
				this.current?.id,
				Math.floor(this.time * 1000)
			);
		}
	}

	private async restore() {
		const saved = readSnapshot();
		if (saved) {
			// Karaoke mixes only live as long as the server's session: drop them on restart.
			const queue = saved.queue.filter((s) => !s.karaokeOf);
			this.queue = queue;
			this.index = queue.length ? Math.min(Math.max(saved.index, 0), queue.length - 1) : -1;
			this.setTime(saved.time);
			this.duration = this.current?.duration ?? 0;
			this.shuffle = saved.shuffle;
			this.repeat = saved.repeat;
			this.unshuffled = saved.unshuffled?.filter((s) => !s.karaokeOf) ?? null;
			setMediaMetadata(this.current);
			if (this.queue.length) return;
		}
		const remote = await getPlayQueue();
		if (remote && !this.queue.length) {
			this.queue = remote.songs;
			this.index = Math.max(0, remote.songs.findIndex((s) => s.id === remote.current));
			this.setTime(remote.position / 1000);
			this.duration = this.current?.duration ?? 0;
			setMediaMetadata(this.current);
		}
	}

	// ---- lifecycle -----------------------------------------------------------

	/** Stops and releases the audio element so it can never keep playing orphaned. */
	destroy() {
		this.saveSnapshot();
		this.cancelKaraoke();
		this.destroyed = true;
		this.unload();
		this.singleTab.close();
		this.stopRemoteWatch();
		this.stopRemoteWatch = () => {};
		this.interruptions.dispose();
		this.stopDiagnostics();
		this.stopAudioSettings();
		this.stopDiagnostics = () => {};
		this.audio.remove();
		this.standby?.remove();
		this.output?.dispose();
		window.removeEventListener('pagehide', this.onPageHide);
		document.removeEventListener('visibilitychange', this.onVisibilityChange);
		clearTimeout(this.saveTimer);
	}

	/** Called on sign-out. */
	reset() {
		this.unload();
		this.cancelKaraoke();
		this.karaoke = null;
		this.queue = [];
		this.index = -1;
		this.setTime(0);
		this.duration = 0;
		this.error = null;
		this.buffering = false;
		this.unshuffled = null;
		clearSnapshot();
	}
}

let instance: Player | null = null;

// The live player is also kept on globalThis. A dev hot-reload of this module (or of
// anything it imports, like api.ts) re-runs it and would create a second Player with
// its own <audio> while the old one keeps playing. Vite only runs dispose hooks for
// the edited module itself, so the registry is what reliably catches that case.
const registry = globalThis as { __musicPlayer?: Player };

/** Created lazily, after sign-in, so restore() has a session to talk to. */
export function getPlayer() {
	if (!instance) {
		const prev = registry.__musicPlayer;
		// (An instance from an older module version may predate `position`.)
		const handoff = prev ? { time: prev.position ?? prev.currentTime, playing: prev.playing } : null;
		// Destroy first: it writes the queue snapshot the new instance restores from.
		prev?.destroy();
		instance = new Player();
		registry.__musicPlayer = instance;
		if (handoff) {
			instance.seek(handoff.time);
			if (handoff.playing) instance.resume();
		}
	}
	return instance;
}

export type { Player };
