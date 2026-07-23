package runtimeauth

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"strings"
	"time"
)

const TTL = 5 * time.Minute

type Claims struct {
	Issuer            string   `json:"iss"`
	Audience          string   `json:"aud"`
	IssuedAt          int64    `json:"iat"`
	ExpiresAt         int64    `json:"exp"`
	JWTID             string   `json:"jti"`
	ClientID          string   `json:"client_id"`
	UserID            string   `json:"user_id"`
	WorkspaceID       string   `json:"workspace_id"`
	RuntimeID         string   `json:"runtime_id"`
	RuntimeGeneration int64    `json:"runtime_generation"`
	Scopes            []string `json:"scopes"`
}

func (c Claims) HasScope(scope string) bool {
	for _, candidate := range c.Scopes {
		if candidate == scope {
			return true
		}
	}
	return false
}

type TokenManager struct {
	key      []byte
	issuer   string
	audience string
}

func New(key []byte, issuer, audience string) (*TokenManager, error) {
	if len(key) < 32 || strings.TrimSpace(issuer) == "" || strings.TrimSpace(audience) == "" {
		return nil, errors.New("MCP access signing key, issuer, and audience are required")
	}
	return &TokenManager{key: append([]byte(nil), key...), issuer: issuer, audience: audience}, nil
}

func (m *TokenManager) Issue(claims Claims) (string, error) {
	if m == nil || len(m.key) < 32 || !m.validClaims(claims) {
		return "", errors.New("invalid runtime access token claims")
	}
	header, err := json.Marshal(struct {
		Algorithm string `json:"alg"`
		Type      string `json:"typ"`
	}{Algorithm: "HS256", Type: "JWT"})
	if err != nil {
		return "", err
	}
	payload, err := json.Marshal(claims)
	if err != nil {
		return "", err
	}
	signingInput := base64.RawURLEncoding.EncodeToString(header) + "." + base64.RawURLEncoding.EncodeToString(payload)
	return signingInput + "." + base64.RawURLEncoding.EncodeToString(m.sign(signingInput)), nil
}

func (m *TokenManager) Verify(token string, now time.Time) (Claims, error) {
	if m == nil || len(m.key) < 32 {
		return Claims{}, errors.New("runtime access token verifier unavailable")
	}
	parts := strings.Split(token, ".")
	if len(parts) != 3 || parts[0] == "" || parts[1] == "" || parts[2] == "" {
		return Claims{}, errors.New("invalid runtime access token")
	}
	supplied, err := base64.RawURLEncoding.DecodeString(parts[2])
	if err != nil || !hmac.Equal(supplied, m.sign(parts[0]+"."+parts[1])) {
		return Claims{}, errors.New("invalid runtime access token")
	}
	var header struct {
		Algorithm string `json:"alg"`
		Type      string `json:"typ"`
	}
	headerBytes, err := base64.RawURLEncoding.DecodeString(parts[0])
	if err != nil || json.Unmarshal(headerBytes, &header) != nil || header.Algorithm != "HS256" || header.Type != "JWT" {
		return Claims{}, errors.New("invalid runtime access token")
	}
	payload, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return Claims{}, errors.New("invalid runtime access token")
	}
	var claims Claims
	if json.Unmarshal(payload, &claims) != nil || !m.validClaims(claims) || claims.ExpiresAt <= now.Unix() || claims.IssuedAt > now.Add(30*time.Second).Unix() {
		return Claims{}, errors.New("invalid runtime access token")
	}
	return claims, nil
}

func (m *TokenManager) sign(value string) []byte {
	mac := hmac.New(sha256.New, m.key)
	_, _ = mac.Write([]byte(value))
	return mac.Sum(nil)
}

func (m *TokenManager) validClaims(claims Claims) bool {
	return claims.Issuer == m.issuer && claims.Audience == m.audience && claims.IssuedAt > 0 && claims.ExpiresAt == claims.IssuedAt+int64(TTL/time.Second) &&
		strings.TrimSpace(claims.JWTID) != "" && strings.TrimSpace(claims.ClientID) != "" && strings.TrimSpace(claims.UserID) != "" &&
		strings.TrimSpace(claims.WorkspaceID) != "" && strings.TrimSpace(claims.RuntimeID) != "" && claims.RuntimeGeneration > 0 && len(claims.Scopes) > 0
}
