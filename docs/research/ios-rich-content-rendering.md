# iOS Rich Content Rendering

## Scope and current shape

This is an implementation-oriented recommendation for the current iOS client.

It is intentionally limited to the existing iOS message/rendering code and the response/event shapes used by the control service and adjacent WebSocket/SSE clients.

The current app targets iOS 17, uses Swift 6 strict concurrency, and has no declared third-party package dependency in `ios/project.yml`.

`ZiggyMessage.content` is already either a string or `JSONValue`, but `AppModel.loadMessages` currently converts only `content.text` into `ChatItem.text`.

`ZiggyMarkdownText` uses `AttributedString(markdown:options:.full)` and falls back to plain `Text`.

That gives basic emphasis, links, headings, and code-like inline styling, but it is not a complete block renderer.

`ZiggyMessageBubble` has three current presentation paths: user text, assistant Markdown text, and progress text.

The WebSocket model has `message`, `delta`, `stream_end`, `error`, and work events.

`AssistantDelta` is text-oriented and identifies a chat/session, stream/message, role, and optional sequence.

The WebSocket client republishes only delta, completion, and failure to `AssistantStream`.

The SSE parser is framing-only; an endpoint-specific decoder must interpret each JSON event.

The existing web model confirms the practical upstream shapes: text messages, media URLs, `tool_hint`/`progress` kinds, and text deltas.

The current UI groups consecutive progress/tool hints as a trace row and accumulates deltas by concatenation.

These facts favor an additive normalized content model and a stream reducer, not fence sniffing in SwiftUI.

## Recommendation

Adopt a versioned, typed content protocol at the transport boundary.

Render the protocol with SwiftUI-first native views.

Use one small `WKWebView` only for Mermaid diagrams, with a fixed local HTML shell and no arbitrary server HTML.

Keep the protocol semantic: a code block is a code block, a chart is chart data, and an authenticated image is an asset reference.

Do not treat Markdown fences, HTML tags, or URL suffixes as the content protocol.

Do not pass assistant-produced HTML into a web view.

Preserve the existing string fields during migration, with the server or client adapter mapping legacy text into a `markdown` block.

## Content protocol v1

The wire envelope should have a stable version, event identity, stream identity, and monotonic sequence.

```swift
enum RichContentVersion: String, Codable, Sendable { case v1 = "1" }

struct RichContentMessage: Codable, Hashable, Sendable {
    let version: RichContentVersion
    let id: String
    let chatID: String
    let role: MessageRole
    let blocks: [RichBlock]
    let createdAt: ZiggyTimestamp?
}

enum RichBlock: Codable, Hashable, Sendable {
    case markdown(String)
    case code(language: String?, code: String)
    case table(TableBlock)
    case taskList(TaskListBlock)
    case mermaid(MermaidBlock)
    case media(MediaBlock)
    case chart(ChartBlock)
    case file(FileBlock)
    case quote(String)
    case divider
    case unsupported(type: String)
}
```

The actual `Codable` representation should use a discriminator, for example `{"type":"markdown","text":"..."}`.

Each block should be bounded by server and client limits, including text bytes, rows, columns, points, and media dimensions.

Unknown block types must decode to an unavailable placeholder with a readable label, not fail the whole message.

The protocol should reserve `extensions` for additive metadata, while requiring renderers to ignore unknown keys.

Representative JSON:

```json
{
  "version": "1",
  "id": "msg-42",
  "chat_id": "chat-7",
  "role": "assistant",
  "blocks": [
    {"type": "markdown", "text": "## Result\nThe deploy passed."},
    {"type": "code", "language": "bash", "code": "curl -I https://example.test"},
    {"type": "chart", "chart_type": "line", "title": "Latency", "series": []}
  ]
}
```

Use `source` plus `mediaType` only for explicitly supported source forms.

```swift
enum MediaSource: Codable, Hashable, Sendable {
    case authenticatedAsset(id: String, expiresAt: ZiggyTimestamp?)
    case allowlistedURL(URL)
    case localFile(id: String)
}

struct MediaBlock: Codable, Hashable, Sendable {
    let source: MediaSource
    let mediaType: String
    let name: String?
    let width: Int?
    let height: Int?
    let alt: String?
}
```

The client should fetch authenticated assets through its authenticated networking layer and hand native views local data or a controlled file URL.

The server should prefer asset IDs or short-lived signed URLs over bearer tokens embedded in content.

## Event protocol and streaming

Use the same logical event schema over WebSocket JSON frames and SSE `data` payloads.

