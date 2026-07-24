package api

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

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
func (f *fakeDriver) DeleteTenantData(context.Context, runtime.Request) error {
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

func TestSocketRejectsOversizedRequest(t *testing.T) {
	socket, stop := startServer(t, Server{Driver: &fakeDriver{}, MaxRequestBytes: 64})
	defer stop()
	connection := dial(t, socket)
	defer connection.Close()
	if _, err := connection.Write([]byte(strings.Repeat("x", 65) + "\n")); err != nil {
		t.Fatal(err)
	}
	if response := readResponse(t, connection); response.Error != "request_too_large" {
		t.Fatalf("response=%+v", response)
	}
}

func TestSocketHardCapsSubstantiallyOversizedRequest(t *testing.T) {
	socket, stop := startServer(t, Server{Driver: &fakeDriver{}, MaxRequestBytes: 64})
	defer stop()
	connection := dial(t, socket)
	defer connection.Close()
	// A 1 MiB line must be rejected after the max+1-byte reader limit, rather
	// than accumulated by bufio.ReadString across repeated buffer fills.
	_, _ = connection.Write([]byte(strings.Repeat("x", 1<<20) + "\n"))
	if response := readResponse(t, connection); response.Error != "request_too_large" {
		t.Fatalf("response=%+v", response)
	}
}

func TestSocketTimesOutStalledPeer(t *testing.T) {
	socket, stop := startServer(t, Server{Driver: &fakeDriver{}, ReadTimeout: 50 * time.Millisecond})
	defer stop()
	connection := dial(t, socket)
	defer connection.Close()
	if response := readResponse(t, connection); response.Error != "request_timeout" {
		t.Fatalf("response=%+v", response)
	}
}

func TestSocketRejectsExcessConnections(t *testing.T) {
	driver := &blockingDriver{started: make(chan struct{}), release: make(chan struct{})}
	socket, stop := startServer(t, Server{Driver: driver, MaxConcurrent: 1, DriverTimeout: time.Second})
	defer stop()
	first := dial(t, socket)
	defer first.Close()
	writeRequest(t, first, Request{Operation: "ensure_running", WorkspaceID: "tenant-alpha", Generation: 7})
	select {
	case <-driver.started:
	case <-time.After(time.Second):
		t.Fatal("first request did not acquire the only socket slot")
	}
	second := dial(t, socket)
	defer second.Close()
	if response := readResponse(t, second); response.Error != "server_busy" {
		t.Fatalf("response=%+v", response)
	}
	close(driver.release)
	if response := readResponse(t, first); response.Status == nil || response.Status.State != runtime.StateRunning {
		t.Fatalf("response=%+v", response)
	}
}

func TestSocketDriverUsesRequestDeadline(t *testing.T) {
	driver := &blockingDriver{started: make(chan struct{})}
	socket, stop := startServer(t, Server{Driver: driver, DriverTimeout: 50 * time.Millisecond})
	defer stop()
	connection := dial(t, socket)
	defer connection.Close()
	writeRequest(t, connection, Request{Operation: "ensure_running", WorkspaceID: "tenant-alpha", Generation: 7})
	if response := readResponse(t, connection); response.Error != "lifecycle_failed" {
		t.Fatalf("response=%+v", response)
	}
	if !driver.sawDeadline() {
		t.Fatal("driver did not receive a request deadline")
	}
}

func TestPeerPolicyRestrictsDestructiveDataDeletion(t *testing.T) {
	server := Server{
		SocketGroupID:  4242,
		SocketGroupSet: true,
		DestructiveUID: 1001,
		DestructiveSet: true,
	}
	controlPeer := peerIdentity{UID: 1002, GID: 4242, PID: 99}
	if !server.authorizePeer(controlPeer) {
		t.Fatal("configured control group peer was rejected")
	}
	if server.authorizeDestructivePeer(controlPeer) {
		t.Fatal("control group peer was allowed to delete tenant data")
	}
	if !server.authorizeDestructivePeer(peerIdentity{UID: 1001, GID: 1001}) {
		t.Fatal("configured destructive peer was rejected")
	}
	if server.authorizePeer(peerIdentity{UID: 1002, GID: 4343}) {
		t.Fatal("peer outside the configured primary group was accepted")
	}
}

func TestSecondServerCannotReplaceLiveSocket(t *testing.T) {
	socket := testSocketPath(t, "manager.sock")
	server := Server{Driver: &fakeDriver{}}
	stop := startServerAt(t, server, socket)
	defer stop()

	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if err := server.ListenAndServe(ctx, socket); err == nil || !strings.Contains(err.Error(), "already owned") {
		t.Fatalf("second server error=%v", err)
	}

	connection := dial(t, socket)
	defer connection.Close()
	writeRequest(t, connection, Request{Operation: "ensure_running", WorkspaceID: "tenant-alpha", Generation: 7})
	if response := readResponse(t, connection); response.Status == nil || response.Status.State != runtime.StateRunning {
		t.Fatalf("first server socket was replaced: %+v", response)
	}
}

func TestServerRecoversStaleSocketWhileHoldingLock(t *testing.T) {
	socket := testSocketPath(t, "manager.sock")
	stale, err := net.ListenUnix("unix", &net.UnixAddr{Name: socket, Net: "unix"})
	if err != nil {
		t.Fatal(err)
	}
	stale.SetUnlinkOnClose(false)
	if err := stale.Close(); err != nil {
		t.Fatal(err)
	}
	stop := startServerAt(t, Server{Driver: &fakeDriver{}}, socket)
	stop()
}

func startServer(t *testing.T, server Server) (string, func()) {
	t.Helper()
	socket := testSocketPath(t, fmt.Sprintf("zrm-%d.sock", time.Now().UnixNano()))
	return socket, startServerAt(t, server, socket)
}

func testSocketPath(t *testing.T, name string) string {
	t.Helper()
	dir, err := os.MkdirTemp("", "zrm-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(dir) })
	return filepath.Join(dir, name)
}

func startServerAt(t *testing.T, server Server, socket string) func() {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	errs := make(chan error, 1)
	go func() { errs <- server.ListenAndServe(ctx, socket) }()
	deadline := time.Now().Add(time.Second)
	for {
		if _, err := os.Stat(socket); err == nil {
			break
		}
		select {
		case err := <-errs:
			cancel()
			t.Fatalf("manager socket failed to start: %v", err)
		default:
		}
		if time.Now().After(deadline) {
			cancel()
			t.Fatal("manager socket was not created")
		}
		time.Sleep(time.Millisecond)
	}
	return func() {
		cancel()
		select {
		case err := <-errs:
			if err != nil {
				t.Errorf("server failed: %v", err)
			}
		case <-time.After(time.Second):
			t.Error("server did not stop")
		}
	}
}

func dial(t *testing.T, socket string) *net.UnixConn {
	t.Helper()
	connection, err := net.DialUnix("unix", nil, &net.UnixAddr{Name: socket, Net: "unix"})
	if err != nil {
		t.Fatal(err)
	}
	if err := connection.SetReadDeadline(time.Now().Add(time.Second)); err != nil {
		t.Fatal(err)
	}
	return connection
}

func writeRequest(t *testing.T, connection net.Conn, request Request) {
	t.Helper()
	raw, err := json.Marshal(request)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := connection.Write(append(raw, '\n')); err != nil {
		t.Fatal(err)
	}
}

func readResponse(t *testing.T, connection net.Conn) Response {
	t.Helper()
	var response Response
	if err := json.NewDecoder(connection).Decode(&response); err != nil {
		t.Fatal(err)
	}
	return response
}

type blockingDriver struct {
	mu           sync.Mutex
	started      chan struct{}
	release      chan struct{}
	deadlineSeen bool
}

func (d *blockingDriver) EnsureRunning(ctx context.Context, request runtime.Request) (runtime.Status, error) {
	_, hasDeadline := ctx.Deadline()
	d.mu.Lock()
	d.deadlineSeen = hasDeadline
	d.mu.Unlock()
	select {
	case <-d.started:
	default:
		close(d.started)
	}
	if d.release != nil {
		select {
		case <-d.release:
			return runtime.Status{WorkspaceID: request.WorkspaceID, Generation: request.Generation, State: runtime.StateRunning}, nil
		case <-ctx.Done():
			return runtime.Status{}, ctx.Err()
		}
	}
	<-ctx.Done()
	return runtime.Status{}, ctx.Err()
}
func (d *blockingDriver) Health(context.Context, runtime.Request) (runtime.Status, error) {
	return runtime.Status{}, runtime.ErrNotFound
}
func (d *blockingDriver) Drain(context.Context, runtime.Request) (runtime.Status, error) {
	return runtime.Status{}, runtime.ErrNotFound
}
func (d *blockingDriver) Stop(context.Context, runtime.Request) (runtime.Status, error) {
	return runtime.Status{}, runtime.ErrNotFound
}
func (d *blockingDriver) Delete(context.Context, runtime.Request) error { return runtime.ErrNotFound }
func (d *blockingDriver) DeleteTenantData(context.Context, runtime.Request) error {
	return runtime.ErrNotFound
}

func (d *blockingDriver) sawDeadline() bool {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.deadlineSeen
}
