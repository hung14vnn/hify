import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

describe('YouTube search source', () => {
	beforeEach(() => {
		vi.resetModules();
		localStorage.setItem('music.session', JSON.stringify({ base: 'https://music.test', apiKey: 'test-session' }));
	});

	afterEach(() => {
		vi.unstubAllGlobals();
		localStorage.clear();
	});

	it.each(['spotify', 'ytmusic', 'youtube'] as const)('sends the distinct %s catalog to search3', async (source) => {
		const song = { id: 'ytv-abcdefghijk', title: 'Independent upload', artist: 'Channel' };
		const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({
			'subsonic-response': { status: 'ok', searchResult3: { song: [song] } }
		})));
		vi.stubGlobal('fetch', fetcher);
		const { search } = await import('./api');
		const result = await search('independent upload', undefined, 0, false, source);
		const url = new URL(String(fetcher.mock.calls[0][0]));
		expect(url.pathname).toBe('/rest/search3');
		expect(url.searchParams.get('source')).toBe(source);
		expect(result.songs).toEqual([song]);
		expect(result.artists).toEqual([]);
		expect(result.albums).toEqual([]);
	});
});
