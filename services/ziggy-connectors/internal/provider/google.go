package provider

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
	"unicode/utf8"

	"golang.org/x/net/html"
	"golang.org/x/sync/errgroup"
)

type GoogleConfig struct {
	ClientID     string
	ClientSecret string
	AuthURL      string
	TokenURL     string
	UserInfoURL  string
	ProfileURL   string
	GmailAPIURL  string
	HTTPClient   *http.Client
}

type Google struct {
	clientID     string
	clientSecret string
	authURL      string
	tokenURL     string
	userInfoURL  string
	profileURL   string
	gmailAPIURL  string
	client       *http.Client
}

const maximumGoogleResponseBytes = 8 << 20

func NewGoogle(config GoogleConfig) *Google {
	client := config.HTTPClient
	if client == nil {
		transport := http.DefaultTransport.(*http.Transport).Clone()
		transport.MaxIdleConns = 16
		transport.MaxIdleConnsPerHost = 8
		transport.IdleConnTimeout = 90 * time.Second
		client = &http.Client{Timeout: 10 * time.Second, Transport: transport}
	}
	return &Google{
		clientID:     config.ClientID,
		clientSecret: config.ClientSecret,
		authURL:      config.AuthURL,
		tokenURL:     config.TokenURL,
		userInfoURL:  config.UserInfoURL,
		profileURL:   config.ProfileURL,
		gmailAPIURL:  strings.TrimRight(config.GmailAPIURL, "/"),
		client:       client,
	}
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

func (g *Google) RefreshAccessToken(ctx context.Context, refreshToken string) (Token, error) {
	if strings.TrimSpace(refreshToken) == "" {
		return Token{}, errors.New("Google refresh token is missing")
	}
	form := url.Values{
		"client_id":     {g.clientID},
		"client_secret": {g.clientSecret},
		"refresh_token": {refreshToken},
		"grant_type":    {"refresh_token"},
	}
	var response struct {
		AccessToken string `json:"access_token"`
		ExpiresIn   int64  `json:"expires_in"`
		Scope       string `json:"scope"`
		Error       string `json:"error"`
	}
	if err := g.postForm(ctx, g.tokenURL, form, &response); err != nil {
		return Token{}, err
	}
	if response.Error != "" || response.AccessToken == "" {
		return Token{}, errors.New("Google access token refresh failed")
	}
	return Token{
		AccessToken: response.AccessToken,
		Expiry:      response.ExpiresIn,
		Scopes:      strings.Fields(response.Scope),
	}, nil
}

func (g *Google) SearchMessages(ctx context.Context, accessToken, query string, maximum int) ([]MessageSummary, error) {
	if maximum < 1 || maximum > 20 {
		return nil, errors.New("Gmail search maximum must be between 1 and 20")
	}
	endpoint, err := url.Parse(g.gmailAPIURL + "/users/me/messages")
	if err != nil || endpoint.Scheme != "https" || endpoint.Host == "" {
		return nil, errors.New("Gmail API endpoint is invalid")
	}
	values := endpoint.Query()
	values.Set("maxResults", fmt.Sprint(maximum))
	if query = strings.TrimSpace(query); query != "" {
		values.Set("q", query)
	}
	endpoint.RawQuery = values.Encode()
	var page struct {
		Messages []struct {
			ID       string `json:"id"`
			ThreadID string `json:"threadId"`
		} `json:"messages"`
	}
	if err := g.getBearer(ctx, endpoint.String(), accessToken, &page); err != nil {
		return nil, err
	}
	summaries := make([]MessageSummary, len(page.Messages))
	group, groupContext := errgroup.WithContext(ctx)
	group.SetLimit(4)
	for index, candidate := range page.Messages {
		index, candidate := index, candidate
		group.Go(func() error {
			summary, err := g.messageSummary(groupContext, accessToken, candidate.ID)
			if err != nil {
				return nil
			}
			if summary.ThreadID == "" {
				summary.ThreadID = candidate.ThreadID
			}
			summaries[index] = summary
			return nil
		})
	}
	if err := group.Wait(); err != nil {
		return nil, err
	}
	result := make([]MessageSummary, 0, len(summaries))
	for _, summary := range summaries {
		if summary.ID != "" {
			result = append(result, summary)
		}
	}
	if len(result) == 0 && len(page.Messages) > 0 {
		return nil, errors.New("Gmail message metadata is unavailable")
	}
	return result, nil
}

func (g *Google) GetMessage(ctx context.Context, accessToken, messageID string) (Message, error) {
	if !validMessageID(messageID) {
		return Message{}, errors.New("Gmail message ID is invalid")
	}
	endpoint, err := url.Parse(g.gmailAPIURL + "/users/me/messages/" + url.PathEscape(messageID))
	if err != nil || endpoint.Scheme != "https" || endpoint.Host == "" {
		return Message{}, errors.New("Gmail API endpoint is invalid")
	}
	values := endpoint.Query()
	values.Set("format", "full")
	endpoint.RawQuery = values.Encode()
	var document gmailMessage
	if err := g.getBearer(ctx, endpoint.String(), accessToken, &document); err != nil {
		return Message{}, err
	}
	return Message{
		MessageSummary: summaryFromMessage(document),
		LabelIDs:       append([]string(nil), document.LabelIDs...),
		Body:           messageBody(document.Payload),
	}, nil
}

func (g *Google) messageSummary(ctx context.Context, accessToken, messageID string) (MessageSummary, error) {
	if !validMessageID(messageID) {
		return MessageSummary{}, errors.New("Gmail message ID is invalid")
	}
	endpoint, err := url.Parse(g.gmailAPIURL + "/users/me/messages/" + url.PathEscape(messageID))
	if err != nil {
		return MessageSummary{}, errors.New("Gmail API endpoint is invalid")
	}
	values := endpoint.Query()
	values.Set("format", "metadata")
	for _, name := range []string{"Subject", "From", "To", "Date"} {
		values.Add("metadataHeaders", name)
	}
	endpoint.RawQuery = values.Encode()
	var document gmailMessage
	if err := g.getBearer(ctx, endpoint.String(), accessToken, &document); err != nil {
		return MessageSummary{}, err
	}
	return summaryFromMessage(document), nil
}

type gmailMessage struct {
	ID       string    `json:"id"`
	ThreadID string    `json:"threadId"`
	LabelIDs []string  `json:"labelIds"`
	Snippet  string    `json:"snippet"`
	Payload  gmailPart `json:"payload"`
}

type gmailPart struct {
	MimeType string `json:"mimeType"`
	Headers  []struct {
		Name  string `json:"name"`
		Value string `json:"value"`
	} `json:"headers"`
	Body struct {
		Data string `json:"data"`
	} `json:"body"`
	Parts []gmailPart `json:"parts"`
}

func summaryFromMessage(document gmailMessage) MessageSummary {
	headers := make(map[string]string, len(document.Payload.Headers))
	for _, header := range document.Payload.Headers {
		headers[strings.ToLower(header.Name)] = header.Value
	}
	return MessageSummary{
		ID:       document.ID,
		ThreadID: document.ThreadID,
		Subject:  truncateText(headers["subject"], 4<<10),
		From:     truncateText(headers["from"], 4<<10),
		To:       truncateText(headers["to"], 4<<10),
		Date:     truncateText(headers["date"], 1<<10),
		Snippet:  truncateText(document.Snippet, 8<<10),
	}
}

func messageBody(root gmailPart) string {
	var plain []string
	var rich []string
	var visit func(gmailPart)
	visit = func(part gmailPart) {
		if decoded := decodeBody(part.Body.Data); decoded != "" {
			switch strings.ToLower(part.MimeType) {
			case "text/plain":
				plain = append(plain, decoded)
			case "text/html":
				rich = append(rich, htmlText(decoded))
			}
		}
		for _, child := range part.Parts {
			visit(child)
		}
	}
	visit(root)
	body := strings.Join(plain, "\n\n")
	if strings.TrimSpace(body) == "" {
		body = strings.Join(rich, "\n\n")
	}
	return truncateText(strings.TrimSpace(body), 64<<10)
}

func decodeBody(value string) string {
	if strings.TrimSpace(value) == "" {
		return ""
	}
	decoded, err := base64.RawURLEncoding.DecodeString(strings.TrimRight(value, "="))
	if err != nil {
		return ""
	}
	return string(decoded)
}

func htmlText(document string) string {
	root, err := html.Parse(strings.NewReader(document))
	if err != nil {
		return ""
	}
	var output strings.Builder
	var visit func(*html.Node, bool)
	visit = func(node *html.Node, hidden bool) {
		if node.Type == html.ElementNode {
			name := strings.ToLower(node.Data)
			hidden = hidden || name == "script" || name == "style" || name == "noscript"
		}
		if node.Type == html.TextNode && !hidden {
			value := strings.TrimSpace(node.Data)
			if value != "" {
				if output.Len() > 0 {
					output.WriteByte(' ')
				}
				output.WriteString(value)
			}
		}
		for child := node.FirstChild; child != nil; child = child.NextSibling {
			visit(child, hidden)
		}
	}
	visit(root, false)
	return output.String()
}

func truncateText(value string, maximum int) string {
	if len(value) <= maximum {
		return value
	}
	value = value[:maximum]
	for len(value) > 0 && !utf8.ValidString(value) {
		value = value[:len(value)-1]
	}
	return value
}

func validMessageID(value string) bool {
	if len(value) < 1 || len(value) > 128 {
		return false
	}
	for _, character := range value {
		if character >= 'a' && character <= 'z' ||
			character >= 'A' && character <= 'Z' ||
			character >= '0' && character <= '9' ||
			character == '_' || character == '-' {
			continue
		}
		return false
	}
	return true
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
	decoder := json.NewDecoder(io.LimitReader(response.Body, maximumGoogleResponseBytes))
	if err := decoder.Decode(output); err != nil {
		return errors.New("invalid Google response")
	}
	return nil
}

func codeChallenge(verifier string) string {
	// RFC 7636 S256: base64url(sha256(verifier)).
	return base64URLSHA256(verifier)
}
