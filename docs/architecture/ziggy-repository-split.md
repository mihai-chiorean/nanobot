# Ziggy Repository Split

## Decision

Ziggy should become a product repository with a narrow, explicit dependency on
Nanobot. The current fork remains the source of truth until the export is
reviewed, but the product repository must not contain a root nanobot/ tree,
Nanobot Python tests, Nanobot packaging files, or Nanobot's generated WebUI
bundle.

The export is a one-way operation. It copies tracked allowlisted files into a
new directory, writes deterministic metadata and product CI files, and refuses
to overwrite an existing destination, initializes a new Git repository on its
main branch, and validates the result. It never deletes or moves the source
repository.

The contract is machine-readable in
config/ziggy-repository-split.json. The implementation is
scripts/export-ziggy-product.sh. The validator and its negative tests are
scripts/validate-ziggy-product-split.py and
scripts/test-ziggy-product-split.sh.

## Target layout

~~~text
.
├── ios/                         SwiftUI client, Swift Testing, and UI XCTest target
├── web/                         branded PWA, assets, tests, and bridge extension
├── services/
│   ├── ziggy-control/           authenticated product front door
│   ├── ziggy-connectors/        tenant connectors and MCP OAuth client
│   └── ziggy-work/              durable Work service
├── observability/               collector, dashboards, and privacy checks
├── deploy/                      index; implementation stays with each service
├── docs/architecture/           Ziggy architecture contracts
├── docs/runbooks/               canonical operational runbooks
├── docs/research/               research notes
├── licenses/                    Nanobot license and third-party attribution
├── config/                      this split contract
├── scripts/                     export and split validation tools
├── .github/workflows/           generated product CI
└── .ziggy/                      export metadata and Nanobot lock
~~~

The source webui/ directory is renamed to web/. The export transform rewrites
the copied Vite config so its output is web/dist, never ../nanobot/web/dist.
The source bridge/ directory is placed at web/bridge/. The three service
directories are preserved so their Go modules, tests, migrations, and
service-local deployment assets remain together. observability/ is promoted
from services/observability/. Root runbooks/ and research/ are indexes only;
the canonical documents stay under docs/ so existing relative links remain
valid.

The export intentionally omits root Python tests, pyproject.toml, Dockerfile,
docker-compose.yml, entrypoint.sh, Nanobot source, and the generated
nanobot/web/dist bundle. Nanobot is a runtime dependency, not a product
subdirectory.

## Dependency pin and update model

The upstream baseline contract is:

~~~text
repository: https://github.com/HKUDS/nanobot.git
ref:        v0.1.5.post3
commit:     0b1631f33d8040802aa09d66a01bc731e3cb85a2
~~~

This baseline is not the effective production runtime yet. Plain upstream
v0.1.5.post3 is not runnable for Ziggy while the patch queue in this document
remains. Until a patched image digest or wheel exists, the exporter emits an
effective_runtime pin of kind source-export using the source repository, source
ref, and exact export commit. That pin is the runtime currently being handed
off to deployment. The lock therefore records both upstream_baseline and
effective_runtime; validation requires both to agree with the export metadata.

A deployment may replace effective_runtime with an immutable patched container
digest or wheel URL after the patch queue is packaged. That transition changes
the effective pin, not Ziggy product history or the upstream baseline.

An update is a dependency change, not a product rebase:

1. Select a Nanobot tag or commit, or build a separately owned runtime artifact
   with an immutable digest.
2. Update the upstream baseline ref and commit together in the split manifest;
   the effective source-export pin is generated from the export source.
3. Run the product contract tests: WebSocket authentication and transport,
   Work command/event compatibility, MCP OAuth, memory path safety, and the
   three Go service suites.
4. Record which patch entries disappeared or changed classification.
5. Export again and review the generated .ziggy metadata.

While a patch is still required, the deployment can consume a separately pinned
Ziggy runtime artifact or patch overlay. That artifact may be built from a
temporary patch queue, but its source must not be copied into this product
repository. This lets Nanobot advance without rebasing Ziggy product history.

## Remaining Nanobot patch queue

The entries below describe the smallest behavioral surfaces that currently
prevent a plain upstream Nanobot runtime. Removal order is deliberate: lower
numbers remove product-specific coupling first, then upstreamable changes can
be released and the dependency pin can advance.

| Order | Surface | Classification | Current evidence | Removal action |
| --- | --- | --- | --- | --- |
| 0 | Web branding and packaged frontend | product-boundary | 89815b71, 479882fe; nanobot/web/dist and pyproject.toml | Keep the PWA in web/ and stop shipping Nanobot's generated bundle. |
| 1 | Runtime tenant auth and token transport | product-boundary | a183956f, 89815b71, 479882fe, 3fdedbea, 92823268 | Move Clerk verification, tenant admission, and token issuance to ziggy-control. Leave a generic runtime transport contract. |
| 2 | Durable Work hooks | product-boundary | 55683729, 89815b71, 479882fe | Make ziggy-work the durable owner and keep only a versioned command/event adapter in the runtime. |
| 3 | MCP OAuth client credentials | upstreamable | 4addeb38, 145cec6f, 6972981c | Upstream generic OAuth client-credentials and token-endpoint configuration. Keep tenant credential storage in ziggy-connectors. |
| 4 | Memory and RAG tenant scope | product-boundary | 2343f7c, ac1fe757 | Bind memory roots and recall providers at the per-tenant runtime boundary. Upstream independent path-safety checks. |
| 5 | Generic runtime correctness | upstreamable | 8314f206, 92823268 | Submit isolated fixes with tests, then advance the pinned dependency. |
| 6 | Patched runtime delivery | temporary | d2eb946 | Use a separately pinned artifact or overlay until orders 1 through 5 are gone. |

