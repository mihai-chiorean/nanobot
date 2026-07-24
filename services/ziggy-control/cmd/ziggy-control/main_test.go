package main

import (
	"context"
	"io"
	"log/slog"
	"sync/atomic"
	"testing"
	"time"
)

type tenantCountReaderStub struct {
	count atomic.Int64
}

func (stub *tenantCountReaderStub) AdmissionTenantCount(context.Context) (int, error) {
	return int(stub.count.Load()), nil
}

func TestTenantCountRefreshPublishesWithoutRequestPathLocks(t *testing.T) {
	store := &tenantCountReaderStub{}
	store.count.Store(7)
	source := newAtomicTenantCount(1)
	ctx, cancel := context.WithCancel(context.Background())
	done := startTenantCountRefresh(
		ctx,
		store,
		source,
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		time.Millisecond,
	)

	deadline := time.Now().Add(time.Second)
	for source.CurrentTenantCount() != 7 && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	if got := source.CurrentTenantCount(); got != 7 {
		t.Fatalf("tenant count = %d, want 7", got)
	}
	cancel()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("tenant count refresh did not stop")
	}
}

func TestAtomicTenantCountKeepsAdmissionDenominatorPositive(t *testing.T) {
	source := newAtomicTenantCount(0)
	if got := source.CurrentTenantCount(); got != 1 {
		t.Fatalf("zero tenant count = %d, want 1", got)
	}
	source.Store(-1)
	if got := source.CurrentTenantCount(); got != 1 {
		t.Fatalf("negative tenant count = %d, want 1", got)
	}
}
