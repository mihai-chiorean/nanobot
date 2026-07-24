GO ?= go
BINARY := bin/ziggy-control
IMPORTER := bin/import-tenant-manifest
VERSION ?= $(shell git rev-parse --short HEAD 2>/dev/null || printf dev)
LDFLAGS := -s -w -X main.version=$(VERSION)

.PHONY: build tenant-import test test-race vet verify linux-amd64 linux-arm64 clean

build:
	CGO_ENABLED=0 $(GO) build -trimpath -ldflags '$(LDFLAGS)' -o $(BINARY) ./cmd/ziggy-control

tenant-import:
	CGO_ENABLED=0 $(GO) build -trimpath -o $(IMPORTER) ./cmd/import-tenant-manifest

test:
	$(GO) test ./...

test-race:
	$(GO) test -race ./...

vet:
	$(GO) vet ./...

verify: test test-race vet build

linux-amd64:
	GOOS=linux GOARCH=amd64 CGO_ENABLED=0 $(GO) build -trimpath -ldflags '$(LDFLAGS)' -o bin/ziggy-control-linux-amd64 ./cmd/ziggy-control

linux-arm64:
	GOOS=linux GOARCH=arm64 CGO_ENABLED=0 $(GO) build -trimpath -ldflags '$(LDFLAGS)' -o bin/ziggy-control-linux-arm64 ./cmd/ziggy-control

clean:
	rm -rf bin coverage.out
