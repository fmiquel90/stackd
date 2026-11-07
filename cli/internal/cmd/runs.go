package cmd

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/spf13/cobra"

	"github.com/fmiquel90/stackd/cli/internal/client"
)

// The CLI exposes no `confirm`: a service-account token can never confirm an apply or a destroy
// (SPECS §23), and a human confirms in the UI where they see the plan.

func newRunsCmd(a *app) *cobra.Command {
	cmd := &cobra.Command{
		Use:   "runs",
		Short: "Trigger plans, follow runs, read logs",
		Long: `Runs on an environment. An environment is named by its id or as <stack>/<environment>.

There is no 'confirm' command: applying a plan is a named human's decision, taken in the UI
(a service-account token can never confirm, SPECS §23).`,
	}
	cmd.AddCommand(runsList(a), runsPlan(a), runsGet(a), runsLogs(a), runsCancel(a), runsDiscard(a))
	return cmd
}

func runsList(a *app) *cobra.Command {
	var space string
	var limit int
	c := &cobra.Command{
		Use:   "list <stack>/<env> | <env-id>",
		Short: "List an environment's runs, newest first",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			c, err := a.client()
			if err != nil {
				return err
			}
			e, err := resolveEnv(cmd.Context(), c, args[0], space)
			if err != nil {
				return err
			}
			var runs []client.Run
			raw, err := c.JSON(cmd.Context(), http.MethodGet, "/environments/"+e.ID+"/runs", nil, nil, &runs)
			if err != nil {
				return err
			}
			if a.json() {
				return printJSON(a.out, raw)
			}
			if limit > 0 && len(runs) > limit {
				runs = runs[:limit]
			}
			rows := make([][]string, 0, len(runs))
			for _, r := range runs {
				rows = append(rows, []string{
					r.ID, r.Type, r.State, r.TriggeredBy, short(r.CommitSHA), planSummary(r.PlanSummary),
					r.CreatedAt.Local().Format(time.DateTime),
				})
			}
			return table(a.out, []string{"ID", "TYPE", "STATE", "TRIGGER", "COMMIT", "PLAN", "CREATED"}, rows)
		},
	}
	c.Flags().StringVar(&space, "space", "", "space of the stack, when its name is not unique")
	c.Flags().IntVar(&limit, "limit", 20, "rows shown in text output (0 = all)")
	return c
}

func runsPlan(a *app) *cobra.Command {
	var (
		space, commit       string
		destroy, downstream bool
		wait, logs          bool
		targets, replace    []string
		timeout             time.Duration
	)
	c := &cobra.Command{
		Use:   "plan <stack>/<env> | <env-id>",
		Short: "Trigger a plan (tracked, or destroy) on an environment",
		Long: `Trigger a plan. The run is queued; a worker picks it up. Applying it needs a human
confirmation in the UI (or the environment's autodeploy).

With --wait the command follows the run until it awaits confirmation (unconfirmed) or ends, and
exits 0 if it reached unconfirmed or finished, 2 if it failed, was canceled or discarded.`,
		Example: `  stackd runs plan network/prod --wait --logs
  stackd runs plan network/dev --target module.vpc --output json`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			c, err := a.client()
			if err != nil {
				return err
			}
			e, err := resolveEnv(cmd.Context(), c, args[0], space)
			if err != nil {
				return err
			}
			body := map[string]any{"type": "tracked"}
			if destroy {
				body["type"] = "destroy"
			}
			if downstream {
				body["with_downstream"] = true
			}
			if commit != "" {
				body["commit_sha"] = commit
			}
			if len(targets) > 0 {
				body["targets"] = targets
			}
			if len(replace) > 0 {
				body["replace"] = replace
			}
			var run client.Run
			raw, err := c.JSON(cmd.Context(), http.MethodPost, "/environments/"+e.ID+"/runs", nil, body, &run)
			if err != nil {
				return err
			}
			if !wait {
				if a.json() {
					return printJSON(a.out, raw)
				}
				fmt.Fprintf(a.out, "Run %s %s on %s\n", run.ID, run.State, e.Name)
				return nil
			}
			fmt.Fprintf(a.errOut, "Run %s %s on %s\n", run.ID, run.State, e.Name)
			ctx, cancel := withOptionalTimeout(cmd.Context(), timeout)
			defer cancel()
			return a.follow(ctx, c, run.ID, logs)
		},
	}
	f := c.Flags()
	f.StringVar(&space, "space", "", "space of the stack, when its name is not unique")
	f.StringVar(&commit, "commit", "", "plan this commit instead of the branch head")
	f.BoolVar(&destroy, "destroy", false, "a destroy plan (confirming it needs the destroy right)")
	f.BoolVar(&downstream, "with-downstream", false, "start a cascade run group through dependent environments")
	f.StringArrayVar(&targets, "target", nil, "resource address passed as -target (repeatable)")
	f.StringArrayVar(&replace, "replace", nil, "resource address passed as -replace (repeatable, tracked runs only)")
	f.BoolVar(&wait, "wait", false, "follow the run until it awaits confirmation or ends")
	f.BoolVar(&logs, "logs", false, "with --wait, stream the run's logs (to stderr with --output json)")
	f.DurationVar(&timeout, "timeout", 30*time.Minute, "with --wait, give up after this long (0 = no limit)")
	return c
}

