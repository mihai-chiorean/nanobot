GO ?= go
BINARY := bin/ziggy-runtime-manager
VERSION ?= $(shell git rev-parse --short HEAD 2>/dev/null || printf dev)
LDFLAGS := -s -w -X main.version=$(VERSION)

.PHONY: build test test-race vet verify clean

build:
	CGO_ENABLED=0 $(GO) build -trimpath -ldflags '$(LDFLAGS)' -o $(BINARY) ./cmd/ziggy-runtime-manager

test:
	$(GO) test ./...

test-race:
	$(GO) test -race ./...

vet:
	$(GO) vet ./...

verify: test test-race vet build

clean:
	rm -rf bin
