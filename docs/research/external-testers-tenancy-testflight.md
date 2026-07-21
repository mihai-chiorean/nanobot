# External Testers: Tenancy and TestFlight

Status: release research and design recommendation

Date: 2026-07-21

Scope: 3-10 invited external testers using the iOS app and the existing Go
front door. This document describes the next work; it does not change runtime,
auth, deployment, or iOS implementation.

## Executive Decision

The current build is a single-user personal deployment. It is not safe to
invite a second tester by adding another email or by sharing the current guest
code. The current control plane would authorize that person into the owner's
Nanobot workspace, and Nanobot's own WebSocket contract treats a known
`chat_id` as sufficient to attach to a conversation.

The smallest credible external release is therefore:

- Clerk-authenticated, email-allowlisted accounts with one personal workspace
  per Clerk subject.
- One isolated, unprivileged Nanobot process and filesystem root per active
  workspace. For 3-10 testers, long-lived processes are acceptable; eviction
  is not required for the first release.
- Chat and read-only conversation history only. Disable integrations, shell
  execution, scheduled Work, and shared files until they have tenant-scoped
  authorization.
- A shared model endpoint behind admission control, with one active turn per
  tester and a small global cap.
- A tested admin disable and deletion runbook before the first invitation.
- Direct email invitations to a private TestFlight external group. Do not use a
  public TestFlight link for this cohort.

This scope is intentionally narrower than the target architecture in
[`ziggy-multitenant-runtime.md`](../architecture/ziggy-multitenant-runtime.md).
It proves the boundary first and leaves durable memory, connectors, and
background automation as follow-on work.

## Current State From Code

### Control plane and identity

`services/ziggy-control` is a compatibility front door, not a tenancy service.
Its README says that phase one has no PostgreSQL dependency, workspace
scheduler, or second-user support, and explicitly warns that admitting another
email would put that user in the owner's Nanobot workspace
([README](../../services/ziggy-control/README.md)).

The current implementation:

- Verifies a Clerk session JWT with Clerk's Go SDK, derives a principal from the
  signed `sub` and email claim, and can fall back to the Clerk Users API
  ([Clerk middleware](../../services/ziggy-control/internal/auth/clerk.go)).
- Authorizes `/auth/bootstrap` only when the principal email matches
  `ZIGGY_OWNER_EMAIL` and, when configured, the subject matches
  `ZIGGY_OWNER_SUBJECT` ([handler](../../services/ziggy-control/internal/httpapi/handler.go)).
- Forwards the original Clerk bearer token to Nanobot, which mints the opaque
  short-lived REST/WebSocket token used by the existing clients. The Clerk
  check is duplicated; later REST, SSE, and WebSocket requests use the
  Nanobot token ([control README](../../services/ziggy-control/README.md),
  [production review](../reviews/ziggy-control-production-review-2026-07-21.md)).
- Has no durable user, invitation, workspace, membership, quota, deletion, or
  audit database. Configuration is immutable after startup and the only
  allowlist is one owner email plus an optional subject.
- Proxies the current Nanobot REST, SSE, and WebSocket surfaces. It strips
  product-specific spoofable headers, but it does not resolve a workspace or
  rewrite a `chat_id` to a tenant namespace.

### iOS client and auth

The iOS target is `com.mihaichiorean.ziggy`, uses development team
`98KW2QQ963`, automatic signing, generated Info.plist keys, and iOS 17 as its
deployment target ([XcodeGen project](../../ios/project.yml),
[base config](../../ios/Config/Base.xcconfig)). No entitlements file or
Associated Domains capability is present in the repository.

The current app:

- Presents a fixed owner identity in the UI.
- Accepts a server URL and private guest code, or reads them from
  `ZIGGY_SERVER_URL` and `ZIGGY_GUEST_CODE` for tests.
- Stores the server URL and guest enrollment code in Keychain. Short-lived
  `nbwt_` tokens remain in memory and are refreshed by repeating guest
  bootstrap ([AppModel](../../ios/Ziggy/App/AppModel.swift),
  [credential store](../../ios/Ziggy/Services/CredentialStore.swift),
  [authentication notes](../../ios/Docs/AUTHENTICATION.md)).
