package crypto

import (
	"crypto/hmac"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"
)

type StateClaims struct {
	TransactionID string `json:"tid"`
	UserID        string `json:"uid"`
	WorkspaceID   string `json:"wid"`
	Nonce         string `json:"nonce"`
	ExpiresAt     int64  `json:"exp"`
}

type StateSigner struct{ key []byte }

func NewStateSigner(key []byte) *StateSigner { return &StateSigner{key: append([]byte(nil), key...)} }

func (s *StateSigner) Sign(claims StateClaims) (string, error) {
	if len(s.key) < 32 || claims.TransactionID == "" || claims.UserID == "" || claims.WorkspaceID == "" || claims.Nonce == "" {
		return "", errors.New("invalid OAuth state signer or claims")
	}
	payload, err := json.Marshal(claims)
	if err != nil {
		return "", fmt.Errorf("marshal OAuth state: %w", err)
	}
	encoded := base64.RawURLEncoding.EncodeToString(payload)
	return "v1." + encoded + "." + s.mac(encoded), nil
}

func (s *StateSigner) Verify(state string, now time.Time) (StateClaims, error) {
	parts := strings.Split(state, ".")
	if len(parts) != 3 || parts[0] != "v1" || len(s.key) < 32 {
		return StateClaims{}, errors.New("invalid OAuth state")
	}
	supplied, err := base64.RawURLEncoding.DecodeString(parts[2])
	if err != nil {
		return StateClaims{}, errors.New("invalid OAuth state signature")
	}
	expected := s.macBytes(parts[1])
	if subtle.ConstantTimeCompare(supplied, expected) != 1 {
		return StateClaims{}, errors.New("invalid OAuth state signature")
	}
	payload, err := base64.RawURLEncoding.DecodeString(parts[1])
	var claims StateClaims
	if err != nil || json.Unmarshal(payload, &claims) != nil {
		return StateClaims{}, errors.New("invalid OAuth state payload")
	}
	if claims.TransactionID == "" || claims.UserID == "" || claims.WorkspaceID == "" || claims.Nonce == "" || claims.ExpiresAt <= now.Unix() {
		return StateClaims{}, errors.New("expired or incomplete OAuth state")
	}
	return claims, nil
}

func (s *StateSigner) mac(encoded string) string {
	return base64.RawURLEncoding.EncodeToString(s.macBytes(encoded))
}

func (s *StateSigner) macBytes(encoded string) []byte {
	m := hmac.New(sha256.New, s.key)
	_, _ = m.Write([]byte(encoded))
	return m.Sum(nil)
}

func Hash(value string) []byte {
	sum := sha256.Sum256([]byte(value))
	return sum[:]
}
