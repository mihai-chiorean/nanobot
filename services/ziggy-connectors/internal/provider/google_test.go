package provider

import (
	"net/url"
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
