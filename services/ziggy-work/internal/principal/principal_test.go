package principal

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"net/http/httptest"
	"testing"
)

func TestVerifyRequestMatchesConnectorContract(t *testing.T) {
	key := []byte("01234567890123456789012345678901")
	payload := base64.RawURLEncoding.EncodeToString([]byte(`{"user_id":"u","workspace_id":"w","expires_at":4102444800}`))
	mac := hmac.New(sha256.New, key)
	_, _ = mac.Write([]byte(payload))
	req := httptest.NewRequest("GET", "/api/work", nil)
	req.Header.Set(HeaderPayload, payload)
	req.Header.Set(HeaderSignature, base64.RawURLEncoding.EncodeToString(mac.Sum(nil)))
	p, err := NewVerifier(key).VerifyRequest(req)
	if err != nil {
		t.Fatal(err)
	}
	if p.UserID != "u" || p.WorkspaceID != "w" {
		t.Fatalf("principal=%+v", p)
	}
}
