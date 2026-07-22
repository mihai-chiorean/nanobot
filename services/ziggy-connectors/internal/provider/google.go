package provider

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

type GoogleConfig struct {
	ClientID     string
	ClientSecret string
	AuthURL      string
	TokenURL     string
	UserInfoURL  string
	ProfileURL   string
	HTTPClient   *http.Client
}

type Google struct {
	clientID     string
	clientSecret string
	authURL      string
	tokenURL     string
	userInfoURL  string
	profileURL   string
	client       *http.Client
}

func NewGoogle(config GoogleConfig) *Google {
	client := config.HTTPClient
	if client == nil {
		transport := http.DefaultTransport.(*http.Transport).Clone()
		transport.MaxIdleConns = 16
		transport.MaxIdleConnsPerHost = 8
		transport.IdleConnTimeout = 90 * time.Second
		client = &http.Client{Timeout: 10 * time.Second, Transport: transport}
	}
	return &Google{clientID: config.ClientID, clientSecret: config.ClientSecret, authURL: config.AuthURL, tokenURL: config.TokenURL, userInfoURL: config.UserInfoURL, profileURL: config.ProfileURL, client: client}
}

func (g *Google) AuthorizationURL(redirectURI, state, verifier string, scopes []string) (string, error) {
	u, err := url.Parse(g.authURL)
	if err != nil || u.Scheme != "https" || u.Host == "" {
		return "", errors.New("Google authorization endpoint must be an https URL")
	}
	query := u.Query()
	query.Set("client_id", g.clientID)
	query.Set("redirect_uri", redirectURI)
	query.Set("response_type", "code")
	query.Set("scope", strings.Join(scopes, " "))
	query.Set("state", state)
	query.Set("access_type", "offline")
	query.Set("include_granted_scopes", "true")
	query.Set("prompt", "consent select_account")
	query.Set("code_challenge", codeChallenge(verifier))
	query.Set("code_challenge_method", "S256")
	u.RawQuery = query.Encode()
	return u.String(), nil
}

func (g *Google) ExchangeCode(ctx context.Context, code, verifier, redirectURI string) (Token, error) {
	form := url.Values{"code": {code}, "client_id": {g.clientID}, "client_secret": {g.clientSecret}, "redirect_uri": {redirectURI}, "grant_type": {"authorization_code"}, "code_verifier": {verifier}}
	var response struct {
		AccessToken  string `json:"access_token"`
		RefreshToken string `json:"refresh_token"`
		ExpiresIn    int64  `json:"expires_in"`
		Scope        string `json:"scope"`
		Error        string `json:"error"`
		ErrorDesc    string `json:"error_description"`
	}
	if err := g.postForm(ctx, g.tokenURL, form, &response); err != nil {
		return Token{}, err
	}
	if response.Error != "" {
		return Token{}, fmt.Errorf("Google token exchange failed: %s", response.Error)
	}
	if response.RefreshToken == "" || response.AccessToken == "" {
		return Token{}, errors.New("Google token exchange did not return required tokens")
	}
	return Token{AccessToken: response.AccessToken, RefreshToken: response.RefreshToken, Expiry: response.ExpiresIn, Scopes: strings.Fields(response.Scope)}, nil
}

func (g *Google) ValidateProfile(ctx context.Context, accessToken string) (Profile, error) {
	var identity struct {
		Subject string `json:"sub"`
		Email   string `json:"email"`
	}
	if err := g.getBearer(ctx, g.userInfoURL, accessToken, &identity); err != nil {
		return Profile{}, err
	}
	if identity.Subject == "" || identity.Email == "" {
		return Profile{}, errors.New("Google identity profile is incomplete")
	}
	var gmail struct {
		EmailAddress string `json:"emailAddress"`
	}
	if err := g.getBearer(ctx, g.profileURL, accessToken, &gmail); err != nil {
		return Profile{}, err
	}
	if !strings.EqualFold(identity.Email, gmail.EmailAddress) {
		return Profile{}, errors.New("Google identity and Gmail profile do not match")
	}
	return Profile{Subject: identity.Subject, Email: identity.Email, Provider: "google", ProfileID: gmail.EmailAddress}, nil
}

func (g *Google) postForm(ctx context.Context, endpoint string, form url.Values, output any) error {
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, strings.NewReader(form.Encode()))
	if err != nil {
		return errors.New("create Google token request")
	}
	request.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	return g.doJSON(request, output)
}

func (g *Google) getBearer(ctx context.Context, endpoint, token string, output any) error {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return errors.New("create Google profile request")
	}
	request.Header.Set("Authorization", "Bearer "+token)
	return g.doJSON(request, output)
}

func (g *Google) doJSON(request *http.Request, output any) error {
	response, err := g.client.Do(request)
	if err != nil {
		return errors.New("Google request failed")
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return fmt.Errorf("Google request returned status %d", response.StatusCode)
	}
	decoder := json.NewDecoder(io.LimitReader(response.Body, 1<<20))
	if err := decoder.Decode(output); err != nil {
		return errors.New("invalid Google response")
	}
	return nil
}

func codeChallenge(verifier string) string {
	// RFC 7636 S256: base64url(sha256(verifier)).
	return base64URLSHA256(verifier)
}
