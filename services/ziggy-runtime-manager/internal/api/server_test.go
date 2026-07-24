package api

import (
	"bytes"
	"context"
	"errors"
	"log"
	"testing"

	"github.com/mihai-chiorean/nanobot/services/ziggy-runtime-manager/runtime"
)

type fakeDriver struct {
	request runtime.Request
}

func (f *fakeDriver) EnsureRunning(_ context.Context, request runtime.Request) (runtime.Status, error) {
	f.request = request
	return runtime.Status{WorkspaceID: request.WorkspaceID, Generation: request.Generation, State: runtime.StateRunning}, nil
}
func (f *fakeDriver) Health(context.Context, runtime.Request) (runtime.Status, error) {
	return runtime.Status{}, runtime.ErrNotFound
}
func (f *fakeDriver) Drain(context.Context, runtime.Request) (runtime.Status, error) {
	return runtime.Status{}, runtime.ErrNotFound
}
func (f *fakeDriver) Stop(context.Context, runtime.Request) (runtime.Status, error) {
	return runtime.Status{}, runtime.ErrNotFound
}
func (f *fakeDriver) Delete(context.Context, runtime.Request) error {
	return errors.New("not implemented")
}

func TestDispatchAllowsOnlyLifecycleRequestFields(t *testing.T) {
	driver := &fakeDriver{}
	server := Server{Driver: driver}
	status, err := server.dispatch(context.Background(), Request{Operation: "ensure_running", WorkspaceID: "tenant-alpha", Generation: 4})
	if err != nil || status.State != runtime.StateRunning {
		t.Fatalf("status=%+v err=%v", status, err)
	}
	if driver.request.WorkspaceID != "tenant-alpha" || driver.request.Generation != 4 {
		t.Fatalf("unexpected driver request: %+v", driver.request)
	}
}

func TestLifecycleLogRedactsUntrustedContent(t *testing.T) {
	var output bytes.Buffer
	server := Server{Logger: log.New(&output, "", 0)}
	server.log(Request{Operation: "secret=do-not-log", WorkspaceID: "prompt contains a secret", Generation: 1}, "failed")
	if bytes.Contains(output.Bytes(), []byte("secret")) || bytes.Contains(output.Bytes(), []byte("prompt")) {
		t.Fatalf("untrusted request content appeared in lifecycle log: %s", output.String())
	}
}
