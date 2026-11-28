package cmd

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

const (
	spaceA = "01900000-0000-7000-8000-00000000000a"
	spaceB = "01900000-0000-7000-8000-00000000000b"
	stack1 = "01900000-0000-7000-8000-000000000001"
	stack2 = "01900000-0000-7000-8000-000000000002"
	env1   = "01900000-0000-7000-8000-0000000000e1"
	run1   = "01900000-0000-7000-8000-0000000000f1"
	token  = "stkd_test-token"
)

// fakeAPI is a minimal stand-in for the Stackd API: it records requests and answers from a
// route table. It checks the bearer token on every call, like the real API.
type fakeAPI struct {
	t      *testing.T
	mu     sync.Mutex
	routes map[string]func(w http.ResponseWriter, r *http.Request, body []byte)
	calls  []call
}

type call struct {
	method, path, query string
	body                map[string]any
	userAgent           string
}

func newFakeAPI(t *testing.T) (*fakeAPI, *httptest.Server) {
	f := &fakeAPI{t: t, routes: map[string]func(http.ResponseWriter, *http.Request, []byte){}}
	srv := httptest.NewServer(http.HandlerFunc(f.serve))
	t.Cleanup(srv.Close)
	// Common fixtures: two spaces, a stack "network" in each, one env in space A's.
	f.json("GET /api/v1/spaces", 200, []map[string]any{
		{"id": spaceA, "name": "platform", "description": nil, "created_at": "2026-01-01T00:00:00Z"},
		{"id": spaceB, "name": "apps", "description": "Apps", "created_at": "2026-01-01T00:00:00Z"},
	})
	f.json("GET /api/v1/stacks", 200, []map[string]any{
		{"id": stack1, "space_id": spaceA, "name": "network", "repo_url": "file:///r", "tool": "opentofu", "tool_version": "1.10", "project_root": ".", "extra_field": "kept"},
		{"id": stack2, "space_id": spaceB, "name": "network", "repo_url": "file:///r", "tool": "opentofu", "tool_version": "1.10", "project_root": "."},
	})
	f.json("GET /api/v1/stacks/"+stack1+"/environments", 200, []map[string]any{
		{"id": env1, "stack_id": stack1, "name": "dev", "tier": "dev", "branch": "main"},
	})
	return f, srv
}

func (f *fakeAPI) json(route string, status int, v any) {
	f.routes[route] = func(w http.ResponseWriter, _ *http.Request, _ []byte) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_ = json.NewEncoder(w).Encode(v)
	}
}

func (f *fakeAPI) problem(route string, status int, title, detail string) {
	f.routes[route] = func(w http.ResponseWriter, _ *http.Request, _ []byte) {
		w.Header().Set("Content-Type", "application/problem+json")
		w.WriteHeader(status)
		_ = json.NewEncoder(w).Encode(map[string]any{"type": "about:blank", "title": title, "status": status, "detail": detail})
	}
}

func (f *fakeAPI) serve(w http.ResponseWriter, r *http.Request) {
	body, _ := io.ReadAll(r.Body)
	c := call{method: r.Method, path: r.URL.Path, query: r.URL.RawQuery, userAgent: r.UserAgent()}
	if len(body) > 0 {
		_ = json.Unmarshal(body, &c.body)
	}
	f.mu.Lock()
	f.calls = append(f.calls, c)
	f.mu.Unlock()
	if r.Header.Get("Authorization") != "Bearer "+token {
		w.WriteHeader(401)
		_, _ = w.Write([]byte(`{"title":"Unauthorized","status":401}`))
		return
	}
	h, ok := f.routes[r.Method+" "+r.URL.Path]
	if !ok {
		w.WriteHeader(404)
		_, _ = w.Write([]byte(`{"title":"Not Found","status":404}`))
		return
	}
	h(w, r, body)
}

func (f *fakeAPI) last(method, path string) *call {
	f.mu.Lock()
	defer f.mu.Unlock()
	for i := len(f.calls) - 1; i >= 0; i-- {
		if f.calls[i].method == method && f.calls[i].path == path {
			return &f.calls[i]
		}
	}
	return nil
}

type result struct {
	out, errOut string
	err         error
}

func runCLI(t *testing.T, srvURL, stdin string, args ...string) result {
	t.Helper()
	var out, errOut bytes.Buffer
	a := &app{in: strings.NewReader(stdin), out: &out, errOut: &errOut, pollInterval: time.Millisecond}
	root := newRoot(a)
	root.SetArgs(append([]string{"--url", srvURL, "--token", token}, args...))
	err := root.ExecuteContext(context.Background())
	return result{out.String(), errOut.String(), err}
}