func runsGet(a *app) *cobra.Command {
	return &cobra.Command{
		Use:   "get <run-id>",
		Short: "Show a run",
		Args:  runIDArg,
		RunE: func(cmd *cobra.Command, args []string) error {
			c, err := a.client()
			if err != nil {
				return err
			}
			var r client.Run
			raw, err := c.JSON(cmd.Context(), http.MethodGet, "/runs/"+args[0], nil, nil, &r)
			if err != nil {
				return err
			}
			if a.json() {
				return printJSON(a.out, raw)
			}
			return printRun(a.out, &r)
		},
	}
}

func printRun(w io.Writer, r *client.Run) error {
	kv := [][2]string{
		{"ID", r.ID},
		{"Environment", r.EnvironmentID},
		{"Type", r.Type},
		{"State", r.State},
		{"Triggered by", r.TriggeredBy},
		{"Commit", short(r.CommitSHA)},
		{"Plan", planSummary(r.PlanSummary)},
		{"Used mocks", yesNo(r.UsedMocks)},
	}
	if len(r.PlanOptions) > 0 {
		for _, k := range []string{"targets", "replace"} {
			if v := r.PlanOptions[k]; len(v) > 0 {
				kv = append(kv, [2]string{"Plan " + k, strings.Join(v, ", ")})
			}
		}
	}
	if r.RunGroupID != nil {
		kv = append(kv, [2]string{"Run group", *r.RunGroupID})
	}
	if r.Error != nil {
		kv = append(kv, [2]string{"Error", *r.Error})
	}
	kv = append(kv, [2]string{"Created", r.CreatedAt.Local().Format(time.DateTime)})
	if r.FinishedAt != nil {
		kv = append(kv, [2]string{"Finished", r.FinishedAt.Local().Format(time.DateTime)})
	}
	return fields(w, kv)
}

func runsLogs(a *app) *cobra.Command {
	var follow bool
	var timeout time.Duration
	c := &cobra.Command{
		Use:   "logs <run-id>",
		Short: "Print a run's logs (ANSI kept)",
		Long: `Print a run's logs. With --follow, keep printing new lines until the run ends or awaits
confirmation; the exit status is then the run's, as for 'runs plan --wait' (0, or 2 if it failed,
was canceled or discarded). With --output json and --follow, log lines go to stderr and stdout
carries only the run's final JSON.`,
		Args: runIDArg,
		RunE: func(cmd *cobra.Command, args []string) error {
			c, err := a.client()
			if err != nil {
				return err
			}
			if follow {
				ctx, cancel := withOptionalTimeout(cmd.Context(), timeout)
				defer cancel()
				return a.follow(ctx, c, args[0], true)
			}
			var chunks []client.LogChunk
			raw, err := c.JSON(cmd.Context(), http.MethodGet, "/runs/"+args[0]+"/logs", nil, nil, &chunks)
			if err != nil {
				return err
			}
			if a.json() {
				return printJSON(a.out, raw)
			}
			newLogPrinter(a.out).print(chunks)
			return nil
		},
	}
	c.Flags().BoolVarP(&follow, "follow", "f", false, "keep printing new lines until the run ends or awaits confirmation")
	c.Flags().DurationVar(&timeout, "timeout", 30*time.Minute, "with --follow, give up after this long (0 = no limit)")
	return c
}

