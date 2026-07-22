package provider

import "context"

type Token struct {
	RefreshToken string
	AccessToken  string
	Expiry       int64
	Scopes       []string
}

type Profile struct {
	Subject   string
	Email     string
	Provider  string
	ProfileID string
}

type Gmail interface {
	AuthorizationURL(string, string, string, []string) (string, error)
	ExchangeCode(context.Context, string, string, string) (Token, error)
	ValidateProfile(context.Context, string) (Profile, error)
}
