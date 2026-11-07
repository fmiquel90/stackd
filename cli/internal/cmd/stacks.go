package cmd

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"

	"github.com/spf13/cobra"

	"github.com/fmiquel90/stackd/cli/internal/client"
)

func newStacksCmd(a *app) *cobra.Command {
	cmd := &cobra.Command{Use: "stacks", Aliases: []string{"stack"}, Short: "Manage stacks"}
	cmd.AddCommand(stacksList(a), stacksGet(a), stacksCreate(a), stacksUpdate(a), stacksDelete(a))
	return cmd
}

func stacksList(a *app) *cobra.Command {
	var space string
	c := &cobra.Command{
		Use:   "list",
		Short: "List stacks",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			c, err := a.client()
			if err != nil {
				return err
			}
			var stacks []client.Stack
			raw, err := c.JSON(cmd.Context(), http.MethodGet, "/stacks", nil, nil, &stacks)
			if err != nil {
				return err
			}
			if space != "" {
				sp, err := resolveSpace(cmd.Context(), c, space)
				if err != nil {
					return err
				}
				stacks, raw, err = filterBySpace(stacks, raw, sp.ID)
				if err != nil {
					return err
				}
			}
			if a.json() {
				return printJSON(a.out, raw)
			}
			rows := make([][]string, 0, len(stacks))
			for _, s := range stacks {
				rows = append(rows, []string{s.ID, s.Name, s.Tool + " " + s.ToolVersion, s.RepoURL, s.ProjectRoot})
			}
			return table(a.out, []string{"ID", "NAME", "TOOL", "REPO", "ROOT"}, rows)
		},
	}
	c.Flags().StringVar(&space, "space", "", "only stacks of this space (id or name)")
	return c
}

func stacksGet(a *app) *cobra.Command {
	var space string
	c := &cobra.Command{
		Use:   "get <stack>",
		Short: "Show a stack (id or name)",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			c, err := a.client()
			if err != nil {
				return err
			}
			s, err := resolveStack(cmd.Context(), c, args[0], space)
			if err != nil {
				return err
			}
			if a.json() {
				// The API's own body, not the CLI's subset of it.
				raw, err := c.Do(cmd.Context(), http.MethodGet, "/stacks/"+s.ID, nil, nil)
				if err != nil {
					return err
				}
				return printJSON(a.out, raw)
			}
			return printStack(a.out, s)
		},
	}
	c.Flags().StringVar(&space, "space", "", "space of the stack, when its name is not unique")
	return c
}

func printStack(w io.Writer, s *client.Stack) error {
	return fields(w, [][2]string{
		{"ID", s.ID},
		{"Name", s.Name},
		{"Space", s.SpaceID},
		{"Description", deref(s.Description)},
		{"Repo", s.RepoURL},
		{"Repo auth", s.RepoAuthKind},
		{"Repo secret set", yesNo(s.HasRepoSecret)},
		{"Webhook secret set", yesNo(s.HasWebhookSecret)},
		{"Project root", s.ProjectRoot},
		{"Tool", s.Tool + " " + s.ToolVersion},
		{"Labels", labelsString(s.Labels)},
	})
}

// stackFlags are the writable stack fields shared by create and update.
type stackFlags struct {
	name, description, repoURL, repoAuthKind, projectRoot, tool, toolVersion string
	labels                                                                   []string
	repoSecretStdin, clearLabels                                             bool
}

func (f *stackFlags) register(cmd *cobra.Command, forUpdate bool) {
	fl := cmd.Flags()
	if forUpdate {
		fl.StringVar(&f.name, "name", "", "rename the stack")
		fl.BoolVar(&f.clearLabels, "clear-labels", false, "remove every label")
	}
	fl.StringVar(&f.description, "description", "", "description")
	fl.StringVar(&f.repoURL, "repo-url", "", "git repository URL")
	fl.StringVar(&f.repoAuthKind, "repo-auth-kind", "", "repository auth: none, token or deploy_key")
	fl.StringVar(&f.projectRoot, "project-root", "", "directory of the root module in the repo (default \".\")")
	fl.StringVar(&f.tool, "tool", "", "opentofu (default) or terraform")
	fl.StringVar(&f.toolVersion, "tool-version", "", "MAJOR.MINOR or MAJOR.MINOR.PATCH")
	fl.StringArrayVar(&f.labels, "label", nil, "label key=value (repeatable; replaces all labels)")
	fl.BoolVar(&f.repoSecretStdin, "repo-secret-stdin", false,
		"read the repository token / deploy key from stdin (never pass a secret as a flag)")
	if forUpdate {
		cmd.MarkFlagsMutuallyExclusive("label", "clear-labels")
	}
}

// body builds the request from the flags the user actually set (PATCH semantics for update).
func (f *stackFlags) body(cmd *cobra.Command, in io.Reader) (map[string]any, error) {
	b := map[string]any{}
	set := func(flag, key, val string) {
		if cmd.Flags().Changed(flag) {
			b[key] = val
		}
	}
	set("name", "name", f.name)
	set("description", "description", f.description)
	set("repo-url", "repo_url", f.repoURL)
	set("repo-auth-kind", "repo_auth_kind", f.repoAuthKind)
	set("project-root", "project_root", f.projectRoot)
	set("tool", "tool", f.tool)
	set("tool-version", "tool_version", f.toolVersion)
	if f.clearLabels {
		b["labels"] = nil
	}
	if cmd.Flags().Changed("label") {
		labels, err := parseLabels(f.labels)
		if err != nil {
			return nil, err
		}
		b["labels"] = labels
	}
	if f.repoSecretStdin {
		secret, err := readSecret(in)
		if err != nil {
			return nil, err
		}
		b["repo_secret"] = secret
	}
	return b, nil
}

