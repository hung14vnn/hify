// Prefer a colour that actually occurs in the cover. Complementary colours
// would cancel each other out if the whole image were averaged in RGB.
import { coverUrl } from './api';
import { hue } from './format';

export interface Tint {
	top: string;
	bottom: string;
	accent?: string;
	glow?: string;
	ambient?: string;
}

const cache = new Map<string, Tint | null>();

function fallbackTint(seed: string | undefined): Tint {
	const h = hue(seed ?? 'music');
	return {
		top: `hsl(${h} 45% 26%)`,
		bottom: `hsl(${h} 35% 12%)`,
		accent: `hsl(${h} 65% 48%)`,
		glow: `hsl(${h} 65% 55% / 0.45)`
	};
}

function rgbToHsl(r: number, g: number, b: number): [number, number, number] {
	r /= 255;
	g /= 255;
	b /= 255;
	const max = Math.max(r, g, b);
	const min = Math.min(r, g, b);
	const l = (max + min) / 2;
	if (max === min) return [0, 0, l];
	const d = max - min;
	const s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
	const h = max === r ? (g - b) / d + (g < b ? 6 : 0) : max === g ? (b - r) / d + 2 : (r - g) / d + 4;
	return [h * 60, s, l];
}

function measure(img: HTMLImageElement): Tint | null {
	const size = 24;
	const canvas = document.createElement('canvas');
	canvas.width = canvas.height = size;
	const ctx = canvas.getContext('2d', { willReadFrequently: true });
	if (!ctx) return null;
	ctx.drawImage(img, 0, 0, size, size);
	// Throws for cross-origin art served without CORS; caller falls back to the blur.
	const { data } = ctx.getImageData(0, 0, size, size);
	let r = 0;
	let g = 0;
	let b = 0;
	let weight = 0;
	let visibleWeight = 0;
	const bands = Array.from({ length: 12 }, () => ({ r: 0, g: 0, b: 0, weight: 0 }));

	for (let i = 0; i < data.length; i += 4) {
		const alpha = data[i + 3] / 255;
		const [ph, ps, pl] = rgbToHsl(data[i], data[i + 1], data[i + 2]);
		const chroma = ps * (1 - Math.abs(2 * pl - 1));
		const w = 0.15 + chroma;
		r += data[i] * w;
		g += data[i + 1] * w;
		b += data[i + 2] * w;
		weight += w;
		visibleWeight += w * alpha;

		const band = bands[Math.round(ph / 30) % bands.length];
		const strength = chroma * alpha;
		band.r += data[i] * strength;
		band.g += data[i + 1] * strength;
		band.b += data[i + 2] * strength;
		band.weight += strength;
	}
	const average = rgbToHsl(r / weight, g / weight, b / weight);
	// Album, artist and playlist headers use the original average-colour tint.
	const [avgHue, avgSat, avgLight] = average;
	const topSaturation = Math.min(avgSat * 1.35, 0.70) * 100;
	const topLightness = (Math.min(Math.max(avgLight, 0.20), 0.44) + 0.05) * 100;
	const dominant = bands.reduce((best, band) => band.weight > best.weight ? band : best);
	// A tiny coloured logo should not turn an otherwise neutral cover neon.
	const [h, s, l] = dominant.weight > visibleWeight * 0.08
		? rgbToHsl(dominant.r / dominant.weight, dominant.g / dominant.weight, dominant.b / dominant.weight)
		: average;
	const sat = Math.min(s * 1.15, 0.80) * 100;
	const light = Math.min(Math.max(l, 0.25), 0.42);

	return {
		top: `hsl(${avgHue.toFixed(0)} ${topSaturation.toFixed(0)}% ${topLightness.toFixed(0)}%)`,
		ambient: `hsl(${h.toFixed(0)} ${sat.toFixed(0)}% ${((light + 0.05) * 100).toFixed(0)}%)`,
		bottom: `hsl(${h.toFixed(0)} ${(sat * 0.9).toFixed(0)}% ${(light * 0.70 * 100).toFixed(0)}%)`,
		accent: `hsl(${h.toFixed(0)} ${sat.toFixed(0)}% 52%)`,
		glow: `hsl(${h.toFixed(0)} ${sat.toFixed(0)}% 55% / 0.45)`
	};
}

/**
 * Resolves synchronously (a plain value) once a cover has been measured, so pages that
 * are revisited can paint their tint in the same frame; `await` works on either form.
 */
export function artworkTint(coverArt: string | undefined): Tint | null | Promise<Tint | null> {
	if (!coverArt) return fallbackTint('default');
	if (cache.has(coverArt)) return cache.get(coverArt)!;
	const src = coverUrl(coverArt, 64);
	if (!src) return fallbackTint(coverArt);
	return new Promise((resolve) => {
		const img = new Image();
		img.crossOrigin = 'anonymous';
		img.decoding = 'async';
		img.onload = () => {
			let tint: Tint | null = null;
			try {
				tint = measure(img);
			} catch {
				tint = fallbackTint(coverArt);
			}
			if (!tint) tint = fallbackTint(coverArt);
			cache.set(coverArt, tint);
			resolve(tint);
		};
		img.onerror = () => {
			const tint = fallbackTint(coverArt);
			cache.set(coverArt, tint);
			resolve(tint);
		};
		img.src = src;
	});
}