```swift
enum RichEvent: Codable, Hashable, Sendable {
    case hello(HelloEvent)
    case message(RichContentMessage)
    case contentStart(ContentStartEvent)
    case contentAppend(ContentAppendEvent)
    case contentReplace(ContentReplaceEvent)
    case thinking(ThinkingEvent)
    case tool(ToolEvent)
    case progress(ProgressEvent)
    case contentEnd(ContentEndEvent)
    case error(StreamErrorEvent)
}

struct ContentAppendEvent: Codable, Hashable, Sendable {
    let streamID: String
    let sequence: Int
    let blockID: String
    let text: String
}
```

`contentAppend` is valid only for appendable text/code blocks; it must never require reparsing an arbitrary Markdown fence to discover block boundaries.

For rich blocks, send `contentStart` with a block type and append only its typed payload, or send a complete `message` when the block is ready.

`contentReplace` supports corrections from a model or parser without duplicating the message.

`thinking` and `tool` events are separate timeline items with visibility policy, status, timestamps, and optional redacted detail.

```swift
struct ThinkingEvent: Codable, Hashable, Sendable {
    let streamID: String
    let sequence: Int
    let phase: String
    let summary: String?
}

struct ToolEvent: Codable, Hashable, Sendable {
    let streamID: String
    let sequence: Int
    let callID: String
    let name: String
    let status: String
    let summary: String?
}
```

The reducer should key state by `(chatID, streamID)` and order by sequence, buffering a small bounded gap window.

Duplicate sequence numbers must be idempotent; a conflicting duplicate is a stream error.

On reconnect, request replay from the last acknowledged sequence or fetch the completed message by ID.

Do not merge thinking/tool rows into assistant Markdown merely because they arrived between deltas.

The existing `message.kind`, `delta`, and `stream_end` shapes remain accepted as a legacy adapter.

## Markdown, CommonMark, and GFM

Define the supported baseline as CommonMark plus selected GitHub Flavored Markdown features.

Support headings, paragraphs, emphasis, strong, links, block quotes, ordered/unordered lists, thematic breaks, inline code, fenced code, tables, task list items, and soft/hard line breaks.

Render links as native `Link` or a controlled open action; never allow `javascript:` or custom schemes by default.

Treat raw HTML as text or remove it during sanitization; do not expose an HTML renderer for assistant content.

Disable remote Markdown image loading in the selected renderer. Images must be
separate typed media blocks so authentication, host policy, byte limits, alt
text, caching, and logout purge cannot be bypassed with Markdown syntax.

The legacy adapter may remove known transport-only tags such as a backend
thinking wrapper, but it must not become a general HTML interpreter. The typed
protocol replaces those wrappers with `thinking` events.

Task lists should be read-only initially unless the protocol includes stable task IDs and an explicit update command.

Tables need horizontal scrolling, a header row, accessible row/column labels, and a bounded cell count.

Code needs horizontal scrolling, monospaced text, selectable/copyable content, language labeling, and a copy action.

For streaming Markdown, render complete blocks when possible and show a plain-text fallback for an incomplete block.

Avoid reparsing the entire growing answer on every token; coalesce updates on the main actor at a modest cadence.

Library comparison for Markdown:

