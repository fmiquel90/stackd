// Package client is a thin HTTP client for the Stackd human API (/api/v1).
//
// It only speaks the public REST surface (docs/api.md): no shared code with the API server, so
// the CLI stays an ordinary external client that works against any compatible instance.
package client

import (
	"bytes"
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

// Client calls one Stackd instance with one bearer token.
type Client struct {
	base      *url.URL // instance root, e.g. https://stackd.example.com
	token     string
	userAgent string
	http      *http.Client
}

// New validates the instance URL and returns a client. A trailing "/api/v1" in rawURL is
// accepted and ignored, so both forms of the URL work.
func New(rawURL, token, userAgent string) (*Client, error) {
	if rawURL == "" {
		return nil, errors.New("no Stackd URL: set STACKD_URL or pass --url")
	}
	u, err := url.Parse(strings.TrimRight(rawURL, "/"))
	if err != nil || u.Scheme == "" || u.Host == "" {
		return nil, fmt.Errorf("invalid Stackd URL %q: expected e.g. https://stackd.example.com", rawURL)
	}
	if u.Scheme != "http" && u.Scheme != "https" {
		return nil, fmt.Errorf("invalid Stackd URL %q: scheme must be http or https", rawURL)
	}
	u.Path = strings.TrimSuffix(u.Path, "/api/v1")
	return &Client{
		base:      u,
		token:     token,
		userAgent: userAgent,
		http:      &http.Client{Timeout: 60 * time.Second},
	}, nil
}

// Problem is an RFC 9457 problem+json error returned by the API (SPECS §1).
type Problem struct {
	Status int              `json:"status"`
	Title  string           `json:"title"`
	Detail string           `json:"detail,omitempty"`
	Type   string           `json:"type,omitempty"`
	Errors []map[string]any `json:"errors,omitempty"` // request validation details (422)
}

func (p *Problem) Error() string {
	msg := fmt.Sprintf("%d %s", p.Status, p.Title)
	if p.Detail != "" {
		msg += ": " + p.Detail
	}
	for _, e := range p.Errors {
		loc, _ := json.Marshal(e["loc"])
		msg += fmt.Sprintf("\n  - %s: %v", loc, e["msg"])
	}
	return msg
}

// IsStatus reports whether err is an API problem with that HTTP status.
func IsStatus(err error, status int) bool {
	var p *Problem
	return errors.As(err, &p) && p.Status == status
}

// Do sends a request to /api/v1+path. A non-nil body is sent as JSON. On a 2xx it returns the
// raw response body (empty on 204); otherwise a *Problem.
func (c *Client) Do(ctx context.Context, method, path string, query url.Values, body any) ([]byte, error) {
	u := *c.base
	u.Path = c.base.Path + "/api/v1" + path
	u.RawQuery = query.Encode()

	var reader io.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			return nil, fmt.Errorf("encode request: %w", err)
		}
		reader = bytes.NewReader(raw)
	}
	req, err := http.NewRequestWithContext(ctx, method, u.String(), reader)
	if err != nil {
		return nil, err
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	req.Header.Set("Accept", "application/json, application/problem+json")
	req.Header.Set("User-Agent", c.userAgent)
	if c.token != "" {
		req.Header.Set("Authorization", "Bearer "+c.token)
	}

	resp, err := c.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("%s %s: %w", method, path, err)
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, 64<<20))
	if err != nil {
		return nil, fmt.Errorf("%s %s: read response: %w", method, path, err)
	}
	if resp.StatusCode >= 200 && resp.StatusCode < 300 {
		return data, nil
	}
	p := &Problem{}
	if json.Unmarshal(data, p) != nil || p.Title == "" {
		p = &Problem{Title: http.StatusText(resp.StatusCode), Detail: strings.TrimSpace(string(data))}
	}
	p.Status = resp.StatusCode
	if p.Status == http.StatusUnauthorized && c.token == "" {
		p.Detail = "no token: set STACKD_TOKEN or pass --token"
	}
	return nil, p
}

// JSON sends a request and decodes a JSON response into out (skipped when out is nil). It
// returns the raw body as well, so callers can print it verbatim in --output json mode.
func (c *Client) JSON(ctx context.Context, method, path string, query url.Values, body, out any) ([]byte, error) {
	data, err := c.Do(ctx, method, path, query, body)
	if err != nil {
		return nil, err
	}
	if out != nil && len(data) > 0 {
		if err := json.Unmarshal(data, out); err != nil {
			return nil, fmt.Errorf("%s %s: decode response: %w", method, path, err)
		}
	}
	return data, nil
}
