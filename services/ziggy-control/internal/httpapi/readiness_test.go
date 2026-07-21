package httpapi

import (
	"context"
	"net/http"
	"net/http/httptest"
	"net/url"
	"sync/atomic"
	"testing"
	"time"
)

func TestHTTPReadinessChecker(t *testing.T) {
	tests := []struct {
		name    string
		status  int
		wantErr bool
	}{
		{name: "successful", status: http.StatusNoContent},
		{name: "missing route", status: http.StatusNotFound, wantErr: true},
		{name: "upstream error", status: http.StatusInternalServerError, wantErr: true},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				w.WriteHeader(test.status)
			}))
			defer server.Close()
			target, _ := url.Parse(server.URL)
			checker := NewHTTPReadinessChecker(target, "/healthz", time.Second, time.Second, nil)
			err := checker.Check(context.Background())
			if (err != nil) != test.wantErr {
				t.Errorf("Check() error = %v, wantErr %v", err, test.wantErr)
			}
		})
	}
}

func TestHTTPReadinessCheckerHonorsTimeout(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		time.Sleep(200 * time.Millisecond)
	}))
	defer server.Close()
	target, _ := url.Parse(server.URL)
	checker := NewHTTPReadinessChecker(target, "/", 20*time.Millisecond, time.Second, nil)

	if err := checker.Check(context.Background()); err == nil {
		t.Fatal("Check() error = nil, want timeout")
	}
}

func TestHTTPReadinessCheckerCachesSuccessfulProbe(t *testing.T) {
	var requests atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		if r.URL.Path != "/healthz" {
			t.Errorf("path = %q, want /healthz", r.URL.Path)
		}
		w.WriteHeader(http.StatusNoContent)
	}))
	defer server.Close()
	target, _ := url.Parse(server.URL + "/base")
	checker := NewHTTPReadinessChecker(target, "/healthz", time.Second, time.Minute, nil)

	for range 3 {
		if err := checker.Check(context.Background()); err != nil {
			t.Fatalf("Check() error = %v", err)
		}
	}
	if got := requests.Load(); got != 1 {
		t.Errorf("upstream requests = %d, want 1", got)
	}
}
