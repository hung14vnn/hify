import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

describe('Last.fm connect', () => {
	beforeEach(() => {
		vi.resetModules();
		localStorage.clear();
		localStorage.setItem('music.session', JSON.stringify({
			base: 'https://music.test/hify-api', apiKey: 'test-session'
		}));
	});

	afterEach(() => {
		vi.unstubAllGlobals();
		localStorage.clear();
	});

	it('uses the API base and a bearer header without exposing the session in the URL', async () => {
		const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ url: 'https://www.last.fm/api/auth/?api_key=test' })));
		vi.stubGlobal('fetch', fetcher);
		const { connectLastfm } = await import('./api');
		await expect(connectLastfm()).resolves.toContain('https://www.last.fm/api/auth/');
		expect(fetcher).toHaveBeenCalledWith('https://music.test/hify-api/api/v1/lastfm/connect', {
			headers: { Authorization: 'Bearer test-session' }, cache: 'no-store', credentials: 'include'
		});
	});

	it.each([
		[401, 'Session expired. Sign in again.'],
		[503, 'Last.fm API key is not configured'],
		[502, 'Last.fm is temporarily unavailable']
	])('preserves server error details for HTTP %s', async (status, detail) => {
		vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail }), { status })));
		const { connectLastfm } = await import('./api');
		await expect(connectLastfm()).rejects.toThrow(detail);
	});

	it('handles a proxy HTML error without misreporting missing configuration', async () => {
		vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('<html>Bad gateway</html>', { status: 502 })));
		const { connectLastfm } = await import('./api');
		await expect(connectLastfm()).rejects.toThrow('Couldn’t connect to Last.fm (HTTP 502)');
	});

	it('rejects a missing authorization URL', async () => {
		vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{}')));
		const { connectLastfm } = await import('./api');
		await expect(connectLastfm()).rejects.toThrow('Last.fm did not return a sign-in page');
	});

	it('does not send a request when signed out', async () => {
		localStorage.clear();
		const fetcher = vi.fn();
		vi.stubGlobal('fetch', fetcher);
		const { connectLastfm } = await import('./api');
		await expect(connectLastfm()).rejects.toThrow('Not signed in');
		expect(fetcher).not.toHaveBeenCalled();
	});
});