- Sends the WebSocket token in the query string. The client has no Clerk SDK,
  browser session exchange, OAuth callback handling, redirect URI, universal
  link, or per-user account model.

The app documentation calls Google sign-in and server-side identity
verification a future step and explicitly says that the current build has no
per-user data partitioning ([features](../../ios/Docs/FEATURES.md)).

### Nanobot state and isolation-relevant behavior

Nanobot already supports multiple instances when each instance gets a separate
config directory, workspace, port, and runtime subdirectories
([multiple instances](../multiple-instances.md),
[path helpers](../../nanobot/config/paths.py)). The relevant current paths
are:

| Data | Current location/behavior | External-release implication |
|---|---|---|
| Config and provider settings | Default `~/.nanobot/config.json`; an instance's runtime directory is the config file's parent. | Never copy a shared config into every tester runtime. Provider keys and MCP settings must not be exposed to an untrusted per-user process. |
| Conversations | `<workspace>/sessions/<safe-key>.jsonl`; a legacy global `~/.nanobot/sessions` path is used as a migration fallback. | A workspace directory is an isolation boundary only when the gateway chooses it server-side and the process cannot reach other paths. |
| Documents and files | The entire configured workspace is available to Nanobot tools. The repository does not define a separate tenant document store. | Give each user a dedicated root and enable `restrictToWorkspace`; do not mount a shared host directory. |
| Context | The live agent context is assembled in the process from session messages, workspace files, skills, and model/tool results. | Process memory, prompt assembly, logs, and crash artifacts must be treated as tenant data. |
| Memory | `SOUL.md`, `USER.md`, `memory/MEMORY.md`, `memory/history.jsonl`, cursors, and a local GitStore live under the workspace ([memory docs](../memory.md)). | One workspace per user is sufficient for the first local-memory proof, but deletion must remove the whole tree and any backups. |
| Media | Instance runtime path `media/`, optionally per channel; WebSocket payloads can contain local filesystem paths ([paths](../../nanobot/config/paths.py), [WebSocket docs](../websocket.md)). | Media URLs and file-serving routes need the same tenant authorization as conversations. Disable attachments unless this is tested. |
| Cron and channel runtime state | Instance runtime subdirectories `cron/`, `logs/`, and channel-specific state under the config directory. | Disable cron/background channels for testers or create them only inside the user's runtime root. |
| Integrations and secrets | Nanobot supports configured MCP servers and provider credentials in its config model. Ziggy has no implemented per-user connector service or encrypted credential store. | Do not enable integrations in the first external cohort. Shared API keys must remain outside Nanobot and outside prompts. |

The architecture proposal already identifies one live Nanobot process plus
workspace as the intended isolation unit, with Go responsible for identity,
routing, lifecycle, quotas, and durable product state
([architecture](../architecture/ziggy-multitenant-runtime.md)). That is a target,
not current behavior.

Nanobot's own WebSocket documentation is a strict warning for this work:
`chat_id` is currently a capability, so anyone holding a valid WebSocket
credential and a known ID can attach to that conversation. The same document
says multi-tenant deployments must namespace chat IDs or add a tenant auth gate
([WebSocket security boundary](../websocket.md)). A separate process alone is
not enough if the public gateway can route a tester to the wrong process.

## Blockers and Acceptable Shortcuts

### Strict blockers before inviting external testers

1. **Identity and admission:** each tester must authenticate with a stable Clerk
   subject; the server must require that subject to be admitted and active.
   Email is an invite/allowlist attribute, not the durable foreign key.
2. **Server-derived workspace:** every request, session key, WebSocket attach,
   media access, and tool capability must resolve to the workspace associated
   with the verified subject. No client-supplied workspace or chat ID may grant
   access.
3. **Runtime and filesystem isolation:** each user runtime must run as an
   unprivileged process/container with a dedicated config/runtime directory and
   workspace root. It must not have another user's mount, Docker socket, host
   secrets, or public listener. Enable `restrictToWorkspace` and Linux
   `bwrap`/equivalent where shell access exists; the safer release choice is to
   disable shell/exec entirely.