| Option | Fit | Cost / risk |
|---|---|---|
| Apple `AttributedString` | Already present; good native inline baseline | Block GFM, tables, task lists, and fenced-code control are insufficient; verify exact iOS 17 behavior |
| [gonzalezreal/Textual](https://github.com/gonzalezreal/textual) | Active SwiftUI-native successor to MarkdownUI; structured text, tables, code blocks, selection, styling, and custom parsers | Young `0.x` API; fixture, streaming, accessibility, and long-message performance tests are required before adoption |
| [swiftlang/swift-markdown](https://github.com/swiftlang/swift-markdown) | Maintained parser/AST with full control over validation and native block rendering | Ziggy must own more rendering and styling code |
| [MarkdownUI](https://github.com/gonzalezreal/swift-markdown-ui) | Established GFM SwiftUI renderer | In maintenance mode; do not add it to a new implementation |

Recommended path: keep `AttributedString` for the compatibility path and run a
small Textual spike against Ziggy's fixture and streaming tests. Use Textual if
its `0.x` API and long-message performance are acceptable. Otherwise parse
with `swift-markdown` and render Ziggy's typed native blocks directly.

Textual is currently MIT-licensed and its latest release at the time of this
research is `0.5.0` from June 2026. Pin the selected version. Exact GFM
coverage, package size, and performance must still be verified in the app.

## Code blocks

Code is a native SwiftUI block, not HTML.

Use a dark/light adaptive surface from `ZiggyPalette`, but do not assume a fixed dark code theme.

Display the declared language as a small label and preserve the raw code for copy/export.

Syntax highlighting is optional phase two; avoid running an untrusted language grammar in a web view.

Use native `AttributedString` styling initially and
[swift-markdown](https://github.com/swiftlang/swift-markdown) when the renderer
needs AST-level code-block control. Syntax highlighting is a separate feature
and should not block safe code rendering.

## Mermaid

Mermaid is the one justified WKWebView boundary because the ecosystem is JavaScript-first and diagram layout is not a small SwiftUI primitive.

Use a local bundled HTML shell containing a pinned Mermaid build and a tiny message bridge. The native side sends only a validated Mermaid block plus theme, font scale, and a render request.

The HTML shell must not load remote scripts, navigate, execute arbitrary links,
or accept arbitrary HTML. Initialize Mermaid in strict security mode, disable
diagram link callbacks and HTML labels, and cap source length and node count
before invoking JavaScript.

Disable navigation and external resource loading except the local bundle; return a static error tile on parse failure. Prefer server-provided diagram source in the typed block, with maximum length and diagram complexity limits.

Use the official [mermaid-js/mermaid](https://github.com/mermaid-js/mermaid)
distribution in the local shell. No sufficiently established native Swift
Mermaid renderer was identified for this recommendation. Pin the Mermaid
version and review its security advisories, CSP behavior, SVG output, and
accessibility before each upgrade.

Do not permit Mermaid to fetch arbitrary URLs or embed arbitrary SVG returned by the model.

Consider rendering Mermaid to a native image snapshot after success for scrolling, offline replay, and memory stability, while retaining source for re-rendering.

Server-side rendering is not the initial recommendation. It would remove
Mermaid JavaScript from iOS, but it adds a headless rendering service, server
resource use, media authorization, and another cached artifact. A bundled,
network-disabled web view keeps the source and renderer local and can be
snapshotted immediately. Reconsider server-side raster/PDF rendering on
Beelink if web-view memory or accessibility remains unacceptable; do not run a
headless browser on Spark.

## Images, video, and PDFs

### Authenticated images

Use `AsyncImage` only for public or already-authorized URLs; authenticated content should use an actor-backed asset loader.

The loader sends the bearer credential through `URLRequest`, validates status and MIME type, enforces byte limits, and stores an opaque cache key.

Decode with ImageIO downsampling to the display size to avoid decompression spikes.

Native options are `URLSession` plus `Image`, [kean/Nuke](https://github.com/kean/Nuke), or [SDWebImage/SDWebImageSwiftUI](https://github.com/SDWebImage/SDWebImageSwiftUI).

Nuke is the preferred candidate if cache, progressive decode, and request coalescing justify a dependency; verify Swift 6 and iOS 17 support first.

Do not log signed URLs, authorization headers, or image bytes.

Every image requires alt text, a loading state, a failure placeholder, and a full-screen accessible viewer action.

### Video files and URLs

Represent video as a media block with MIME type, duration metadata if known, and an authenticated asset source.

Use `AVPlayer`/`VideoPlayer` for playback and download protected files to an app-private temporary location.

Do not put bearer tokens in `AVPlayer` URL query strings unless the server explicitly provides short-lived scoped URLs.

Candidates are native AVKit, [Limekiller/Player](https://github.com/Limekiller/Player), or [piemonte/Player](https://github.com/piemonte/Player).

Native AVKit is recommended; the other repositories and their current iOS 17/Swift 6 status must be verified before consideration.

Provide poster/loading/error states, captions when available, reduced-motion behavior, and no autoplay with sound.

### PDFs and general files

Use PDFKit for PDFs and Quick Look for unsupported downloadable files.

Download authenticated files with size/type checks into an app-private cache, then present a native `PDFView` wrapper or `QLPreviewController` wrapper.

Candidates are PDFKit, Quick Look, and [PSPDFKit/PSPDFKit-iOS](https://github.com/PSPDFKit/PSPDFKit-iOS) only if a commercial dependency is acceptable.

PDFKit/Quick Look are the default; commercial licensing, package access, and feature need must be verified before PSPDFKit.

Never render a server-provided filename as a path; use generated local names and content-derived type checks.

## Charts and graphs

Charts should be semantic data, not SVG or HTML.

```swift
struct ChartBlock: Codable, Hashable, Sendable {
    let chartType: String
    let title: String?
    let xLabel: String?
    let yLabel: String?
    let series: [ChartSeries]
}

struct ChartSeries: Codable, Hashable, Sendable {
    let name: String
    let points: [ChartPoint]
}

struct ChartPoint: Codable, Hashable, Sendable {
    let x: String
    let y: Double
}
```

Use Swift Charts for line, bar, area, and point charts with bounded data.

Candidates are native Swift Charts, [willdale/SwiftUICharts](https://github.com/willdale/SwiftUICharts), or [danielgindi/Charts](https://github.com/danielgindi/Charts).

Swift Charts is the recommendation for iOS 17; the alternatives must be checked for maintenance, accessibility, and Swift 6 compatibility before use.

Charts require textual summaries or a data table fallback for VoiceOver, Dynamic Type, and export.

Reject NaN, infinity, unbounded point counts, misleading unsupported axes, and colors that are the sole encoding of a series.

## Sanitization, URLs, caching, and security

Validate the decoded protocol before rendering; rendering code is not the validation boundary.

Allow only `https` and, where explicitly needed, `http` on a configured private-development host.

Allowlist exact media hosts or server asset IDs; do not allow arbitrary redirects to untrusted hosts.

Revalidate the final URL after redirects, enforce content-type and maximum size, and reject MIME/extension mismatches.

Use `URLSession` with an ephemeral or explicitly configured session, certificate policy as required by deployment, and no credentialed cross-origin web content. Store caches in `Library/Caches`, keyed by a content hash plus authenticated user scope, with expiration and logout purge.

Do not cache authorization headers, raw tokens, private query strings, or sensitive thinking/tool payloads.

Persist completed typed content only if the existing message history contract guarantees user scoping and redaction.

Treat tool output and model text as untrusted data, including Markdown link labels, code, chart labels, Mermaid source, filenames, and PDF metadata. Limit nesting, recursion, table dimensions, image pixels, video duration, PDF bytes, chart points, and Mermaid complexity.

Redact or omit chain-of-thought; expose only an intentional thinking summary/status event.

For `WKWebView`, set a strict CSP, disable JavaScript except the local Mermaid shell, block navigation, and bridge only JSON with an explicit schema.

## Dark mode and accessibility

All native blocks must use the existing adaptive `ZiggyPalette` rather than hard-coded colors. Mermaid receives the current color scheme and must use a high-contrast local theme.

Dynamic Type must reflow tables, code, captions, and trace rows without clipping. Images need alt text; videos need captions or a transcript; charts need a summary and data table; diagrams need source text or a generated description.

Use accessibility headings for Markdown headings, meaningful labels for controls, and avoid announcing every streaming token.

Respect Reduce Motion by disabling cursor/diagram animation and avoiding autoplay. Selectable text and copy actions should work for Markdown and code without copying hidden metadata.

## Offline and failure behavior

Completed native content should replay from the existing REST history if it contains the typed protocol.

If only legacy text is available, render it as Markdown with the current fallback behavior.

If an asset is offline and cached, show it; otherwise show a labeled retry placeholder without blocking the message. If a block is unknown or invalid, preserve surrounding blocks and show “Content unavailable” with a diagnostic-free retry action.

If a stream disconnects, keep accumulated content marked incomplete, keep tool/thinking status visible, and offer reconnect/retry.

If sequence recovery fails, fetch the completed message rather than concatenating possibly duplicated deltas.

## Rollout

Phase 0: add fixture decoding and reducer tests for legacy text, current WebSocket frames, current SSE frames, and v1 envelopes. Phase 1: ship typed Markdown/code blocks, native table/task-list rendering, and legacy fallback behind a server capability flag.

Phase 2: add authenticated media assets, native PDF/video presentation, cache policy, and offline replay. Phase 3: add Swift Charts and then the isolated Mermaid shell after security and snapshot tests pass.

Phase 4: enable typed thinking/tool/progress timeline events and sequence-based reconnect recovery. Keep a kill switch that forces legacy text rendering and disables remote media/Mermaid independently.

Do not change the current implementation files as part of this research task.

## Acceptance tests

1. Decode v1 JSON with every block type, preserve unknown blocks as placeholders, and decode current `ZiggyMessage`, WebSocket, and SSE payloads without regression.

2. Reduce out-of-order, duplicate, and reconnect replay events deterministically.

3. Render CommonMark/GFM fixtures for headings, lists, quotes, links, inline code, fences, tables, task lists, scrolling, and VoiceOver labels.

4. Stream Markdown and code chunks without fence sniffing or duplicated text; verify incomplete stream, tool failure, thinking summary, retry, and final-message recovery states.

5. Reject raw HTML, `javascript:` links, non-allowlisted hosts, invalid MIME types, oversized payloads, malformed chart values, and unsafe redirects.

6. Exercise authenticated image/video/PDF loading, cache hit/expiry, offline hit/miss, HTTP error, cancellation, token non-disclosure, and logout purge.

7. Render Mermaid success/failure/timeout, blocked navigation, dark mode, Reduce Motion, chart summaries, data-table fallback, Dynamic Type, and iPhone portrait states.

8. Measure memory/frame behavior with long streams and maximum media/table/chart inputs; before implementation verify each library’s version, license, iOS 17 support, Swift 6 warnings, package size, and accessibility.
