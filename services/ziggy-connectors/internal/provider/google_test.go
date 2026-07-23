package provider

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
)

func TestGoogleAuthorizationURLRequestsOfflineGranularConsent(t *testing.T) {
	google := NewGoogle(GoogleConfig{
		ClientID: "client-id",
		AuthURL:  "https://accounts.google.test/o/oauth2/v2/auth",
	})
	raw, err := google.AuthorizationURL(
		"https://gateway.test/oauth/google/callback",
		"state",
		"verifier",
		[]string{"openid", "email", "https://www.googleapis.com/auth/gmail.readonly"},
	)
	if err != nil {
		t.Fatal(err)
	}
	parsed, err := url.Parse(raw)
	if err != nil {
		t.Fatal(err)
	}
	query := parsed.Query()
	if got := query.Get("access_type"); got != "offline" {
		t.Fatalf("access_type = %q", got)
	}
	if got := query.Get("include_granted_scopes"); got != "true" {
		t.Fatalf("include_granted_scopes = %q", got)
	}
	if got := query.Get("prompt"); got != "consent select_account" {
		t.Fatalf("prompt = %q", got)
	}
	if got := query.Get("scope"); got != "openid email https://www.googleapis.com/auth/gmail.readonly" {
		t.Fatalf("scope = %q", got)
	}
}

func TestGoogleRefreshSearchAndReadMessage(t *testing.T) {
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/token":
			if err := r.ParseForm(); err != nil {
				t.Error(err)
			}
			if r.Form.Get("refresh_token") != "refresh-token" || r.Form.Get("grant_type") != "refresh_token" {
				t.Errorf("refresh form = %v", r.Form)
			}
			writeProviderJSON(t, w, map[string]any{"access_token": "access-token", "expires_in": 3600})
		case "/gmail/v1/users/me/messages":
			if r.Header.Get("Authorization") != "Bearer access-token" || r.URL.Query().Get("q") != "newer_than:1d" {
				t.Errorf("search request headers/query = %v %v", r.Header, r.URL.Query())
			}
			writeProviderJSON(t, w, map[string]any{
				"messages": []map[string]string{{"id": "message-1", "threadId": "thread-1"}},
			})
		case "/gmail/v1/users/me/messages/message-1":
			switch r.URL.Query().Get("format") {
			case "metadata":
				writeProviderJSON(t, w, gmailTestMessage(""))
			case "full":
				writeProviderJSON(t, w, gmailTestMessage(
					base64.RawURLEncoding.EncodeToString([]byte("<html><style>hidden</style><body>Useful <b>newsletter</b></body></html>")),
				))
			default:
				t.Errorf("message format = %q", r.URL.Query().Get("format"))
				w.WriteHeader(http.StatusBadRequest)
			}
		default:
			t.Errorf("unexpected path %q", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()

	google := NewGoogle(GoogleConfig{
		ClientID:     "client-id",
		ClientSecret: "client-secret",
		TokenURL:     server.URL + "/token",
		GmailAPIURL:  server.URL + "/gmail/v1",
		HTTPClient:   server.Client(),
	})
	token, err := google.RefreshAccessToken(context.Background(), "refresh-token")
	if err != nil || token.AccessToken != "access-token" {
		t.Fatalf("refresh token = %#v, error = %v", token, err)
	}
	messages, err := google.SearchMessages(context.Background(), token.AccessToken, "newer_than:1d", 5)
	if err != nil {
		t.Fatal(err)
	}
	if len(messages) != 1 || messages[0].Subject != "Daily update" || messages[0].From != "sender@example.test" {
		t.Fatalf("messages = %#v", messages)
	}
	message, err := google.GetMessage(context.Background(), token.AccessToken, messages[0].ID)
	if err != nil {
		t.Fatal(err)
	}
	if message.Body != "Useful newsletter" || strings.Contains(message.Body, "hidden") {
		t.Fatalf("message body = %q", message.Body)
	}
}

func gmailTestMessage(htmlBody string) map[string]any {
	return map[string]any{
		"id":       "message-1",
		"threadId": "thread-1",
		"snippet":  "Useful newsletter",
		"labelIds": []string{"INBOX"},
		"payload": map[string]any{
			"mimeType": "multipart/alternative",
			"headers": []map[string]string{
				{"name": "Subject", "value": "Daily update"},
				{"name": "From", "value": "sender@example.test"},
				{"name": "To", "value": "owner@example.test"},
				{"name": "Date", "value": "Tue, 22 Jul 2026 12:00:00 -0700"},
			},
			"parts": []map[string]any{{
				"mimeType": "text/html",
				"body":     map[string]string{"data": htmlBody},
			}},
		},
	}
}

func writeProviderJSON(t *testing.T, w http.ResponseWriter, value any) {
	t.Helper()
	w.Header().Set("Content-Type", "application/json")
	if err := json.NewEncoder(w).Encode(value); err != nil {
		t.Error(err)
	}
}
