import { getAutoMixInfo, type AutoMixInfo } from '../api';

const MAX_OVERLAP = 12;

export interface MixSource {
	id: string;
	mixToken?: string;
	/** The whole response is held in a local blob; later network ranges cannot change it. */
	prefetched?: boolean;
}

class TrackAnalysis {
	private info: AutoMixInfo | null = null;
	private request: AbortController | null = null;
	private finalWindow = false;
	private attempts = 0;
	private retryAt = 0;

	constructor(readonly id: string, readonly token: string) {}

	cues(source: MixSource) {
		return source.prefetched || this.finalWindow ? this.info : null;
	}

	cancel() {
		this.request?.abort();
		this.request = null;
	}

	check(remaining: number, prefetched: boolean) {
		// A later range or provider fallback may invalidate a streaming source after
		// an early positive result. Use only a fresh binding as the mix approaches.
		const verifyStream = !prefetched && remaining <= MAX_OVERLAP + 3;
		if (verifyStream) {
			if (this.finalWindow) return;
			this.info = null;
			this.cancel();
		}
		this.finalWindow = verifyStream;
		if (this.info || this.request || (!verifyStream && performance.now() < this.retryAt)) return;
		// Two early reads cover fast prefetches. Reserve two more for the end of a
		// long stream, when the server is more likely to have received its tail.
		const limit = remaining <= MAX_OVERLAP + 3 ? 4 : remaining <= 45 ? 3 : 2;
		if (this.attempts >= limit) return;
		this.attempts++;
		const request = new AbortController();
		this.request = request;
		const timeout = setTimeout(() => request.abort(), 2_000);
		void getAutoMixInfo(this.id, this.token, request.signal).then((info) => {
			if (!request.signal.aborted) this.info = info;
		}).catch(() => {
			// Offline, older servers and unanalysed tracks all use the ordinary fade.
		}).finally(() => {
			clearTimeout(timeout);
			if (this.request !== request) return;
			this.request = null;
			this.retryAt = performance.now() + 5_000;
		});
	}
}

/** Keep only the active pair. A late request updates only the track that issued it. */
export class AutoMixPair {
	private tracks = new Map<string, TrackAnalysis>();

	prepare(outgoing: MixSource | null, incoming: MixSource | null, remaining: number) {
		for (const [token, track] of this.tracks) {
			if (token === outgoing?.mixToken || token === incoming?.mixToken) continue;
			track.cancel();
			this.tracks.delete(token);
		}
		for (const source of [outgoing, incoming]) {
			if (!source?.mixToken) continue;
			let track = this.tracks.get(source.mixToken);
			if (!track) {
				track = new TrackAnalysis(source.id, source.mixToken);
				this.tracks.set(source.mixToken, track);
			}
			track.check(remaining, source.prefetched === true);
		}
	}

	overlap(outgoing: MixSource | null, incoming: MixSource | null): number {
		if (!outgoing?.mixToken || !incoming?.mixToken) return 0;
		const outro = this.tracks.get(outgoing.mixToken)?.cues(outgoing)?.outroSeconds ?? 0;
		const intro = this.tracks.get(incoming.mixToken)?.cues(incoming)?.introSeconds ?? 0;
		const seconds = Math.min(outro, intro, MAX_OVERLAP);
		return seconds >= 1 ? seconds : 0;
	}

	clear() {
		for (const track of this.tracks.values()) track.cancel();
		this.tracks.clear();
	}
}