func runAction(a *app, action, short string) *cobra.Command {
	return &cobra.Command{
		Use:   action + " <run-id>",
		Short: short,
		Args:  runIDArg,
		RunE: func(cmd *cobra.Command, args []string) error {
			c, err := a.client()
			if err != nil {
				return err
			}
			var r client.Run
			raw, err := c.JSON(cmd.Context(), http.MethodPost, "/runs/"+args[0]+"/"+action, nil, nil, &r)
			if err != nil {
				return err
			}
			if a.json() {
				return printJSON(a.out, raw)
			}
			fmt.Fprintf(a.out, "Run %s %s\n", r.ID, r.State)
			return nil
		},
	}
}

func runsCancel(a *app) *cobra.Command {
	return runAction(a, "cancel", "Cancel a queued or running run")
}

func runsDiscard(a *app) *cobra.Command {
	return runAction(a, "discard", "Discard a plan awaiting confirmation")
}

// runIDArg requires exactly one argument that is a UUID: a run id goes into the URL path, so a
// value like "../environments/<id>/runs" must never reach it.
func runIDArg(cmd *cobra.Command, args []string) error {
	if err := cobra.ExactArgs(1)(cmd, args); err != nil {
		return err
	}
	if !isUUID(args[0]) {
		return fmt.Errorf("invalid run id %q: expected a UUID", args[0])
	}
	return nil
}

// withOptionalTimeout is context.WithTimeout, except that 0 means no deadline.
func withOptionalTimeout(ctx context.Context, d time.Duration) (context.Context, context.CancelFunc) {
	if d <= 0 {
		return context.WithCancel(ctx)
	}
	return context.WithTimeout(ctx, d)
}

// transient reports whether a failed call is worth retrying while following a run: a network
// error, a 5xx (an API restarting behind its proxy) or a 429. A 4xx is an answer, not a blip.
func transient(err error) bool {
	var p *client.Problem
	if errors.As(err, &p) {
		return p.Status >= 500 || p.Status == http.StatusTooManyRequests
	}
	return true
}

// workerPhases are the log phases a worker writes while a run executes, in order (SPECS §4.1:
// log phases are run states). Followed incrementally; a final full read catches any other.
var workerPhases = []string{"preparing", "planning", "checking", "applying", "running"}

// follow polls a run until it settles: terminal, or unconfirmed — the plan awaits a human, the end
// of what automation can do (nothing more happens until someone confirms). It prints new log
// lines when withLogs: to stdout, or to stderr in JSON mode so stdout stays one JSON document.
// Transient API errors are retried until the context's deadline.
func (a *app) follow(ctx context.Context, c *client.Client, runID string, withLogs bool) error {
	logOut := a.out
	if a.json() {
		logOut = a.errOut
	}
	printer := newLogPrinter(logOut)
	lastState := ""
	failures := 0
	var run client.Run
	gaveUp := func() error {
		return fmt.Errorf("gave up waiting on run %s (last state %s): %w", runID, lastState, context.Cause(ctx))
	}
	for {
		raw, err := c.JSON(ctx, http.MethodGet, "/runs/"+runID, nil, nil, &run)
		if err != nil {
			if ctx.Err() != nil {
				return gaveUp()
			}
			if !transient(err) {
				return err
			}
			failures++
			if failures == 1 {
				fmt.Fprintf(a.errOut, "Warning: %v (retrying)\n", err)
			}
		} else {
			if failures > 0 {
				fmt.Fprintln(a.errOut, "Reconnected.")
				failures = 0
			}
			done := client.TerminalStates[run.State] || run.State == "unconfirmed"
			if withLogs {
				printer.poll(ctx, c, runID, run.State, done)
			}
			if run.State != lastState {
				if lastState != "" {
					fmt.Fprintf(a.errOut, "Run %s: %s\n", runID, run.State)
				}
				lastState = run.State
			}
			if done {
				return a.settled(&run, raw, withLogs)
			}
		}
		// Back off on repeated failures (up to 30 s), otherwise poll at the normal interval.
		delay := a.pollInterval
		for i := 1; i < failures && delay < 30*time.Second; i++ {
			delay *= 2
		}
		select {
		case <-ctx.Done():
			return gaveUp()
		case <-time.After(delay):
		}
	}
}

