import adapter from '@sveltejs/adapter-static';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

/** @type {import('@sveltejs/kit').Config} */
const config = {
	preprocess: vitePreprocess(),
	kit: {
		// Pure SPA: no server at runtime. Data comes from the remote a3watch
		// agent API over the cloudflared tunnel; Vercel serves only static assets.
		adapter: adapter({
			fallback: 'index.html',
			precompress: false,
			strict: false
		})
	}
};

export default config;