The manifest contains the exact files for each surface. The classification is
about ownership, not whether the implementation is valuable: a product-boundary
patch can remain valuable while being removed from Nanobot by moving its
responsibility into Ziggy control, Work, connectors, or the PWA.

Generic WebSocket Bearer-header handling, generic lifecycle hooks, OAuth
token-endpoint behavior, and directory traversal protections are good
upstream candidates. Clerk tenant policy, durable tenant storage, Omi
provider binding, and Ziggy-specific Work semantics are product boundaries.
Generated branding output is removed immediately because it creates the
largest accidental fork surface.

## CI, build, and test mapping

The exporter generates .github/workflows/ziggy-product.yml. The mapping is
intentionally product-only:

| Component | Working directory | Required checks |
| --- | --- | --- |
| Swift client | ios/ | Resolve Swift packages; xcodebuild build-for-testing; simulator test-without-building. |
| PWA | web/ | npm ci, npm test, npm run lint, npm run build. |
| ziggy-control | services/ziggy-control/ | make verify, linux-amd64, linux-arm64. |
| ziggy-connectors | services/ziggy-connectors/ | make verify, linux-amd64, linux-arm64. |
| ziggy-work | services/ziggy-work/ | go test, go test -race, go vet, go build. |
| Observability | observability/otelcol/ | shell syntax checks; profile and privacy-canary validation in deployment CI. |

Each Go service keeps its own go.mod, go.sum, Makefile, migrations, tests,
and deployment assets. The product repository does not add a Go workspace
file; independent modules keep service release and dependency updates
separate.

## Deployment, docs, and notices

Deployment implementation remains beside the service that owns it. The
top-level deploy/README.md is an index and must not become a second copy.
Collector assets move to observability/. Runbooks and research remain under
docs/runbooks/ and docs/research/ and receive root indexes for discoverability.

The Nanobot MIT license and original third-party notice are copied under
licenses/. The generated root LICENSE and THIRD_PARTY_NOTICES.md make the
dependency boundary visible without claiming to redistribute Nanobot source.
Web and Go dependency notices remain governed by their package lockfiles and
module metadata.

Secrets and generated artifacts are excluded in two ways: the exporter
requires a clean source worktree and enumerates git-tracked files only, and
the validator rejects secret-like names, unknown top-level paths, root
nanobot/, and generated fork packaging paths. Nested real .env files are
rejected. Bounded text scans include example and sample files and reject
private-key headers, Grafana Cloud glc_ tokens, sk_live_/sk_test_ tokens, and
Google API key forms. Fixtures use explicit non-matching placeholders; only
generated dependency and build directories are skipped from content scanning.

The web CI runs npm audit --audit-level=high as a non-blocking report and then
validates the product tree again after npm run build. The current lockfile
reports two high and two critical advisories; they require dependency-owner
triage and targeted upgrades, not broad force upgrades.

## History strategy

The first private repository should be created from one verified export
commit. This gives a reviewable boundary and avoids importing all Nanobot
history. The source commit is recorded in .ziggy/export-metadata.json.

Useful Ziggy history can be prepared separately in a disposable clone. Do not
run history filtering in the active source worktree:

~~~sh
git clone git@github.com:mihai-chiorean/nanobot.git /tmp/ziggy-history
cd /tmp/ziggy-history
git filter-repo --force \
  --path ios/ \
  --path webui/ \
  --path bridge/ \
  --path services/ziggy-control/ \
  --path services/ziggy-connectors/ \
  --path services/ziggy-work/ \
  --path services/observability/ \
  --path docs/architecture/ \
  --path docs/runbooks/ \
  --path docs/research/ \
  --path docs/reviews/ \
  --path LICENSE \
  --path THIRD_PARTY_NOTICES.md \
  --path-rename webui/:web/ \
  --path-rename bridge/:web/bridge/ \
  --path-rename services/observability/:observability/
~~~

Review the filtered result, then layer the verified export over it as the
current tree. Generated files, root indexes, and the final manifest should be
introduced by the export commit. Nanobot paths must stay out of the filtered
history. If component-level provenance is more useful, use git subtree split
on each product directory and import only those refs; do not combine a
Nanobot subtree with the product repository.

## Commands

From a clean source worktree:

~~~sh
scripts/validate-ziggy-product-split.py --source .
scripts/export-ziggy-product.sh --destination /tmp/ziggy-product-export
scripts/test-ziggy-product-split.sh \
  --source . \
  --export /tmp/ziggy-product-export
~~~

The exporter refuses an existing destination. This is intentional: choose a
new destination for every export and do not delete or move the current
repository as part of separation.
