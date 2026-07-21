package httpapi

import (
	"context"
	"net/http"
	"net/http/httptest"
	"net/url"
	"testing"
	"time"
)

func TestHTTPReadinessChecker(t *testing.T) {
	tests := []struct {
		name    string
		status  int
		wantErr bool
	}{
		{name: "reachable even when route missing", status: http.StatusNotFound},
		{name: "upstream error", status: http.StatusInternalServerError, wantErr: true},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				w.WriteHeader(test.status)
			}))
			defer server.Close()
			target, _ := url.Parse(server.URL)
			checker := NewHTTPReadinessChecker(target, time.Second)
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
	checker := NewHTTPReadinessChecker(target, 20*time.Millisecond)

	if err := checker.Check(context.Background()); err == nil {
		t.Fatal("Check() error = nil, want timeout")
	}
}
