/// <reference types="vite/client" />

// Optional build-time config for zero-entry auto-connect. Set these as Vercel
// environment variables. VITE_A3WATCH_TOKEN is inlined into the client bundle,
// so only set it when the deployment is protected by Vercel Authentication.
interface ImportMetaEnv {
	readonly VITE_A3WATCH_API?: string;
	readonly VITE_A3WATCH_TOKEN?: string;
}
interface ImportMeta {
	readonly env: ImportMetaEnv;
}