4. **Conversation and stream isolation:** REST list/get/delete, WebSocket
   attach, reconnect, new-chat, stream events, and error responses must be
   checked against the authenticated workspace. A known chat ID owned by A
   must fail for B.
5. **Bounded shared-model admission:** a user cannot consume all inference
   capacity or cause unbounded queue growth. Enforce per-user and global
   concurrency, request size, timeout, cancellation, and queue limits before
   external use.
6. **Offboarding:** an operator must be able to disable a tester immediately,
   revoke sessions/capabilities, stop the runtime, and delete or quarantine all
   user data without affecting another user. TestFlight removal alone is not
   an application revocation mechanism.
7. **TestFlight release readiness:** the Apple app record, matching bundle ID,
   distribution-signed processed build, privacy disclosures, export-compliance
   determination, test information, feedback contact, and external beta review
   path must be complete. See [Apple's TestFlight overview](https://developer.apple.com/help/app-store-connect/test-a-beta-version/testflight-overview/).

### Shortcuts acceptable for 3-10 testers

- Keep one Nanobot process per tester alive on the Beelink. On-demand startup,
  idle eviction, HA, and automatic cross-host placement can follow.
- Use a small PostgreSQL schema or a single transactional store for users,
  workspaces, allowlist state, runtime records, and usage counters. Redis and a
  general workflow engine are not prerequisites.
- Keep local Nanobot sessions and memory inside each user's workspace for the
  first release. A centralized memory MCP service and vector backend can
  follow, provided deletion and namespace tests cover the local implementation.
- Use an operator-managed email allowlist and manually send matched Clerk and
  TestFlight invitations. A self-service invitation UI is not required.
- Use a fixed global model cap and one active interactive turn per user rather
  than a sophisticated scheduler. Disable background Work and integrations.
- Support one device/session per tester initially. Reauthentication and
  explicit session management can follow, but revocation must work.

Not acceptable shortcuts are a shared guest code, a second email pointed at the
owner workspace, a public TestFlight link, client-provided workspace routing,
shared provider credentials, or a manual promise not to inspect another user's
conversation.

## Milestones in Dependency Order

### M1. Durable identity, allowlist, and workspace provisioning

**Depends on:** current Clerk verification and the existing bootstrap contract.

**Build:**

- Add durable records for `user` (`clerk_subject` unique), invitation/allowlist
  state, personal `workspace`, session/revocation state, and an audit event.
- Admit only pre-registered email plus the expected Clerk subject after the
  first controlled enrollment. Store normalized email for operations, but use
  `clerk_subject` for authorization and identity continuity.
- Replace owner-only bootstrap with a server-side resolver that creates exactly
  one personal workspace idempotently. Return a Ziggy session/capability bound
  to subject, workspace, runtime, expiry, and nonce. Keep the current Nanobot
  token exchange private behind the gateway.
- Add explicit states: invited, active, disabled, deletion-pending, deleted.
  Reject disabled/deleted users before runtime lookup.
- Define the iOS flow: Clerk sign-in through a native SDK or
  `ASWebAuthenticationSession` with PKCE, server verification of issuer,
  audience, expiry, nonce, and subject, then exchange for a Ziggy session. Do
  not send a Clerk token as the long-lived WebSocket credential.

**Acceptance criteria:**

- Two test identities can sign in and obtain different internal user and
  workspace IDs; repeated bootstrap is idempotent.
- An uninvited valid Clerk subject receives a deny response and no workspace or
  runtime is created.
- An email change does not transfer a workspace; a subject change requires an
  explicit operator migration.
- Disabling a user rejects new bootstrap and existing Ziggy credentials fail
  revocation checks within the documented propagation bound.
- No API accepts a workspace ID as authorization evidence.

### M2. Per-user runtime and data isolation proof

**Depends on:** M1's resolver and capability format.

**Build:**

- Add a runtime supervisor that creates one directory tree per workspace, writes
  a generated config with only that workspace's paths, starts the pinned
  Nanobot as an unprivileged process, and assigns a private loopback/Unix
  endpoint and runtime credential.
- Route the public request to the runtime selected from the verified workspace,
  never from a request field. Bind the runtime credential to user, workspace,
  runtime ID, allowed routes, and expiry.
- Namespace public session/chat IDs with an opaque workspace binding or maintain
  a server-side mapping. Validate ownership on every attach, list, read, delete,
  and stream event.
- Mount only `<tenant-root>/config`, `<tenant-root>/workspace`, and the minimum
  media/runtime paths. Do not expose the host's config, legacy sessions path,
  logs, secrets, Docker socket, or another tenant root.
- For the first release, set `restrictToWorkspace=true`, disable `exec`, MCP,
  cron, channel adapters, and outbound integration tools. Keep uploads disabled
  until media path authorization is proven.

**Acceptance criteria:**

- A and B can each create, resume, list, and delete their own chat while all
  cross-user REST and WebSocket attempts return the same generic not-found or
  forbidden result without data.
- A's marker in `sessions`, workspace files, memory files, process environment,
  runtime logs, and model request metadata never appears in B's responses,
  prompts, or files. The reverse test also passes.
- Killing A's process and starting it again restores A only; assigning the same
  host slot to B cannot reuse A's workspace, environment, token, or cache.
- The gateway cannot reach Nanobot except through private runtime endpoints, and
  Nanobot cannot reach another tenant root or host secret.
- The adversarial matrix below passes repeatedly with concurrent A/B turns and
  forced reconnects.

### M3. Resource governance, deletion, and migration

**Depends on:** M2's runtime identity and data inventory.

**Build:**

- Put a model gateway or equivalent admission layer in front of the shared
  model server. Initial policy: one active interactive turn per user, two to
  four global interactive turns based on measured GPU headroom, bounded queue,
  request/token caps, 60-120 second timeout, cancellation, and no background
  Work. Return a retryable busy response rather than silently queueing forever.
- Record usage counters by user/workspace without raw prompt content. Set a
  daily token/turn budget and request/body/media size caps. The exact numbers
  should be load-tested on Spark before inviting testers.
- Define deletion as a coordinated transaction/runbook: disable user, revoke
  sessions, stop and drain runtime, delete workspace/config/media/cron/log
  material, delete local memory Git history and derived indexes, revoke and
  delete integration credentials if any, then mark the user deleted. Preserve
  only the minimum security/audit record with a documented retention period.
- Freeze the existing personal workspace, checksum a backup, and migrate only
  the owner's data into the owner's new workspace. Start external testers with
  empty workspaces. Do not bulk-import personal sessions or memory into tester
  workspaces.
- Version the tenant data layout. Make migrations additive and restart-safe;
  never roll back a process binary across an incompatible data migration
  without a tested restore path.

**Acceptance criteria:**

- Saturation tests show B cannot starve A indefinitely, queue depth has a hard
  maximum, cancellation frees capacity, and a crashed runtime does not leave a
  lease that routes data to the next user.
- Deleting A removes every inventoried A path and credential and leaves B's
  chat, runtime, memory, and quota unchanged. A's old tokens fail.
- The owner migration passes checksum and conversation spot checks; no tester
  workspace contains owner markers; rollback is a documented artifact/data
  operation.
- Operators can see per-user runtime health, usage, active turns, last access,
  and disable/delete state without seeing prompt content by default.

### M4. iOS and TestFlight release gate

**Depends on:** M1-M3 security acceptance, then the Apple distribution setup.

**Build/release work:**

- Create an App Store Connect app record using the exact bundle ID
  `com.mihaichiorean.ziggy`. Apple documents that the bundle ID in App Store
  Connect must match the Xcode target and that the first uploaded build creates
  the beta version record ([App information](https://developer.apple.com/help/app-store-connect/reference/app-information/app-information),
  [upload builds](https://developer.apple.com/help/app-store-connect/manage-builds/upload-builds)).
- Confirm the Apple Developer team, explicit App ID, distribution certificate,
  provisioning profile, automatic signing, version/build numbers, and required
  capabilities. Apple describes App IDs as the capability allowlist in a
  provisioning profile and requires the App ID to match the Xcode bundle ID
  ([Register an App ID](https://developer.apple.com/help/account/identifiers/register-an-app-id),
  [certificates](https://developer.apple.com/help/account/create-certificates/certificates-overview)).
- Keep capabilities minimal for this release: existing microphone, speech, and
  photo permission strings must match actual behavior; add Associated Domains
  only if the Clerk callback uses an HTTPS universal link. Archive and upload a
  distribution-signed build, wait for processing, and verify the build is
  eligible for TestFlight. Apple specifically requires application identifiers
  in the provisioning profile for TestFlight eligibility
  ([TestFlight overview](https://developer.apple.com/help/app-store-connect/test-a-beta-version/testflight-overview/)).
- Complete the privacy policy URL and App Store privacy questionnaire for the
  real data flow: account/contact data, conversation and document content,
  diagnostics, identifiers, provider/Clerk/observability processing, retention,
  and whether data is linked to the user or used for tracking. Do not guess the
  labels; inventory SDKs and server processors first. Apple requires a privacy
  policy URL for iOS and accurate disclosure of the app and third-party data
  practices ([Manage app privacy](https://developer.apple.com/help/app-store-connect/manage-app-information/manage-app-privacy/),
  [App privacy details](https://developer.apple.com/app-store/app-privacy-details/)).
- Answer export-compliance questions for TLS/OS cryptography and any other
  encryption actually shipped. If Apple requests documentation, upload it and
  resolve the build's compliance status before testing; Apple says a build can
  be marked Missing Compliance until this is handled
  ([export compliance overview](https://developer.apple.com/help/app-store-connect/manage-app-information/overview-of-export-compliance),
  [beta export compliance](https://developer.apple.com/help/app-store-connect/test-a-beta-version/provide-export-compliance-information-for-beta-builds)).
- Use an external group with 3-10 named email testers, a clear What to Test
  note, feedback email/contact, test account instructions if needed, and no
  public link. Apple allows up to 10,000 external testers and up to 100 internal
  App Store Connect testers; the first external build for a group requires
  TestFlight App Review and accompanying metadata
  ([external testers](https://developer.apple.com/help/app-store-connect/test-a-beta-version/invite-external-testers),
  [internal testers](https://developer.apple.com/help/app-store-connect/test-a-beta-version/add-internal-testers)).
- Use internal testing only for the owner/team's App Store Connect accounts and
  fast build smoke checks. Use external testing for the 3-10 invited testers:
  external testers are not App Store Connect users, the first build submitted
  for external testing receives TestFlight App Review, and a TestFlight build
  is available for testing for up to 90 days ([TestFlight overview](https://developer.apple.com/help/app-store-connect/test-a-beta-version/testflight-overview/),
  [invite external testers](https://developer.apple.com/help/app-store-connect/test-a-beta-version/invite-external-testers)).
- Monitor TestFlight feedback and crash reports, map each report to app version,
  build, device, and server correlation ID, and remove sensitive prompt content
  from diagnostics. Apple makes tester screenshots, comments, crash feedback,
  and device information available in App Store Connect
  ([tester feedback](https://developer.apple.com/help/app-store-connect/test-a-beta-version/view-tester-feedback),
  [build metrics](https://developer.apple.com/help/app-store-connect/test-a-beta-version/view-build-status-and-metrics)).

**Acceptance criteria:**

- A TestFlight-installed build can sign in as A and B, survive token refresh and
  WebSocket reconnect, and cannot access the other account's data.
- App Store Connect shows the processed build as eligible, with privacy and
  export compliance resolved; the external group is reviewable with complete
  test information.
- Every invited tester receives a unique Ziggy account/workspace and the
  operator can disable that tester without removing other testers.
- The release runbook includes build rollback, service rollback, user disable,
  data deletion, model overload response, and TestFlight feedback triage.

## Identity, Invites, and Redirects

There are two separate invitation systems:

1. **Ziggy admission:** operator creates the tester's Clerk subject/email
   allowlist record and keeps it disabled until the enrollment is expected.
2. **Apple distribution:** operator adds the same email to the private external
   TestFlight group after the build is ready. App Store Connect's invitation
   proves distribution eligibility, not Ziggy authorization.

The app should obtain a Clerk session through a native flow, then send that
session only to the gateway's bootstrap/exchange endpoint. The gateway verifies
the session and returns a short-lived Ziggy session. Store only the refreshable
credential material supported by the chosen Clerk flow in Keychain; never ship
an allowlist, guest code, provider key, or permanent bearer token in the app.

For callbacks, decide explicitly between:

- A registered custom URL scheme, which is the smallest infrastructure change
  for a 3-10 tester pilot but must be registered with Clerk and handled only by
  this app; or
- An HTTPS universal link on a Ziggy-owned domain. Apple requires both the
  `Associated Domains` entitlement and a matching
  `/.well-known/apple-app-site-association` file served over HTTPS without
  redirects ([Supporting associated domains](https://developer.apple.com/documentation/xcode/supporting-associated-domains)).

The current target has neither callback path. A universal link is not required
merely because the app is distributed through TestFlight; it is required only
if that is the selected callback/link design. Whichever path is selected must
be tested from a cold app launch, an already-running app, a cancelled sign-in,
an expired session, and an account switch. Do not use a callback that can place
an authorization code or Clerk token in a web server access log.

## Data, Secrets, and Integrations

### First release data policy

Each workspace owns its sessions, uploaded files, workspace documents, prompt
context, local memory, runtime media, and any generated artifacts. PostgreSQL
or the chosen durable store owns user/workspace mapping, runtime leases,
allowlist state, quotas, and audit metadata. The model gateway sees only the
request needed for inference and records bounded usage metadata, not raw private
content by default.

Do not treat a directory name as authorization. The gateway must derive the
workspace, the supervisor must verify the runtime capability, and every storage
adapter must receive an explicit workspace key. Backups, crash dumps, temporary
files, metrics labels, and support exports are included in the data inventory.

### Integrations and secrets

The repository's target architecture places OAuth credentials in a separate
encrypted store and exposes capability-scoped connector MCP services, but those
services are not implemented in the current code. Until they exist:

- Disable Gmail, Calendar, Drive, arbitrary MCP, cron, channel adapters, and
  outbound writes for external testers.
- Keep the shared model provider key and Clerk secret on private server-side
  services using the existing systemd credential pattern; never mount them into
  a tester runtime or send them through Nanobot context.
- When integrations return, bind every connector call to a short-lived
  workspace/runtime capability, provider account, action allowlist, rate limit,
  and audit event. Decrypt a credential only for the call and redact results.

## Quotas and Model Contention

The current control plane has request-body and bounded HTTP transport settings,
but no per-user quota, turn admission, model queue, or GPU scheduler. The
shared Spark model must therefore be a release dependency, not an operational
assumption.

Recommended pilot defaults, to be calibrated by load test:

| Limit | Initial policy |
|---|---|
| Interactive turns | 1 active per user; 2-4 global, with a hard queue bound |
| Background Work | Disabled for external testers |
| Request size | Existing gateway body cap plus a lower chat/media cap at the product boundary |
| Timeouts | Per upstream call and total turn deadline; cancellation releases the slot |
| Usage | Per-user daily turn/token budget and operator override |
| Fairness | FIFO within a user, fair admission across users, interactive priority |
| Failure | Retryable busy/timeout response with no cross-user retry or stream leakage |

If a model gateway cannot be delivered in the first pilot, the acceptable
shortcut is a static global semaphore plus one active turn per user and Work
disabled. It is not acceptable to expose the raw shared model endpoint or let
each Nanobot process retry without a global bound.

## Offboarding and Deletion

TestFlight's tester deletion, app uninstall, and Keychain deletion are not
Ziggy account deletion. The operator path must be:

1. Mark the user disabled and reject new bootstrap, REST, WebSocket, media, and
   connector capabilities.
2. Revoke Ziggy sessions and wait for or forcibly close active connections.
3. Stop/drain the runtime and cancel queued work.
4. Delete the user's config, workspace, sessions, media, cron state, runtime
   logs, memory Git history, indexes, temporary files, and backups according to
   the documented retention policy.
5. Revoke/delete integration credentials and remove allowlist/invitation state.
6. Preserve only a non-content audit record required to prove the action, then
   mark the user deleted.

Deletion must be idempotent and must not recursively delete a parent/shared
directory. A dry-run inventory and a post-delete negative lookup are required.
The first pilot can make deletion operator-only; it cannot defer deletion
semantics until after inviting people.

## Migration Plan

The current owner workspace is not a multi-tenant seed dataset. Migration
should be explicit:

1. Stop writes and snapshot the current Nanobot config/runtime/workspace with
   checksums.
2. Create the owner's durable user/workspace record and copy only the owner's
   data into the owner's new root. Preserve session keys and memory files only
   where they are verified to belong to the owner.
3. Start each tester with an empty root and a generated config. Never merge the
   owner's `SOUL.md`, `USER.md`, `MEMORY.md`, session files, provider config, or
   media into tester roots.
4. Validate counts, marker searches, permissions, and a conversation spot check
   before reopening the owner and inviting testers.
5. Keep the old snapshot read-only for the rollback window, then apply the
   retention/deletion policy. A binary rollback must not silently restore an old
   shared path.

Later migration to PostgreSQL-backed conversations or a memory service should
use `(workspace_id, object_id)` as the primary ownership key, dual-read only
during a bounded migration, and run the same cross-user matrix before cutover.

## Threat Model

| Actor or failure | Asset at risk | Required control | Residual pilot risk |
|---|---|---|---|
| Uninvited Clerk user | Account and workspace data | Subject/email admission before provisioning; no fail-open | Operator allowlist error; audit and two-person review for changes |
| Tester changes request IDs, workspace IDs, session keys, or chat IDs | Conversations and streams | Server-derived workspace, ownership checks on every route/event, opaque tenant mapping | A missed route; adversarial route inventory is mandatory |
| Tester guesses another runtime endpoint/token | Files, prompts, tools | Loopback/private network only, capability-bound runtime token, no public Nanobot listener | Host compromise is outside this pilot's tenant model |
| Nanobot path traversal or shell tool | Host/other tenant files and secrets | Disable exec; otherwise `restrictToWorkspace`, bwrap, unprivileged user, minimal mounts | Tool/sandbox defects require follow-up testing before enabling tools |
| Shared model or retry storm | Availability and cross-user prompt mix-up | Central admission, request correlation, cancellation, no shared mutable prompt/session state | Model server remains a shared availability dependency |
| Logs, crash dumps, metrics, backups | Private content and credentials | Redaction, content-free identifiers, access control, retention and deletion inventory | Operational tooling may still leak unless reviewed per release |
| Runtime crash/reuse | Stale files, env, token, queued events | Drain, lease expiry, fresh root verification, cleanup before reassignment | Power loss needs restore/reconciliation testing |
| Provider/MCP integration | OAuth refresh tokens and external data | Disable in pilot; later short-lived capability and encrypted vault | No integration functionality in recommended scope |
| TestFlight public link forwarding | Unplanned accounts | Email-invited external group plus Ziggy allowlist; no public link | Apple-side tester identity and Ziggy identity remain separate |
| Deletion request or operator offboarding | Retained user data | Idempotent runbook, post-delete negative tests, documented audit retention | Backups may require a defined delayed purge |

## Cross-User Leakage Test Matrix

Run this against two real Clerk test subjects, A and B, with unique marker
strings such as `TENANT_A_7f...` and `TENANT_B_3c...`. Repeat with concurrent
turns, reconnects, runtime restarts, and a slot reassignment.

| Area | Test | Pass condition |
|---|---|---|
| Admission | Valid but uninvited subject; disabled subject; expired/revoked session | No workspace/runtime/data is created or returned; generic deny; no sensitive reason in response |
| Provisioning | A and B bootstrap twice and concurrently | Exactly one workspace per subject; no duplicate or shared root; stable IDs after retry |
| REST sessions | A creates/list/reads/deletes A; A requests B key and vice versa | Own operations work; foreign operations return no data and do not alter state |
| WebSocket attach | A attaches to A; A attaches to guessed B `chat_id`; reconnect with stale token | A stream works; B attach fails; stale/revoked token cannot attach |
| Stream routing | A and B generate simultaneously; delay and reorder events | Each client receives only its own deltas, tool/status events, completion, and errors |
| Files/docs | Write/read marker files in each workspace; try `..`, absolute paths, symlinks, and shared filenames | Each root contains only its own marker; traversal/symlink escape is denied |
| Context | Put A marker in session, `USER.md`, memory, and tool result; ask B to recall it | B never receives the marker in prompt, answer, or context dump; A can recall its own marker |
| Memory | Consolidate/dream/restart A and B; search each memory store | Results are workspace-scoped; cursors and Git history do not cross roots |
| Media | Upload/produce A and B media; request foreign path/URL | Own media works only through authorized route; foreign path and guessed URL fail |
| Runtime reuse | Kill A, wait for lease expiry, start B in the same host slot | B has a clean root, env, queue, cache, token, and logs; A data is absent |
| Secrets | Inspect env/config/mounts; attempt prompt/tool access to secrets | No Clerk secret, provider key, vault plaintext, Docker socket, or other root is visible |
| Model contention | Saturate with A, submit B, cancel A, crash a turn | B gets bounded fair admission; cancellation/crash frees capacity; no mixed prompt/result |
| Offboarding | Disable/delete A while connected and while queued | A is cut off and purged per inventory; B remains live and unchanged |
| Migration | Migrate owner snapshot, provision fresh A/B, search all roots/backups | Only owner data is restored to owner; tester roots contain no owner markers |
| Observability | Inspect access logs, metrics, traces, crash/feedback artifacts | IDs/status/latency are present; prompt content, tokens, and foreign data are absent |

The matrix is a release gate, not a post-release QA list. Add route coverage
whenever a new REST, SSE, WebSocket, media, Work, or MCP endpoint is introduced.

## Must Precede TestFlight vs Can Follow

### Must precede the first external TestFlight invite

- M1 identity, allowlist, subject-bound session, and idempotent workspace
  provisioning.
- M2 per-user process/filesystem isolation, server-derived routing,
  conversation/stream ownership checks, and the complete leakage matrix.
- M3 minimum admission control, quotas, disable/delete runbook, owner migration,
  backup/restore verification, and content-safe observability.
- iOS Clerk sign-in and callback path, account switching/expiry behavior, and
  removal of the shared guest-code flow.
- App Store Connect record, matching bundle ID/signing/capabilities, processed
  distribution build, privacy policy/disclosures, export compliance, external
  testing metadata, feedback contact, and Beta App Review approval.
- A private email-invited tester group and a support runbook with tester
  identity, app build, server version, and correlation ID.

### Can follow the first pilot

- On-demand runtime startup, idle eviction, HA, automatic host placement, and a
  cluster scheduler.
- Centralized memory MCP/vector search, richer memory policy, and cross-device
  durable local cache.
- Per-user integrations, OAuth refresh, connector MCP, approval workflows, and
  secret rotation UI.
- Background Work, cron, APNs, channel adapters, sharing, and artifact
  previews.
- Self-service invitations, organizations/shared workspaces, delegated roles,
  multiple devices, advanced quota billing, and automated migration tooling.

## Recommended First Release Sequence

1. Keep the current owner deployment private. Implement M1 and create two
   disposable Clerk test subjects and empty workspaces.
2. Implement M2 with chat-only Nanobot runtimes, no exec/MCP/cron/media, and
   run the A/B matrix under concurrency and restart.
3. Implement M3's fixed model admission, small quotas, disable/delete runbook,
   and owner-only migration. Verify backup restore and deletion inventory.
4. Replace the iOS guest-code enrollment with the selected Clerk callback flow;
   validate cold-start, refresh, logout, account switch, and reconnect.
5. Create the App Store Connect record for `com.mihaichiorean.ziggy`, resolve
   signing, privacy, export compliance, and test information, then submit the
   first external build for Beta App Review.
6. Invite 3 testers by email first, observe model contention, crashes, feedback,
   and support load for several days, then expand to 10 only if the leakage,
   deletion, quota, and recovery gates remain green.

The recommended pilot is successful when testers can have private, reliable
chat conversations and the operator can prove that one tester's identity,
process, files, context, memory, stream, secrets, quota, and deletion never
cross into another tester's account. Everything else is a later product
capability, not a reason to weaken that boundary.