func TestSpacesListAndAuth(t *testing.T) {
	f, srv := newFakeAPI(t)
	// A URL given with the /api/v1 suffix works the same.
	r := runCLI(t, srv.URL+"/api/v1/", "", "spaces", "list")
	if r.err != nil {
		t.Fatal(r.err)
	}
	if !strings.Contains(r.out, "platform") || !strings.Contains(r.out, "apps") {
		t.Fatalf("unexpected output:\n%s", r.out)
	}
	if c := f.last("GET", "/api/v1/spaces"); c == nil || !strings.HasPrefix(c.userAgent, "stackd-cli/") {
		t.Fatalf("missing call or user agent: %+v", c)
	}
}

func TestMissingTokenIsExplained(t *testing.T) {
	_, srv := newFakeAPI(t)
	t.Setenv("STACKD_TOKEN", "")
	var out, errOut bytes.Buffer
	root := newRoot(&app{in: strings.NewReader(""), out: &out, errOut: &errOut})
	root.SetArgs([]string{"--url", srv.URL, "spaces", "list"})
	err := root.Execute()
	if err == nil || !strings.Contains(err.Error(), "STACKD_TOKEN") {
		t.Fatalf("want a hint about STACKD_TOKEN, got %v", err)
	}
}

func TestHelpNeverPrintsTheToken(t *testing.T) {
	t.Setenv("STACKD_TOKEN", "stkd_super-secret")
	var out bytes.Buffer
	root := NewRootCmd(strings.NewReader(""), &out, &out)
	root.SetArgs([]string{"--help"})
	if err := root.Execute(); err != nil {
		t.Fatal(err)
	}
	if strings.Contains(out.String(), "super-secret") {
		t.Fatal("--help printed the token")
	}
}

func TestSpacesCreateForbiddenHint(t *testing.T) {
	f, srv := newFakeAPI(t)
	f.problem("POST /api/v1/spaces", 403, "Forbidden", "Requires role admin.")
	r := runCLI(t, srv.URL, "", "spaces", "create", "new-space")
	if r.err == nil || !strings.Contains(r.err.Error(), "403 Forbidden") ||
		!strings.Contains(r.err.Error(), "service account cannot") {
		t.Fatalf("unexpected error: %v", r.err)
	}
}

func TestStacksCreateResolvesSpaceAndReadsSecretFromStdin(t *testing.T) {
	f, srv := newFakeAPI(t)
	f.json("POST /api/v1/stacks", 201, map[string]any{"id": stack1, "space_id": spaceA, "name": "dns"})
	r := runCLI(t, srv.URL, "ghp_secret\n", "stacks", "create", "dns", "--space", "platform",
		"--repo-url", "https://example.com/r.git", "--tool-version", "1.10", "--label", "team=net",
		"--repo-auth-kind", "token", "--repo-secret-stdin")
	if r.err != nil {
		t.Fatal(r.err)
	}
	c := f.last("POST", "/api/v1/stacks")
	want := map[string]any{
		"name": "dns", "space_id": spaceA, "repo_url": "https://example.com/r.git",
		"tool_version": "1.10", "labels": map[string]any{"team": "net"},
		"repo_auth_kind": "token", "repo_secret": "ghp_secret",
	}
	if g, w := mustJSON(c.body), mustJSON(want); g != w {
		t.Fatalf("body\n got %s\nwant %s", g, w)
	}
	if strings.Contains(r.out, "ghp_secret") {
		t.Fatal("secret echoed")
	}
}

func TestStacksListFiltersBySpaceKeepingRawJSON(t *testing.T) {
	_, srv := newFakeAPI(t)
	r := runCLI(t, srv.URL, "", "-o", "json", "stacks", "list", "--space", "apps")
	if r.err != nil {
		t.Fatal(r.err)
	}
	var got []map[string]any
	if err := json.Unmarshal([]byte(r.out), &got); err != nil {
		t.Fatal(err)
	}
	if len(got) != 1 || got[0]["id"] != stack2 {
		t.Fatalf("unexpected: %s", r.out)
	}
	// Unfiltered: the API's fields are passed through, including ones the CLI does not model.
	r = runCLI(t, srv.URL, "", "-o", "json", "stacks", "list")
	if !strings.Contains(r.out, "extra_field") {
		t.Fatalf("raw JSON not passed through: %s", r.out)
	}
}

