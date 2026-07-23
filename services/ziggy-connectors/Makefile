GO ?= go
BINARY := bin/ziggy-connectors

.PHONY: build test race vet vuln verify linux-amd64 linux-arm64 clean

build:
	CGO_ENABLED=0 $(GO) build -trimpath -o $(BINARY) ./cmd/ziggy-connectors

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

linux-arm64:
	GOOS=linux GOARCH=arm64 CGO_ENABLED=0 $(GO) build -trimpath -o bin/ziggy-connectors-linux-arm64 ./cmd/ziggy-connectors

clean:
	rm -rf bin