// settled prints the outcome of a run that stopped moving and returns its exit status.
func (a *app) settled(run *client.Run, raw []byte, withLogs bool) error {
	switch {
	case a.json():
		if err := printJSON(a.out, raw); err != nil {
			return err
		}
	case withLogs:
		fmt.Fprintf(a.errOut, "Run %s ended %s %s\n", run.ID, run.State, planSummary(run.PlanSummary))
	default:
		if err := printRun(a.out, run); err != nil {
			return err
		}
	}
	if run.State == "unconfirmed" && run.UsedMocks {
		// Not an error, but worth saying: such a plan is blocked at apply by default (§9.3).
		fmt.Fprintln(a.errOut, "Note: this plan consumed mock values; it cannot be applied unless the environment allows mock apply.")
	}
	return runOutcome(run)
}

// runOutcome maps a settled run to the exit status: 0 when it finished or awaits confirmation,
// 2 when it failed, was canceled or discarded.
func runOutcome(r *client.Run) error {
	switch r.State {
	case "finished", "unconfirmed":
		return nil
	default:
		msg := fmt.Sprintf("run %s ended %s", r.ID, r.State)
		if r.Error != nil {
			msg += ": " + *r.Error
		}
		return &exitError{code: 2, msg: msg}
	}
}

// phaseOrder is the run state order (SPECS §4.1), which is also the order of log phases.
var phaseOrder = map[string]int{
	"queued": 0, "preparing": 1, "planning": 2, "checking": 3, "unconfirmed": 4, "confirmed": 5,
	"applying": 6, "running": 7, "finished": 8, "failed": 9, "discarded": 10, "canceled": 11,
}

// logPrinter prints each log chunk once (key: phase + seq, the API's own key), phases in run
// order, chunks by seq, with a phase header whenever the phase changes.
type logPrinter struct {
	w         io.Writer
	seen      map[string]bool
	maxSeq    map[string]int // highest seq printed per phase, for incremental reads
	lastPhase string
}

func newLogPrinter(w io.Writer) *logPrinter {
	return &logPrinter{w: w, seen: map[string]bool{}, maxSeq: map[string]int{}}
}

// poll reads only what is new: for each worker phase the run has reached, the chunks after the
// last seq printed. On the final poll (final=true) it reads everything once, so a phase outside
// workerPhases is never lost. Read errors are skipped: the next poll catches up.
func (p *logPrinter) poll(ctx context.Context, c *client.Client, runID, state string, final bool) {
	if final {
		var chunks []client.LogChunk
		if _, err := c.JSON(ctx, http.MethodGet, "/runs/"+runID+"/logs", nil, nil, &chunks); err == nil {
			p.print(chunks)
		}
		return
	}
	current, ok := phaseOrder[state]
	if !ok {
		current = len(phaseOrder)
	}
	for _, phase := range workerPhases {
		if phaseOrder[phase] > current {
			break
		}
		q := url.Values{"phase": {phase}}
		if seq, ok := p.maxSeq[phase]; ok {
			q.Set("after_seq", strconv.Itoa(seq))
		}
		var chunks []client.LogChunk
		if _, err := c.JSON(ctx, http.MethodGet, "/runs/"+runID+"/logs", q, nil, &chunks); err == nil {
			p.print(chunks)
		}
	}
}

func (p *logPrinter) print(chunks []client.LogChunk) {
	sort.SliceStable(chunks, func(i, j int) bool {
		oi, okI := phaseOrder[chunks[i].Phase]
		oj, okJ := phaseOrder[chunks[j].Phase]
		if !okI {
			oi = len(phaseOrder)
		}
		if !okJ {
			oj = len(phaseOrder)
		}
		if oi != oj {
			return oi < oj
		}
		if chunks[i].Phase != chunks[j].Phase {
			return chunks[i].Phase < chunks[j].Phase
		}
		return chunks[i].Seq < chunks[j].Seq
	})
	for _, ch := range chunks {
		key := fmt.Sprintf("%s/%d", ch.Phase, ch.Seq)
		if p.seen[key] {
			continue
		}
		p.seen[key] = true
		if seq, ok := p.maxSeq[ch.Phase]; !ok || ch.Seq > seq {
			p.maxSeq[ch.Phase] = ch.Seq
		}
		if ch.Phase != p.lastPhase {
			fmt.Fprintf(p.w, "===== %s =====\n", ch.Phase)
			p.lastPhase = ch.Phase
		}
		prefix := ""
		if ch.Section != nil && *ch.Section != "" {
			prefix = "[" + *ch.Section + "] "
		}
		for _, ln := range ch.Lines {
			msg, _ := ln["msg"].(string)
			fmt.Fprintf(p.w, "%s%s\n", prefix, msg)
		}
	}
}
