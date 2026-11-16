import { execSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vitest/config';

// In dev, the API runs on :8089 and the player on :5180, so /rest and /api
// are proxied to keep the default "same origin" setup working without CORS.
const backend = process.env.PLAYER_BACKEND ?? 'http://127.0.0.1:8089';

function git(args: string): string {
	try {
		return execSync(`git ${args}`, { stdio: ['ignore', 'pipe', 'ignore'] }).toString().trim();
	} catch {
		return '';
	}
}

/** Shown on the Account page and stamped into the playback log, to tell builds apart on a device. */
function buildInfo() {
	const { version } = JSON.parse(readFileSync(new URL('./package.json', import.meta.url), 'utf8'));
	const commit = git('rev-parse --short HEAD') || 'unknown';
	const dirty = git('status --porcelain -- .') !== '';
	return { version, commit: dirty ? `${commit}-dirty` : commit, time: new Date().toISOString() };
}

export default defineConfig({
	plugins: [sveltekit()],
	// PUBLIC_API_BASE lets a separately-hosted frontend point at the VM's API.
	envPrefix: ['PUBLIC_', 'VITE_'],
	define: {
		__BUILD__: JSON.stringify(buildInfo())
	},
	test: {
		environment: 'jsdom',
		include: ['src/**/*.spec.ts'],
		// Player tests re-import the store per test (vi.resetModules); compiling it can
		// exceed the 5s default while other files run in parallel.
		testTimeout: 30_000
	},
	server: {
		port: 5180,
		proxy: {
			'/rest': { target: backend, changeOrigin: true },
			'/api': { target: backend, changeOrigin: true }
		}
	}
});
