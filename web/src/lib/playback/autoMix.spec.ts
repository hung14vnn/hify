import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

describe('AutoMix metadata', () => {
	beforeEach(() => {
		vi.resetModules();
		localStorage.setItem('music.session', JSON.stringify({ base: 'https://music.test/subsonic', apiKey: 'test-session' }));
	});

	afterEach(() => {
		localStorage.clear();
		vi.unstubAllGlobals();
		vi.restoreAllMocks();
	});

	it('reads per-track analysis through the signed-in Subsonic session and skips analysis on offset streams', async () => {
		const metadata = { introSeconds: 4.25, outroSeconds: 7 };
		const fetcher = vi.fn().mockResolvedValue(Response.json({ 'subsonic-response': { status: 'ok', autoMixInfo: metadata } }));
		vi.stubGlobal('fetch', fetcher);
		const { getAutoMixInfo, streamUrl } = await import('../api');
		const signal = new AbortController().signal;
		await expect(getAutoMixInfo('a/b', 'captured', signal)).resolves.toEqual(metadata);
		const [target, init] = fetcher.mock.calls[0];
		const url = new URL(target);
		expect(url.pathname).toBe('/subsonic/rest/getAutoMixInfo');
		expect(url.searchParams.get('id')).toBe('a/b');
		expect(url.searchParams.get('mixToken')).toBe('captured');
		expect(url.searchParams.get('apiKey')).toBe('test-session');
		expect(init).toMatchObject({ cache: 'no-store', signal });
		expect(new URL(streamUrl('a')).searchParams.has('autoMix')).toBe(false);
		const complete = new URL(streamUrl('a', 0, 0, undefined, 'captured'));
		expect(complete.searchParams.get('autoMix')).toBe('1');
		expect(complete.searchParams.get('mixToken')).toBe('captured');
		const partial = new URL(streamUrl('a', 0, 30, undefined, 'partial'));
		expect(partial.searchParams.has('autoMix')).toBe(false);
		expect(partial.searchParams.has('mixToken')).toBe(false);
	});

	it('caps retries per track and reserves reads for the last 45 and 15 seconds', async () => {
		const fetcher = vi.fn(async () => Response.json({ 'subsonic-response': { status: 'ok' } }));
		vi.stubGlobal('fetch', fetcher);
		const { AutoMixPair } = await import('./autoMix');
		const pair = new AutoMixPair();
		const outgoing = { id: 'a', mixToken: 'first' };
		const incoming = { id: 'b', mixToken: 'next' };
		let now = 0;
		vi.spyOn(performance, 'now').mockImplementation(() => now);
		for (const [remaining, calls] of [[300, 2], [250, 4], [200, 4], [45, 6], [30, 6], [15, 8], [8, 8]]) {
			pair.prepare(outgoing, incoming, remaining);
			await new Promise((resolve) => setTimeout(resolve, 0));
			expect(fetcher).toHaveBeenCalledTimes(calls);
			expect(pair.overlap(outgoing, incoming)).toBe(0);
			now += 10_000;
		}
		pair.clear();
	});
});
