// Upstream imports "mapbox-gl/dist/mapbox-gl-csp-worker" and accepts either a worker URL
// or a worker class; under Vite the script is served as a URL (see vite.config.ts).
export { default } from "mapbox-gl/dist/mapbox-gl-csp-worker.js?url";
