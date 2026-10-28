// The upstream extension's webview sources are bundled as they are (upstream's own
// build is transpile-only too) and are not type-checked with this app's stricter
// settings: imports from "@upstream/..." are typed loosely here.
declare module "@upstream/*";

declare module "virtual:upstream-replay.css";
