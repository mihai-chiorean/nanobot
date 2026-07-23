SERVICE := ziggy-work

.PHONY: build test race vet fmt migrate migrate-river import reconcile

build:
	go build -ldflags "-X main.version=$${ZIGGY_WORK_VERSION:-dev}" ./cmd/ziggy-work ./cmd/import-nanobot ./cmd/reconcile-runtimes ./cmd/migrate-river

fmt:
	gofmt -w cmd internal

test:
	go test ./...

race:
	go test -race ./...

vet:
	go vet ./...

# Run these explicitly during deployment. The service never migrates silently.
migrate:
	psql "$${ZIGGY_WORK_DATABASE_URL}" -v ON_ERROR_STOP=1 -f migrations/001_work_foundation.sql

migrate-river:
	go run ./cmd/migrate-river

import:
	go run ./cmd/import-nanobot --input "$${INPUT}" --user-id "$${USER_ID}" --workspace-id "$${WORKSPACE_ID}" --artifact-root "$${ARTIFACT_ROOT}" --artifact-destination "$${ARTIFACT_DESTINATION}"

reconcile:
	go run ./cmd/reconcile-runtimes
