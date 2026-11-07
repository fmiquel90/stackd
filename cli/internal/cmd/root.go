// Package cmd holds the stackd command tree (cobra).
package cmd

import (
	"context"
	"fmt"
	"io"
	"os"
	"time"

	"github.com/spf13/cobra"

	"github.com/fmiquel90/stackd/cli/internal/client"
)

// Version is set at build time (-ldflags "-X github.com/fmiquel90/stackd/cli/internal/cmd.Version=…").
var Version = "dev"

// app carries the global options and I/O shared by every command.
type app struct {
	url          string
	token        string
	output       string // text | json
	out, errOut  io.Writer
	in           io.Reader
	pollInterval time.Duration
	api          *client.Client
}

func (a *app) client() (*client.Client, error) {
	if a.api != nil {
		return a.api, nil
	}
	if a.output != "text" && a.output != "json" {
		return nil, fmt.Errorf("invalid --output %q: text or json", a.output)
	}
	if a.url == "" {
		a.url = os.Getenv("STACKD_URL")
	}
	if a.token == "" {
		a.token = os.Getenv("STACKD_TOKEN")
	}
	c, err := client.New(a.url, a.token, "stackd-cli/"+Version)
	if err != nil {
		return nil, err
	}
	a.api = c
	return c, nil
}

func (a *app) json() bool { return a.output == "json" }

// NewRootCmd builds the command tree. Streams are injectable for tests.
func NewRootCmd(in io.Reader, out, errOut io.Writer) *cobra.Command {
	return newRoot(&app{in: in, out: out, errOut: errOut, pollInterval: 2 * time.Second})
}

func newRoot(a *app) *cobra.Command {
	root := &cobra.Command{
		Use:   "stackd",
		Short: "Command-line client for the Stackd API",
		Long: `stackd drives a Stackd instance over its REST API: spaces, stacks, environments,
runs and configuration export/import.

Authentication: a service-account API token (stkd_…) or any bearer token the API accepts.
Set STACKD_URL and STACKD_TOKEN, or pass --url and --token. The token is never printed.

A command can only do what its token is allowed to do. In particular a service account never
confirms an apply or a destroy: that stays a named human's decision (SPECS §23).`,
		SilenceUsage:  true,
		SilenceErrors: true,
	}
	root.SetIn(a.in)
	root.SetOut(a.out)
	root.SetErr(a.errOut)
	pf := root.PersistentFlags()
	// Env fallbacks are read in client(), not used as flag defaults: a default is printed by
	// --help, and the token must never be.
	pf.StringVar(&a.url, "url", "", "Stackd instance URL (env STACKD_URL)")
	pf.StringVar(&a.token, "token", "", "API token (env STACKD_TOKEN)")
	pf.StringVarP(&a.output, "output", "o", envOr("STACKD_OUTPUT", "text"), "output format: text or json (env STACKD_OUTPUT)")

	root.AddCommand(
		newSpacesCmd(a),
		newStacksCmd(a),
		newEnvsCmd(a),
		newRunsCmd(a),
		newConfigCmd(a),
		&cobra.Command{
			Use:   "version",
			Short: "Print the CLI version",
			Args:  cobra.NoArgs,
			Run: func(cmd *cobra.Command, _ []string) {
				fmt.Fprintf(a.out, "stackd %s\n", Version)
			},
		},
	)
	return root
}

// Execute runs the CLI and returns the process exit code.
func Execute(ctx context.Context) int {
	root := NewRootCmd(os.Stdin, os.Stdout, os.Stderr)
	if err := root.ExecuteContext(ctx); err != nil {
		fmt.Fprintln(os.Stderr, "Error:", err)
		if e, ok := err.(*exitError); ok {
			return e.code
		}
		return 1
	}
	return 0
}

// exitError carries a specific exit code (e.g. a run that ended failed under --wait).
type exitError struct {
	code int
	msg  string
}

func (e *exitError) Error() string { return e.msg }

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}
