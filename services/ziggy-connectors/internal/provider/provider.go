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

type MessageSummary struct {
	ID       string `json:"id"`
	ThreadID string `json:"thread_id"`
	Subject  string `json:"subject,omitempty"`
	From     string `json:"from,omitempty"`
	To       string `json:"to,omitempty"`
	Date     string `json:"date,omitempty"`
	Snippet  string `json:"snippet,omitempty"`
}

type Message struct {
	MessageSummary
	LabelIDs []string `json:"label_ids,omitempty"`
	Body     string   `json:"body,omitempty"`
}

type Gmail interface {
	AuthorizationURL(string, string, string, []string) (string, error)
	ExchangeCode(context.Context, string, string, string) (Token, error)
	ValidateProfile(context.Context, string) (Profile, error)
	RefreshAccessToken(context.Context, string) (Token, error)
	SearchMessages(context.Context, string, string, int) ([]MessageSummary, error)
	GetMessage(context.Context, string, string) (Message, error)
}