func TestAmbiguousStackName(t *testing.T) {
	_, srv := newFakeAPI(t)
	r := runCLI(t, srv.URL, "", "envs", "list", "network")
	if r.err == nil || !strings.Contains(r.err.Error(), "--space") {
		t.Fatalf("want an ambiguity error, got %v", r.err)
	}
	r = runCLI(t, srv.URL, "", "envs", "list", "network", "--space", "platform")
	if r.err != nil || !strings.Contains(r.out, "dev") {
		t.Fatalf("err=%v out=%s", r.err, r.out)
	}
}

func TestEnvsCreateSendsOnlyGivenFlags(t *testing.T) {
	f, srv := newFakeAPI(t)
	f.json("POST /api/v1/stacks/"+stack1+"/environments", 201, map[string]any{"id": env1, "name": "prod"})
	f.json("GET /api/v1/stacks/"+stack1, 200, map[string]any{"id": stack1, "space_id": spaceA, "name": "network"})
	r := runCLI(t, srv.URL, "", "envs", "create", stack1, "prod", "--tier", "prod", "--branch", "main", "--protected")
	if r.err != nil {
		t.Fatal(r.err)
	}
	c := f.last("POST", "/api/v1/stacks/"+stack1+"/environments")
	want := map[string]any{"name": "prod", "tier": "prod", "branch": "main", "protected": true}
	if g, w := mustJSON(c.body), mustJSON(want); g != w {
		t.Fatalf("body\n got %s\nwant %s", g, w)
	}
}

func TestDeleteNeedsYes(t *testing.T) {
	f, srv := newFakeAPI(t)
	r := runCLI(t, srv.URL, "", "stacks", "delete", "network")
	if r.err == nil || !strings.Contains(r.err.Error(), "--yes") {
		t.Fatalf("want --yes refusal, got %v", r.err)
	}
	if len(f.calls) != 0 {
		t.Fatalf("no API call expected, got %d", len(f.calls))
	}
}

