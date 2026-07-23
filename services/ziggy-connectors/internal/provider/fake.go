package provider

import (
	"context"
	"errors"
	"net/url"
)

type Fake struct {
	Token    Token
	Profile  Profile
	Messages []MessageSummary
	Message  Message
	Err      error
}

func (f Fake) AuthorizationURL(redirectURI, state, verifier string, scopes []string) (string, error) {
	if f.Err != nil {
		return "", f.Err
	}
	values := url.Values{"redirect_uri": {redirectURI}, "state": {state}, "code_challenge": {codeChallenge(verifier)}, "code_challenge_method": {"S256"}, "scope": {scopes[0]}}
	return "https://fake.google.test/auth?" + values.Encode(), nil
}

func (f Fake) ExchangeCode(context.Context, string, string, string) (Token, error) {
	if f.Err != nil {
		return Token{}, f.Err
	}
	if f.Token.RefreshToken == "" {
		return Token{}, errors.New("fake token has no refresh token")
	}
	return f.Token, nil
}

func (f Fake) ValidateProfile(context.Context, string) (Profile, error) {
	if f.Err != nil {
		return Profile{}, f.Err
	}
	if f.Profile.Subject == "" || f.Profile.Email == "" {
		return Profile{}, errors.New("fake profile is incomplete")
	}
	return f.Profile, nil
}

func (f Fake) RefreshAccessToken(context.Context, string) (Token, error) {
	if f.Err != nil {
		return Token{}, f.Err
	}
	token := f.Token
	if token.AccessToken == "" {
		token.AccessToken = "fake-access-token"
	}
	return token, nil
}

func (f Fake) SearchMessages(context.Context, string, string, int) ([]MessageSummary, error) {
	if f.Err != nil {
		return nil, f.Err
	}
	return append([]MessageSummary(nil), f.Messages...), nil
}

func (f Fake) GetMessage(context.Context, string, string) (Message, error) {
	if f.Err != nil {
		return Message{}, f.Err
	}
	return f.Message, nil
}

var _ Gmail = Fake{}
