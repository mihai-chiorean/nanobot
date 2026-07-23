GO ?= go
BINARY := bin/ziggy-connectors
PROVISION_BINARY := bin/provision-runtime-oauth-client

.PHONY: build test race vet vuln verify linux-amd64 linux-arm64 clean

build:
	CGO_ENABLED=0 $(GO) build -trimpath -o $(BINARY) ./cmd/ziggy-connectors
	CGO_ENABLED=0 $(GO) build -trimpath -o $(PROVISION_BINARY) ./cmd/provision-runtime-oauth-client

test:
	$(GO) test ./...

race:
	$(GO) test -race ./...

vet:
	$(GO) vet ./...

vuln:
	$(GO) run golang.org/x/vuln/cmd/govulncheck@latest ./...

verify: test race vet vuln build

linux-amd64:
	GOOS=linux GOARCH=amd64 CGO_ENABLED=0 $(GO) build -trimpath -o bin/ziggy-connectors-linux-amd64 ./cmd/ziggy-connectors
	GOOS=linux GOARCH=amd64 CGO_ENABLED=0 $(GO) build -trimpath -o bin/provision-runtime-oauth-client-linux-amd64 ./cmd/provision-runtime-oauth-client

linux-arm64:
	GOOS=linux GOARCH=arm64 CGO_ENABLED=0 $(GO) build -trimpath -o bin/ziggy-connectors-linux-arm64 ./cmd/ziggy-connectors
	GOOS=linux GOARCH=arm64 CGO_ENABLED=0 $(GO) build -trimpath -o bin/provision-runtime-oauth-client-linux-arm64 ./cmd/provision-runtime-oauth-client

clean:
	rm -rf bin