// runSequence answers GET /runs/{id} with successive states, then stays on the last one.
func runSequence(f *fakeAPI, states ...string) {
	var mu sync.Mutex
	i := 0
	f.routes["GET /api/v1/runs/"+run1] = func(w http.ResponseWriter, _ *http.Request, _ []byte) {
		mu.Lock()
		s := states[min(i, len(states)-1)]
		i++
		mu.Unlock()
		body := map[string]any{"id": run1, "environment_id": env1, "type": "tracked", "state": s,
			"triggered_by": "manual", "used_mocks": false, "created_at": "2026-01-01T00:00:00Z"}
		if s == "unconfirmed" || s == "finished" {
			body["plan_summary"] = map[string]any{"add": 2, "change": 1, "destroy": 0}
		}
		if s == "failed" {
			body["error"] = "tofu plan exited 1"
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(body)
	}
}

func TestRunsPlanWaitUntilUnconfirmed(t *testing.T) {
	f, srv := newFakeAPI(t)
	f.json("POST /api/v1/environments/"+env1+"/runs", 201, map[string]any{"id": run1, "state": "queued"})
	runSequence(f, "queued", "planning", "unconfirmed")
	f.json("GET /api/v1/runs/"+run1+"/logs", 200, []map[string]any{
		{"phase": "planning", "section": nil, "seq": 1, "lines": []map[string]any{{"t": "x", "msg": "Plan: 2 to add"}}},
	})
	r := runCLI(t, srv.URL, "", "runs", "plan", "network/dev", "--space", "platform", "--wait", "--logs", "--target", "module.vpc")
	if r.err != nil {
		t.Fatal(r.err)
	}
	if strings.Count(r.out, "Plan: 2 to add") != 1 {
		t.Fatalf("log line should print exactly once:\n%s", r.out)
	}
	if !strings.Contains(r.errOut, "unconfirmed") || !strings.Contains(r.errOut, "+2 ~1 -0") {
		t.Fatalf("missing final state:\n%s", r.errOut)
	}
	c := f.last("POST", "/api/v1/environments/"+env1+"/runs")
	if g, w := mustJSON(c.body), mustJSON(map[string]any{"type": "tracked", "targets": []string{"module.vpc"}}); g != w {
		t.Fatalf("body\n got %s\nwant %s", g, w)
	}
}

func TestRunsPlanWaitFailedExitsTwo(t *testing.T) {
	f, srv := newFakeAPI(t)
	f.json("POST /api/v1/environments/"+env1+"/runs", 201, map[string]any{"id": run1, "state": "queued"})
	f.json("GET /api/v1/environments/"+env1, 200, map[string]any{"id": env1, "stack_id": stack1, "name": "dev"})
	runSequence(f, "planning", "failed")
	r := runCLI(t, srv.URL, "", "runs", "plan", env1, "--wait")
	var ee *exitError
	if !errors.As(r.err, &ee) || ee.code != 2 || !strings.Contains(ee.msg, "tofu plan exited 1") {
		t.Fatalf("want exit 2 with the run error, got %v", r.err)
	}
}

func TestConfigImportDryRun(t *testing.T) {
	f, srv := newFakeAPI(t)
	f.json("POST /api/v1/spaces/"+spaceA+"/import", 200, map[string]any{
		"dry_run":         true,
		"summary":         map[string]any{"created": 1, "updated": 0, "unchanged": 3, "skipped": 1},
		"changes":         []map[string]any{{"op": "created", "kind": "stack", "path": "stacks/dns"}},
		"skipped":         []map[string]any{{"kind": "variable", "path": "stacks/dns/variables/token", "reason": "value_required"}},
		"missing_secrets": []any{}, "warnings": []any{}, "worker_pool_tokens": []any{},
	})
	doc := `{"format":"stackd.config","format_version":1,"stacks":[]}`
	r := runCLI(t, srv.URL, doc, "config", "import", "platform", "-", "--dry-run")
	if r.err != nil {
		t.Fatal(r.err)
	}
	c := f.last("POST", "/api/v1/spaces/"+spaceA+"/import")
	if c.query != "dry_run=true" || c.body["format"] != "stackd.config" {
		t.Fatalf("unexpected call: %+v", c)
	}
	for _, want := range []string{"Dry run on space platform", "1 created", "stacks/dns", "value_required"} {
		if !strings.Contains(r.out, want) {
			t.Fatalf("missing %q in:\n%s", want, r.out)
		}
	}
}

func TestValidationProblemIsRendered(t *testing.T) {
	f, srv := newFakeAPI(t)
	f.routes["POST /api/v1/stacks"] = func(w http.ResponseWriter, _ *http.Request, _ []byte) {
		w.Header().Set("Content-Type", "application/problem+json")
		w.WriteHeader(422)
		_, _ = w.Write([]byte(`{"title":"Validation error","status":422,"detail":"Request payload failed validation.",
			"errors":[{"loc":["body","tool_version"],"msg":"Value error, tool_version must be MAJOR.MINOR"}]}`))
	}
	r := runCLI(t, srv.URL, "", "stacks", "create", "x", "--repo-url", "u", "--tool-version", "bad")
	if r.err == nil || !strings.Contains(r.err.Error(), "tool_version must be MAJOR.MINOR") ||
		!strings.Contains(r.err.Error(), "422 Validation error") {
		t.Fatalf("unexpected error: %v", r.err)
	}
}

func mustJSON(v any) string {
	b, _ := json.Marshal(v)
	var m any
	_ = json.Unmarshal(b, &m)
	b, _ = json.Marshal(m) // canonical: sorted keys
	return string(b)
}

func TestRunIDMustBeAUUID(t *testing.T) {
	f, srv := newFakeAPI(t)
	for _, args := range [][]string{
		{"runs", "get", "../stacks"},
		{"runs", "cancel", "../environments/" + env1 + "/runs"},
		{"runs", "logs", "x"},
	} {
		r := runCLI(t, srv.URL, "", args...)
		if r.err == nil || !strings.Contains(r.err.Error(), "invalid run id") {
			t.Fatalf("%v: want an invalid run id error, got %v", args, r.err)
		}
	}
	if len(f.calls) != 0 {
		t.Fatalf("no API call expected, got %+v", f.calls)
	}
}

func TestPlainHTTPRefusedOffLoopback(t *testing.T) {
	r := runCLI(t, "http://stackd.example.com", "", "spaces", "list")
	if r.err == nil || !strings.Contains(r.err.Error(), "refusing plain http") {
		t.Fatalf("want a refusal, got %v", r.err)
	}
	// --allow-http lifts it (the request then fails to connect, which is fine here).
	r = runCLI(t, "http://stackd.invalid", "", "--allow-http", "spaces", "list")
	if r.err != nil && strings.Contains(r.err.Error(), "refusing plain http") {
		t.Fatalf("--allow-http not honoured: %v", r.err)
	}
}

func TestWaitRetriesTransientErrors(t *testing.T) {
	f, srv := newFakeAPI(t)
	f.json("POST /api/v1/environments/"+env1+"/runs", 201, map[string]any{"id": run1, "state": "queued"})
	f.json("GET /api/v1/environments/"+env1, 200, map[string]any{"id": env1, "stack_id": stack1, "name": "dev"})
	runSequence(f, "planning", "unconfirmed")
	good := f.routes["GET /api/v1/runs/"+run1]
	var mu sync.Mutex
	n := 0
	f.routes["GET /api/v1/runs/"+run1] = func(w http.ResponseWriter, r *http.Request, b []byte) {
		mu.Lock()
		n++
		k := n
		mu.Unlock()
		if k <= 2 { // an API restarting behind its proxy
			w.WriteHeader(503)
			_, _ = w.Write([]byte("<html>bad gateway</html>"))
			return
		}
		good(w, r, b)
	}
	r := runCLI(t, srv.URL, "", "runs", "plan", env1, "--wait")
	if r.err != nil {
		t.Fatalf("a transient 503 should be retried, got %v", r.err)
	}
	if !strings.Contains(r.errOut, "retrying") || !strings.Contains(r.errOut, "Reconnected") {
		t.Fatalf("missing retry notice:\n%s", r.errOut)
	}
	// A 4xx is an answer: no retry.
	f.problem("GET /api/v1/runs/"+run1, 404, "Run not found", "")
	r = runCLI(t, srv.URL, "", "runs", "plan", env1, "--wait")
	if r.err == nil || !strings.Contains(r.err.Error(), "404") {
		t.Fatalf("want the 404, got %v", r.err)
	}
}

func TestExitCodes(t *testing.T) {
	f, srv := newFakeAPI(t)
	f.json("POST /api/v1/environments/"+env1+"/runs", 201, map[string]any{"id": run1, "state": "queued"})
	f.json("GET /api/v1/environments/"+env1, 200, map[string]any{"id": env1, "stack_id": stack1, "name": "dev"})
	code := func(states []string, extra ...string) (int, string) {
		runSequence(f, states...)
		var out, errOut bytes.Buffer
		a := &app{in: strings.NewReader(""), out: &out, errOut: &errOut, pollInterval: 5 * time.Millisecond}
		args := append([]string{"--url", srv.URL, "--token", token, "runs", "plan", env1, "--wait"}, extra...)
		return run(context.Background(), newRoot(a), args), errOut.String()
	}
	if c, _ := code([]string{"planning", "unconfirmed"}); c != 0 {
		t.Fatalf("unconfirmed: exit %d", c)
	}
	if c, _ := code([]string{"planning", "finished"}); c != 0 {
		t.Fatalf("finished: exit %d", c)
	}
	if c, _ := code([]string{"canceled"}); c != 2 {
		t.Fatalf("canceled: exit %d", c)
	}
	if c, msg := code([]string{"planning"}, "--timeout", "50ms"); c != 1 || !strings.Contains(msg, "gave up waiting") {
		t.Fatalf("timeout: exit %d, %q", c, msg)
	}
}

// logsHandler serves a run's chunks honouring ?phase= and ?after_seq= like the API, and records
// the queries it got. chunks grows as the test appends to it.
func logsHandler(chunks *[]map[string]any, mu *sync.Mutex, queries *[]string) func(http.ResponseWriter, *http.Request, []byte) {
	return func(w http.ResponseWriter, r *http.Request, _ []byte) {
		mu.Lock()
		defer mu.Unlock()
		*queries = append(*queries, r.URL.RawQuery)
		phase := r.URL.Query().Get("phase")
		after := -1
		if s := r.URL.Query().Get("after_seq"); s != "" {
			_, _ = fmt.Sscan(s, &after)
		}
		out := []map[string]any{}
		for _, c := range *chunks {
			if (phase == "" || c["phase"] == phase) && c["seq"].(int) > after {
				out = append(out, c)
			}
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(out)
	}
}

func chunk(phase string, seq int, msg string) map[string]any {
	return map[string]any{"phase": phase, "section": nil, "seq": seq, "lines": []map[string]any{{"t": "x", "msg": msg}}}
}

func TestLogsFollowIsIncrementalAndOrdered(t *testing.T) {
	f, srv := newFakeAPI(t)
	var mu sync.Mutex
	var queries []string
	chunks := []map[string]any{chunk("planning", 1, "plan line 1")}
	f.routes["GET /api/v1/runs/"+run1+"/logs"] = logsHandler(&chunks, &mu, &queries)
	polls := 0
	f.routes["GET /api/v1/runs/"+run1] = func(w http.ResponseWriter, _ *http.Request, _ []byte) {
		mu.Lock()
		polls++
		state := "planning"
		switch polls {
		case 2:
			chunks = append(chunks, chunk("planning", 2, "plan line 2"), chunk("applying", 1, "apply line 1"))
			state = "applying"
		case 3:
			// A late planning chunk, after applying started: it must print under a planning header.
			chunks = append(chunks, chunk("planning", 3, "plan line 3 (late)"))
			state = "applying"
		case 4:
			state = "finished"
		}
		mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"id": run1, "state": state, "created_at": "2026-01-01T00:00:00Z"})
	}
	r := runCLI(t, srv.URL, "", "runs", "logs", run1, "-f")
	if r.err != nil {
		t.Fatal(r.err)
	}
	want := "===== planning =====\nplan line 1\nplan line 2\n===== applying =====\napply line 1\n" +
		"===== planning =====\nplan line 3 (late)\n"
	if r.out != want {
		t.Fatalf("log output\n got %q\nwant %q", r.out, want)
	}
	mu.Lock()
	defer mu.Unlock()
	sawIncremental := false
	for _, q := range queries {
		if strings.Contains(q, "after_seq=1") && strings.Contains(q, "phase=planning") {
			sawIncremental = true
		}
	}
	if !sawIncremental {
		t.Fatalf("follow never asked for only the new chunks: %v", queries)
	}
}

