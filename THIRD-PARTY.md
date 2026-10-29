# Third-party code

## `plugins/teamboard/ui/panel.html` — bundled MCP Apps SDK

The inline panel ships a minified bundle (~599 KB, starting at `ui/panel.html:675`) that exposes
`window.MCPApps`. It is the MCP Apps client SDK used to talk to the host:

- `@modelcontextprotocol/ext-apps` (MIT) — `App`, `ui/initialize`, `ui/notifications/*`,
  `ui/download-file`, `ui/message`
- `zod` (MIT) — JSON-schema validation of host messages
- one embedded notice for an Express-family package (`Copyright(c) 2015 Douglas Christopher Wilson`,
  MIT) pulled in transitively

**Provenance is not recorded in the bundle and no version string is embedded.** Before shipping a
release, regenerate it from a pinned source and append the license headers:

```bash
npm i -D esbuild @modelcontextprotocol/ext-apps@<pin>
npx esbuild app.ts --bundle --minify --format=iife --global-name=MCPAppsClient \
  --banner:js="/* @modelcontextprotocol/ext-apps <ver> — MIT */" -o bundle.js
```

Then replace the `<script>…</script>` block in `ui/panel.html` and bump `UI_URI` in `mcp_server.py`
(or let `scripts/sync_version.py` derive it from the file hash).

MIT is compatible with this project's MIT license, but the notice must travel with the bundle.
