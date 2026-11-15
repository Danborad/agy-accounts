# Vendored frontend: anglee0323/agy-switch

This directory contains the web frontend of
[anglee0323/agy-switch](https://github.com/anglee0323/agy-switch),
vendored so that `agy-accounts` can serve the same UI from its Python backend.

- Upstream version: 4.10.16
- Upstream commit: `db370ab5d12859f78a30c4acc4a0998a6fe8ce02`
- License: CC BY-NC-SA 4.0 (see `LICENSE`)
- Copyright: anglee0323 and contributors

## Modifications

- `vite.config.ts`: `base` changed from `./` to `/` so the SPA can be served
  from the root path with client-side routes.
- `index.html`: title changed, and `public/tauri-shim.js` is loaded before the
  app bundle.
- `public/tauri-shim.js`: new HTTP bridge (part of agy-accounts) that maps the
  subset of Tauri commands used by the UI onto the agy-accounts HTTP API.
- Rust backend (`src-tauri/`) is **not** included; desktop-only commands are
  implemented or rejected by the bridge.
- Built output lives in `../webapp/`.

No upstream source file other than the two mentioned above was modified.

## Build

```bash
./build.sh
```

Node.js 22.12+ is required (Vite 7). The build is not needed on the target
Debian host; commit `../webapp/` and deploy it directly.