// readSecret reads a secret from stdin: the whole input for a multi-line deploy key, trimmed of
// the trailing newline.
func readSecret(in io.Reader) (string, error) {
	if isTerminal(in) {
		// Typed at a terminal, the secret would be echoed on screen: require a pipe.
		return "", fmt.Errorf(`--repo-secret-stdin reads a pipe, not a terminal: printf %%s "$REPO_TOKEN" | stackd …`)
	}
	data, err := io.ReadAll(bufio.NewReader(in))
	if err != nil {
		return "", fmt.Errorf("read secret from stdin: %w", err)
	}
	s := strings.TrimRight(string(data), "\r\n")
	if s == "" {
		return "", fmt.Errorf("empty secret on stdin")
	}
	return s, nil
}

func stacksCreate(a *app) *cobra.Command {
	var f stackFlags
	var space string
	c := &cobra.Command{
		Use:   "create <name>",
		Short: "Create a stack in a space (writer in that space)",
		Example: `  stackd stacks create network --space platform \
    --repo-url https://github.com/acme/infra.git --project-root network --tool-version 1.10`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			c, err := a.client()
			if err != nil {
				return err
			}
			body, err := f.body(cmd, a.in)
			if err != nil {
				return err
			}
			body["name"] = args[0]
			if space != "" {
				sp, err := resolveSpace(cmd.Context(), c, space)
				if err != nil {
					return err
				}
				body["space_id"] = sp.ID
			}
			var s client.Stack
			raw, err := c.JSON(cmd.Context(), http.MethodPost, "/stacks", nil, body, &s)
			if err != nil {
				return err
			}
			if a.json() {
				return printJSON(a.out, raw)
			}
			fmt.Fprintf(a.out, "Created stack %s (%s)\n", s.Name, s.ID)
			return nil
		},
	}
	f.register(c, false)
	c.Flags().StringVar(&space, "space", "", "target space (id or name); default: the instance's default space")
	_ = c.MarkFlagRequired("repo-url")
	_ = c.MarkFlagRequired("tool-version")
	return c
}

func stacksUpdate(a *app) *cobra.Command {
	var f stackFlags
	var space string
	c := &cobra.Command{
		Use:   "update <stack>",
		Short: "Update a stack's settings (only the flags given are changed)",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			c, err := a.client()
			if err != nil {
				return err
			}
			body, err := f.body(cmd, a.in)
			if err != nil {
				return err
			}
			if len(body) == 0 {
				return fmt.Errorf("nothing to update: pass at least one flag")
			}
			s, err := resolveStack(cmd.Context(), c, args[0], space)
			if err != nil {
				return err
			}
			var out client.Stack
			raw, err := c.JSON(cmd.Context(), http.MethodPatch, "/stacks/"+s.ID, nil, body, &out)
			if err != nil {
				return err
			}
			if a.json() {
				return printJSON(a.out, raw)
			}
			fmt.Fprintf(a.out, "Updated stack %s (%s)\n", out.Name, out.ID)
			return nil
		},
	}
	f.register(c, true)
	c.Flags().StringVar(&space, "space", "", "space of the stack, when its name is not unique")
	return c
}

func stacksDelete(a *app) *cobra.Command {
	var space string
	var yes bool
	c := &cobra.Command{
		Use:   "delete <stack>",
		Short: "Delete a stack and its environments (refused while a run is active)",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if !yes {
				return fmt.Errorf("deleting a stack deletes its environments: pass --yes to confirm")
			}
			c, err := a.client()
			if err != nil {
				return err
			}
			s, err := resolveStack(cmd.Context(), c, args[0], space)
			if err != nil {
				return err
			}
			if _, err := c.Do(cmd.Context(), http.MethodDelete, "/stacks/"+s.ID, nil, nil); err != nil {
				return err
			}
			if !a.json() {
				fmt.Fprintf(a.out, "Deleted stack %s (%s)\n", s.Name, s.ID)
			}
			return nil
		},
	}
	c.Flags().StringVar(&space, "space", "", "space of the stack, when its name is not unique")
	c.Flags().BoolVar(&yes, "yes", false, "confirm the deletion")
	return c
}

// filterBySpace keeps the stacks of one space, both decoded and as the API's raw JSON objects.
func filterBySpace(stacks []client.Stack, raw []byte, spaceID string) ([]client.Stack, []byte, error) {
	var objs []json.RawMessage
	if err := json.Unmarshal(raw, &objs); err != nil || len(objs) != len(stacks) {
		return nil, nil, fmt.Errorf("decode stack list: unexpected response")
	}
	keptStacks := []client.Stack{}
	keptRaw := []json.RawMessage{}
	for i, s := range stacks {
		if s.SpaceID == spaceID {
			keptStacks = append(keptStacks, s)
			keptRaw = append(keptRaw, objs[i])
		}
	}
	out, err := json.Marshal(keptRaw)
	return keptStacks, out, err
}
