# nanobot webui

The browser front-end for the nanobot gateway. It is built with Vite + React 18 +
TypeScript + Tailwind 3 + shadcn/ui, talks to the gateway over the WebSocket
multiplex protocol, and reads session metadata from the embedded REST surface
on the same port.

For the project overview, install guide, and general docs map, see the root
[`README.md`](../README.md).

## Current status

> [!NOTE]
> The standalone WebUI development workflow currently requires a source
> checkout.
>
> WebUI changes in the GitHub repository may land before they are included in
> the next packaged release, so source installs and published package versions
> are not yet guaranteed to move in lockstep.

## Layout

```text
webui/                 source tree (this directory)
nanobot/web/dist/      build output served by the gateway
```

## Runtime boundary

This WebUI is part of the nanobot runtime, not a separate product or a
LibreChat/Open WebUI frontend. The boundary is:

```text
nanobot backend/runtime
  ├─ agent loop, memory, tools, MCP, sessions
  ├─ channels/websocket.py
  │   ├─ WebSocket chat protocol
  │   └─ embedded REST API: /auth/bootstrap, /api/sessions,
  │      /api/settings, /api/media, /api/activity
  └─ embedded React WebUI
      └─ built from webui/ into nanobot/web/dist/
```

That tight coupling is intentional for dogfooding: frontend changes can use
session state and WebSocket activity directly from the local gateway. It is not
yet the long-term product boundary. A future hosted/control-plane product would
likely split these into a local agent runtime, a standalone web/PWA frontend, a
connector/relay, and an org/auth/billing control plane.

## Develop from source

### 1. Install nanobot from source

From the repository root:

```bash
pip install -e .
```

### 2. Enable the WebSocket channel

In `~/.nanobot/config.json`:

```json
{ "channels": { "websocket": { "enabled": true } } }
```

### 3. Start the gateway

In one terminal:

```bash
nanobot gateway
```

### 4. Start the WebUI dev server

In another terminal:

```bash
cd webui
bun install            # npm install also works
bun run dev
```

Then open `http://127.0.0.1:5173`.

By default, the dev server proxies `/api`, `/webui`, `/auth`, and WebSocket
traffic to `http://127.0.0.1:8765`.

If your gateway listens on a non-default port, point the dev server at it:

```bash
NANOBOT_API_URL=http://127.0.0.1:9000 bun run dev
```

For Ziggy's current edge-builder setup, the live gateway is usually reached via
the SSH-forwarded edge-builder port:

```bash
NANOBOT_API_URL=http://127.0.0.1:18793 npm run dev -- --host 0.0.0.0
```

Plain `npm run preview` is only a production static-asset preview unless it is
also pointed at a live gateway. If no gateway is listening at the configured
`NANOBOT_API_URL` (default `127.0.0.1:8765`), the browser will fail at
`/auth/bootstrap` with `bootstrap failed: HTTP 500` or a proxy
`ECONNREFUSED` error.

When the frontend is newer than the deployed gateway, routes added in
`channels/websocket.py` may not exist yet. For example, a local frontend with
the Activity view requires the backend `/api/activity` route to be deployed to
the running gateway; otherwise the request may fall through to the older static
SPA response.

## Build for packaged runtime

```bash
cd webui
bun run build
```

This writes the production assets to `../nanobot/web/dist`, which is the
directory served by `nanobot gateway` and bundled into the Python wheel.

If you are cutting a release, run the build before packaging so the published
wheel contains the current WebUI assets.

## Test

```bash
cd webui
bun run test
```

## Acknowledgements

- [`agent-chat-ui`](https://github.com/langchain-ai/agent-chat-ui) for UI and
  interaction inspiration across the chat surface.
