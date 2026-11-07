package client

import "time"

// The subset of the API's response schemas the CLI renders. Unknown fields are ignored on
// decode, and --output json prints the API's own body, so nothing is lost by keeping these small.

// Space mirrors SpaceOut.
type Space struct {
	ID          string    `json:"id"`
	Name        string    `json:"name"`
	Description *string   `json:"description"`
	CreatedAt   time.Time `json:"created_at"`
}

// Stack mirrors StackOut.
type Stack struct {
	ID               string         `json:"id"`
	SpaceID          string         `json:"space_id"`
	Name             string         `json:"name"`
	Description      *string        `json:"description"`
	RepoURL          string         `json:"repo_url"`
	RepoAuthKind     string         `json:"repo_auth_kind"`
	HasRepoSecret    bool           `json:"has_repo_secret"`
	HasWebhookSecret bool           `json:"has_webhook_secret"`
	ProjectRoot      string         `json:"project_root"`
	Tool             string         `json:"tool"`
	ToolVersion      string         `json:"tool_version"`
	Labels           map[string]any `json:"labels"`
	CreatedAt        time.Time      `json:"created_at"`
	UpdatedAt        time.Time      `json:"updated_at"`
}

// Environment mirrors EnvironmentOut.
type Environment struct {
	ID                      string         `json:"id"`
	StackID                 string         `json:"stack_id"`
	Name                    string         `json:"name"`
	Tier                    string         `json:"tier"`
	Branch                  string         `json:"branch"`
	Autodeploy              bool           `json:"autodeploy"`
	Protected               bool           `json:"protected"`
	RequireSecondPairOfEyes bool           `json:"require_second_pair_of_eyes"`
	ManagedState            bool           `json:"managed_state"`
	AllowMockApply          bool           `json:"allow_mock_apply"`
	AllowFallbackApply      bool           `json:"allow_fallback_apply"`
	DriftCheckEnabled       bool           `json:"drift_check_enabled"`
	DriftStatus             string         `json:"drift_status"`
	HeadSHA                 *string        `json:"head_sha"`
	Stale                   bool           `json:"stale"`
	Locked                  bool           `json:"locked"`
	Labels                  map[string]any `json:"labels"`
	Position                int            `json:"position"`
	CreatedAt               time.Time      `json:"created_at"`
	UpdatedAt               time.Time      `json:"updated_at"`
}

// Run mirrors RunOut.
type Run struct {
	ID            string              `json:"id"`
	EnvironmentID string              `json:"environment_id"`
	Type          string              `json:"type"`
	State         string              `json:"state"`
	PlanOptions   map[string][]string `json:"plan_options"`
	CommitSHA     *string             `json:"commit_sha"`
	CommitMessage *string             `json:"commit_message"`
	TriggeredBy   string              `json:"triggered_by"`
	PlanSummary   map[string]any      `json:"plan_summary"`
	UsedMocks     bool                `json:"used_mocks"`
	Error         *string             `json:"error"`
	RunGroupID    *string             `json:"run_group_id"`
	ConfirmedAt   *time.Time          `json:"confirmed_at"`
	FinishedAt    *time.Time          `json:"finished_at"`
	CreatedAt     time.Time           `json:"created_at"`
}

// LogChunk mirrors LogChunkOut; each line is {"t": timestamp, "msg": text (ANSI kept)}.
type LogChunk struct {
	Phase   string           `json:"phase"`
	Section *string          `json:"section"`
	Seq     int              `json:"seq"`
	Lines   []map[string]any `json:"lines"`
}

// Terminal run states (SPECS §4.1): the run will not move again.
var TerminalStates = map[string]bool{
	"finished": true, "failed": true, "discarded": true, "canceled": true,
}
