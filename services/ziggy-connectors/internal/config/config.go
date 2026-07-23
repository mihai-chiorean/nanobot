package config

import (
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"net"
	"net/url"
	"os"
	"strings"
	"time"
)

const (
	defaultListenAddr      = "127.0.0.1:8790"
	defaultShutdownTimeout = 10 * time.Second
	defaultStateTTL        = 10 * time.Minute
	defaultGoogleAuthURL   = "https://accounts.google.com/o/oauth2/v2/auth"
	defaultGoogleTokenURL  = "https://oauth2.googleapis.com/token"
	defaultGoogleUserInfo  = "https://openidconnect.googleapis.com/v1/userinfo"
	defaultGoogleProfile   = "https://gmail.googleapis.com/gmail/v1/users/me/profile"
	defaultGoogleGmailAPI  = "https://gmail.googleapis.com/gmail/v1"
)

type Config struct {
	Environment        string
	Version            string
	ListenAddr         string
	ShutdownTimeout    time.Duration
	StateTTL           time.Duration
	GoogleClientID     string
	GoogleClientSecret string
	GoogleRedirectURI  string
	GoogleAuthURL      string
	GoogleTokenURL     string
	GoogleUserInfoURL  string
	GoogleProfileURL   string
	GoogleGmailAPIURL  string
	GoogleScopes       []string
	StateSigningKey    []byte
	TokenEncryptionKey []byte
	TrustKey           []byte
	DatabaseURL        string
}

type LookupEnv func(string) (string, bool)
type ReadFile func(string) ([]byte, error)

func Load() (Config, error) { return LoadFrom(os.LookupEnv, os.ReadFile) }

func LoadFrom(lookup LookupEnv, readFile ReadFile) (Config, error) {
	environment := strings.ToLower(valueOr(lookup, "ZIGGY_CONNECTORS_ENV", "development"))
	if environment != "development" && environment != "test" && environment != "staging" && environment != "production" {
		return Config{}, errors.New("ZIGGY_CONNECTORS_ENV must be development, test, staging, or production")
	}
	listenAddr := valueOr(lookup, "ZIGGY_CONNECTORS_LISTEN_ADDR", defaultListenAddr)
	host, _, err := net.SplitHostPort(listenAddr)
	if err != nil {
		return Config{}, fmt.Errorf("ZIGGY_CONNECTORS_LISTEN_ADDR: %w", err)
	}
	if environment == "production" && !isLoopbackHost(host) {
		return Config{}, errors.New("ZIGGY_CONNECTORS_LISTEN_ADDR must bind to loopback in production")
	}
	clientID, err := required(lookup, "ZIGGY_CONNECTORS_GOOGLE_CLIENT_ID")
	if err != nil {
		return Config{}, err
	}
	redirect, err := required(lookup, "ZIGGY_CONNECTORS_GOOGLE_REDIRECT_URI")
	if err != nil {
		return Config{}, err
	}
	if err := validateRedirect(redirect, environment); err != nil {
		return Config{}, fmt.Errorf("ZIGGY_CONNECTORS_GOOGLE_REDIRECT_URI: %w", err)
	}
	clientSecret, err := secret(lookup, readFile, "ZIGGY_CONNECTORS_GOOGLE_CLIENT_SECRET")
	if err != nil {
		return Config{}, err
	}
	stateKey, err := keyFile(lookup, readFile, "ZIGGY_CONNECTORS_STATE_SIGNING_KEY_FILE", 32)
	if err != nil {
		return Config{}, err
	}
	tokenKey, err := keyFile(lookup, readFile, "ZIGGY_CONNECTORS_TOKEN_ENCRYPTION_KEY_FILE", 32)
	if err != nil {
		return Config{}, err
	}
	trustKey, err := keyFile(lookup, readFile, "ZIGGY_CONNECTORS_TRUST_KEY_FILE", 32)
	if err != nil {
		return Config{}, err
	}
	shutdown := defaultShutdownTimeout
	if value := strings.TrimSpace(valueOr(lookup, "ZIGGY_CONNECTORS_SHUTDOWN_TIMEOUT", "")); value != "" {
		shutdown, err = time.ParseDuration(value)
		if err != nil || shutdown <= 0 {
			return Config{}, errors.New("ZIGGY_CONNECTORS_SHUTDOWN_TIMEOUT must be a positive duration")
		}
	}
	stateTTL := defaultStateTTL
	if value := strings.TrimSpace(valueOr(lookup, "ZIGGY_CONNECTORS_STATE_TTL", "")); value != "" {
		stateTTL, err = time.ParseDuration(value)
		if err != nil || stateTTL <= 0 {
			return Config{}, errors.New("ZIGGY_CONNECTORS_STATE_TTL must be a positive duration")
		}
	}
	databaseURL, err := secretOptional(lookup, readFile, "ZIGGY_CONNECTORS_DATABASE_URL")
	if err != nil {
		return Config{}, err
	}
	if environment == "production" && databaseURL == "" {
		return Config{}, errors.New("ZIGGY_CONNECTORS_DATABASE_URL_FILE is required in production")
	}
	return Config{
		Environment:        environment,
		Version:            valueOr(lookup, "ZIGGY_CONNECTORS_VERSION", "dev"),
		ListenAddr:         listenAddr,
		ShutdownTimeout:    shutdown,
		StateTTL:           stateTTL,
		GoogleClientID:     clientID,
		GoogleClientSecret: clientSecret,
		GoogleRedirectURI:  redirect,
		GoogleAuthURL:      valueOr(lookup, "ZIGGY_CONNECTORS_GOOGLE_AUTH_URL", defaultGoogleAuthURL),
		GoogleTokenURL:     valueOr(lookup, "ZIGGY_CONNECTORS_GOOGLE_TOKEN_URL", defaultGoogleTokenURL),
		GoogleUserInfoURL:  valueOr(lookup, "ZIGGY_CONNECTORS_GOOGLE_USERINFO_URL", defaultGoogleUserInfo),
		GoogleProfileURL:   valueOr(lookup, "ZIGGY_CONNECTORS_GOOGLE_PROFILE_URL", defaultGoogleProfile),
		GoogleGmailAPIURL:  valueOr(lookup, "ZIGGY_CONNECTORS_GOOGLE_GMAIL_URL", defaultGoogleGmailAPI),
		GoogleScopes:       []string{"openid", "email", "https://www.googleapis.com/auth/gmail.readonly"},
		StateSigningKey:    stateKey,
		TokenEncryptionKey: tokenKey,
		TrustKey:           trustKey,
		DatabaseURL:        databaseURL,
	}, nil
}

