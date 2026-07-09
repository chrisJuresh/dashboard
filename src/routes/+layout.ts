// Pure client-side SPA. No SSR/prerender: the agent API is remote (reached over
// the cloudflared tunnel from the browser), so nothing is rendered on Vercel's
// servers — they only ship the static shell.
export const ssr = false;
export const prerender = false;
export const trailingSlash = 'ignore';