func TestJSONModeKeepsStdoutValidWithLogs(t *testing.T) {
	f, srv := newFakeAPI(t)
	f.json("POST /api/v1/environments/"+env1+"/runs", 201, map[string]any{"id": run1, "state": "queued"})
	f.json("GET /api/v1/environments/"+env1, 200, map[string]any{"id": env1, "stack_id": stack1, "name": "dev"})
	runSequence(f, "planning", "unconfirmed")
	var mu sync.Mutex
	var queries []string
	chunks := []map[string]any{chunk("planning", 1, "Plan: 2 to add")}
	f.routes["GET /api/v1/runs/"+run1+"/logs"] = logsHandler(&chunks, &mu, &queries)
	r := runCLI(t, srv.URL, "", "-o", "json", "runs", "plan", env1, "--wait", "--logs")
	if r.err != nil {
		t.Fatal(r.err)
	}
	var got map[string]any
	if err := json.Unmarshal([]byte(r.out), &got); err != nil || got["state"] != "unconfirmed" {
		t.Fatalf("stdout is not the run's JSON: %v\n%s", err, r.out)
	}
	if !strings.Contains(r.errOut, "Plan: 2 to add") {
		t.Fatalf("logs should go to stderr in JSON mode:\n%s", r.errOut)
	}
}

