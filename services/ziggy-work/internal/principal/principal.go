package principal

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"net/http"
	"strings"
	"time"
)

const (
	HeaderPayload   = "X-Ziggy-Principal"
	HeaderSignature = "X-Ziggy-Principal-Signature"
)

type Principal struct {
	UserID      string `json:"user_id"`
	WorkspaceID string `json:"workspace_id"`
	ExpiresAt   int64  `json:"expires_at"`
}

type Verifier struct {
	key []byte
	now func() time.Time
}

func NewVerifier(key []byte) *Verifier {
	return &Verifier{key: append([]byte(nil), key...), now: time.Now}
}

func (p Principal) Validate(now time.Time) error {
	if strings.TrimSpace(p.UserID) == "" || strings.TrimSpace(p.WorkspaceID) == "" {
		return errors.New("principal user_id and workspace_id are required")
	}
	if p.ExpiresAt <= now.Unix() {
		return errors.New("principal expired")
	}
	return nil
}

func (v *Verifier) VerifyRequest(r *http.Request) (Principal, error) {
	encoded, signature := strings.TrimSpace(r.Header.Get(HeaderPayload)), strings.TrimSpace(r.Header.Get(HeaderSignature))
	if encoded == "" || signature == "" || len(v.key) == 0 {
		return Principal{}, errors.New("private gateway principal is required")
	}
	supplied, err := base64.RawURLEncoding.DecodeString(signature)
	if err != nil {
		return Principal{}, errors.New("invalid principal signature")
	}
	mac := hmac.New(sha256.New, v.key)
	_, _ = mac.Write([]byte(encoded))
	if !hmac.Equal(supplied, mac.Sum(nil)) {
		return Principal{}, errors.New("invalid principal signature")
	}
	payload, err := base64.RawURLEncoding.DecodeString(encoded)
	if err != nil {
		return Principal{}, errors.New("invalid principal payload")
	}
	var p Principal
	if json.Unmarshal(payload, &p) != nil {
		return Principal{}, errors.New("invalid principal payload")
	}
	if err := p.Validate(v.now()); err != nil {
		return Principal{}, err
	}
	return p, nil
}