func isLoopbackHost(host string) bool {
	if strings.EqualFold(strings.TrimSpace(host), "localhost") {
		return true
	}
	address := net.ParseIP(strings.TrimSpace(host))
	return address != nil && address.IsLoopback()
}

func validateRedirect(raw, environment string) error {
	u, err := url.Parse(raw)
	if err != nil || u.Scheme == "" || u.Host == "" || u.Path == "" || u.RawQuery != "" || u.Fragment != "" {
		return errors.New("must be an absolute URI without query or fragment")
	}
	if environment == "production" && u.Scheme != "https" {
		return errors.New("must use https in production")
	}
	return nil
}

func secret(lookup LookupEnv, readFile ReadFile, envName string) (string, error) {
	return secretOptional(lookup, readFile, envName)
}

func secretOptional(lookup LookupEnv, readFile ReadFile, envName string) (string, error) {
	inlineName := envName
	fileName := envName + "_FILE"
	inline := strings.TrimSpace(valueOr(lookup, inlineName, ""))
	file := strings.TrimSpace(valueOr(lookup, fileName, ""))
	if inline != "" && file != "" {
		return "", fmt.Errorf("set only one of %s or %s", inlineName, fileName)
	}
	if inline != "" {
		return "", fmt.Errorf("%s is not supported; use %s", inlineName, fileName)
	}
	if file == "" {
		if envName == "ZIGGY_CONNECTORS_DATABASE_URL" {
			return "", nil
		}
		return "", fmt.Errorf("%s_FILE is required", envName)
	}
	contents, err := readFile(file)
	if err != nil {
		return "", fmt.Errorf("%s_FILE: %w", envName, err)
	}
	value := strings.TrimSpace(string(contents))
	if value == "" {
		return "", fmt.Errorf("%s_FILE is empty", envName)
	}
	return value, nil
}

func keyFile(lookup LookupEnv, readFile ReadFile, name string, minimum int) ([]byte, error) {
	filename := strings.TrimSpace(valueOr(lookup, name, ""))
	if filename == "" {
		return nil, fmt.Errorf("%s is required", name)
	}
	value, err := readFile(filename)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", name, err)
	}
	value = decodeKey(value)
	if len(value) < minimum {
		return nil, fmt.Errorf("%s must contain at least %d bytes", name, minimum)
	}
	return value, nil
}

func decodeKey(value []byte) []byte {
	trimmed := strings.TrimSpace(string(value))
	for _, decode := range []func(string) ([]byte, error){
		base64.StdEncoding.DecodeString,
		base64.RawStdEncoding.DecodeString,
		hex.DecodeString,
	} {
		if decoded, err := decode(trimmed); err == nil && len(decoded) >= 32 {
			return decoded
		}
	}
	return value
}

func required(lookup LookupEnv, name string) (string, error) {
	value := strings.TrimSpace(valueOr(lookup, name, ""))
	if value == "" {
		return "", fmt.Errorf("%s is required", name)
	}
	return value, nil
}

func valueOr(lookup LookupEnv, name, fallback string) string {
	value, ok := lookup(name)
	if !ok {
		return fallback
	}
	return value
}
