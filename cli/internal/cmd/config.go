package cmd

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"

	"github.com/spf13/cobra"
)

func newConfigCmd(a *app) *cobra.Command {
	cmd := &cobra.Command{
		Use:   "config",
		Short: "Export and import a space's configuration (stackd.config, SPECS §22)",
		Long: `A space's configuration as a stackd.config JSON document: stacks, environments,
variables, variable sets, dependencies, hooks, notifications. Never a secret value.

Import merges by name: it creates what is missing and updates what differs. It never deletes,
and it applies all or nothing. Both commands need the space admin role.`,
	}
	cmd.AddCommand(configExport(a), configImport(a))
	return cmd
}

func configExport(a *app) *cobra.Command {
	var file string
	c := &cobra.Command{
		Use:     "export <space>",
		Short:   "Write a space's configuration document",
		Example: `  stackd config export platform -f platform.json`,
		Args:    cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			c, err := a.client()
			if err != nil {
				return err
			}
			sp, err := resolveSpace(cmd.Context(), c, args[0])
			if err != nil {
				return err
			}
			raw, err := c.Do(cmd.Context(), http.MethodGet, "/spaces/"+sp.ID+"/export", nil, nil)
			if err != nil {
				return err
			}
			if file == "" || file == "-" {
				return printJSON(a.out, raw)
			}
			f, err := os.Create(file)
			if err != nil {
				return err
			}
			if err := printJSON(f, raw); err != nil {
				f.Close()
				return err
			}
			if err := f.Close(); err != nil {
				return err
			}
			fmt.Fprintf(a.errOut, "Wrote %s\n", file)
			return nil
		},
	}
	c.Flags().StringVarP(&file, "file", "f", "", "write to this file instead of stdout")
	return c
}

// importReport is the part of the import report (§22.3) the text output summarizes.
type importReport struct {
	DryRun  bool `json:"dry_run"`
	Summary struct {
		Created   int `json:"created"`
		Updated   int `json:"updated"`
		Unchanged int `json:"unchanged"`
		Skipped   int `json:"skipped"`
	} `json:"summary"`
	Changes []struct {
		Op     string   `json:"op"`
		Kind   string   `json:"kind"`
		Path   string   `json:"path"`
		Fields []string `json:"fields"`
	} `json:"changes"`
	Skipped        []map[string]any    `json:"skipped"`
	MissingSecrets []map[string]any    `json:"missing_secrets"`
	Warnings       []map[string]any    `json:"warnings"`
	PoolTokens     []map[string]string `json:"worker_pool_tokens"`
}

func configImport(a *app) *cobra.Command {
	var dryRun bool
	c := &cobra.Command{
		Use:   "import <space> <file|->",
		Short: "Merge a configuration document into a space",
		Example: `  stackd config import platform platform.json --dry-run
  stackd config import platform platform.json`,
		Args: cobra.ExactArgs(2),
		RunE: func(cmd *cobra.Command, args []string) error {
			var data []byte
			var err error
			if args[1] == "-" {
				if isTerminal(a.in) {
					return fmt.Errorf("'-' reads the document from a pipe: stackd config import <space> - < file.json")
				}
				data, err = io.ReadAll(a.in)
			} else {
				data, err = os.ReadFile(args[1])
			}
			if err != nil {
				return err
			}
			var doc json.RawMessage
			if err := json.Unmarshal(data, &doc); err != nil {
				return fmt.Errorf("%s is not valid JSON: %w", args[1], err)
			}
			c, err := a.client()
			if err != nil {
				return err
			}
			sp, err := resolveSpace(cmd.Context(), c, args[0])
			if err != nil {
				return err
			}
			q := url.Values{}
			if dryRun {
				q.Set("dry_run", "true")
			}
			var rep importReport
			raw, err := c.JSON(cmd.Context(), http.MethodPost, "/spaces/"+sp.ID+"/import", q, doc, &rep)
			if err != nil {
				return err
			}
			if a.json() {
				return printJSON(a.out, raw)
			}
			return printImportReport(a.out, sp.Name, &rep)
		},
	}
	c.Flags().BoolVar(&dryRun, "dry-run", false, "compute the report and roll back")
	return c
}

func printImportReport(w io.Writer, space string, r *importReport) error {
	verb := "Imported into"
	if r.DryRun {
		verb = "Dry run on"
	}
	fmt.Fprintf(w, "%s space %s: %d created, %d updated, %d unchanged, %d skipped\n",
		verb, space, r.Summary.Created, r.Summary.Updated, r.Summary.Unchanged, r.Summary.Skipped)
	rows := [][]string{}
	for _, ch := range r.Changes {
		if ch.Op == "unchanged" {
			continue
		}
		rows = append(rows, []string{ch.Op, ch.Kind, ch.Path, joinFields(ch.Fields)})
	}
	if len(rows) > 0 {
		fmt.Fprintln(w)
		if err := table(w, []string{"OP", "KIND", "PATH", "FIELDS"}, rows); err != nil {
			return err
		}
	}
	listSection(w, "Skipped", r.Skipped)
	listSection(w, "Missing secrets (fill them in through the API or UI)", r.MissingSecrets)
	listSection(w, "Warnings", r.Warnings)
	if len(r.PoolTokens) > 0 {
		// Shown once by the API, like POST /worker-pools: the operator needs them now.
		fmt.Fprintln(w, "\nWorker pool tokens (shown once, store them now):")
		for _, t := range r.PoolTokens {
			fmt.Fprintf(w, "  %s: %s\n", t["name"], t["token"])
		}
	}
	return nil
}

func joinFields(f []string) string {
	b, _ := json.Marshal(f)
	if len(f) == 0 {
		return ""
	}
	return string(b)
}

func listSection(w io.Writer, title string, items []map[string]any) {
	if len(items) == 0 {
		return
	}
	fmt.Fprintf(w, "\n%s:\n", title)
	for _, it := range items {
		b, _ := json.Marshal(it)
		fmt.Fprintf(w, "  %s\n", b)
	}
}
