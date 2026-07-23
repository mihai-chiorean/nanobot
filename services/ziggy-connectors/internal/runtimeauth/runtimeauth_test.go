package runtimeauth

import (
	"strings"
	"testing"
	"time"
)

func TestTokenManagerIssuesAndVerifiesFiveMinuteRuntimeToken(t *testing.T) {
	key := []byte("01234567890123456789012345678901")
	issuer := "http://127.0.0.1:8790"
	audience := issuer + "/mcp"
	manager, err := New(key, issuer, audience)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Unix(1_700_000_000, 0)
	token, err := manager.Issue(Claims{Issuer: issuer, Audience: audience, IssuedAt: now.Unix(), ExpiresAt: now.Add(TTL).Unix(), JWTID: "jti", ClientID: "client", UserID: "user", WorkspaceID: "workspace", RuntimeID: "runtime", RuntimeGeneration: 3, Scopes: []string{"gmail.read"}})
	if err != nil {
		t.Fatal(err)
	}
	claims, err := manager.Verify(token, now.Add(time.Minute))
	if err != nil || !claims.HasScope("gmail.read") || claims.ExpiresAt != now.Add(TTL).Unix() {
		t.Fatalf("Verify() claims = %#v, error = %v", claims, err)
	}
	if _, err := manager.Verify(token, now.Add(TTL)); err == nil {
		t.Fatal("accepted expired token")
	}
	parts := strings.Split(token, ".")
	parts[1] = "tampered"
	if _, err := manager.Verify(strings.Join(parts, "."), now); err == nil {
		t.Fatal("accepted tampered token")
	}
}
