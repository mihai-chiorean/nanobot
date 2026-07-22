package provider

import (
	"context"
	"errors"
	"net/url"
)

type Fake struct {
	Token   Token
	Profile Profile
	Err     error
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

var _ Gmail = Fake{}