func TestClearLabelsSendsNull(t *testing.T) {
	f, srv := newFakeAPI(t)
	f.json("PATCH /api/v1/stacks/"+stack1, 200, map[string]any{"id": stack1, "name": "network"})
	f.json("GET /api/v1/stacks/"+stack1, 200, map[string]any{"id": stack1, "space_id": spaceA, "name": "network"})
	r := runCLI(t, srv.URL, "", "stacks", "update", stack1, "--clear-labels")
	if r.err != nil {
		t.Fatal(r.err)
	}
	c := f.last("PATCH", "/api/v1/stacks/"+stack1)
	if v, ok := c.body["labels"]; !ok || v != nil {
		t.Fatalf("want labels: null, got %+v", c.body)
	}
	r = runCLI(t, srv.URL, "", "stacks", "update", stack1, "--clear-labels", "--label", "a=b")
	if r.err == nil {
		t.Fatal("--clear-labels with --label should be refused")
	}
}

func TestNonProblemErrorBodyIsTruncated(t *testing.T) {
	f, srv := newFakeAPI(t)
	f.routes["GET /api/v1/spaces"] = func(w http.ResponseWriter, _ *http.Request, _ []byte) {
		w.WriteHeader(502)
		_, _ = w.Write([]byte(strings.Repeat("<html>", 1000)))
	}
	r := runCLI(t, srv.URL, "", "spaces", "list")
	if r.err == nil || len(r.err.Error()) > 400 || !strings.Contains(r.err.Error(), "502") {
		t.Fatalf("want a short 502 error, got %d bytes: %.80s", len(r.err.Error()), r.err)
	}
}
