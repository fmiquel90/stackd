package cmd

import (
	"fmt"
	"net/http"
	"strconv"

	"github.com/spf13/cobra"

	"github.com/fmiquel90/stackd/cli/internal/client"
)

func newEnvsCmd(a *app) *cobra.Command {
	cmd := &cobra.Command{
		Use:     "envs",
		Aliases: []string{"env", "environments"},
		Short:   "Manage a stack's environments",
		Long: `Manage environments. An environment is named by its id or as <stack>/<environment>
(the stack by name or id); --space disambiguates a stack name used in several spaces.`,
	}
	cmd.AddCommand(envsList(a), envsGet(a), envsCreate(a), envsUpdate(a), envsDelete(a))
	return cmd
}

func envsList(a *app) *cobra.Command {
	var space string
	c := &cobra.Command{
		Use:   "list <stack>",
		Short: "List a stack's environments",
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
			var envs []client.Environment
			raw, err := c.JSON(cmd.Context(), http.MethodGet, "/stacks/"+s.ID+"/environments", nil, nil, &envs)
			if err != nil {
				return err
			}
			if a.json() {
				return printJSON(a.out, raw)
			}
			rows := make([][]string, 0, len(envs))
			for _, e := range envs {
				rows = append(rows, []string{
					e.ID, e.Name, e.Tier, e.Branch, yesNo(e.Autodeploy), yesNo(e.Protected),
					e.DriftStatus, yesNo(e.Stale),
				})
			}
			return table(a.out, []string{"ID", "NAME", "TIER", "BRANCH", "AUTODEPLOY", "PROTECTED", "DRIFT", "STALE"}, rows)
		},
	}
	c.Flags().StringVar(&space, "space", "", "space of the stack, when its name is not unique")
	return c
}

func envsGet(a *app) *cobra.Command {
	var space string
	c := &cobra.Command{
		Use:   "get <stack>/<env> | <env-id>",
		Short: "Show an environment",
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
			if a.json() {
				raw, err := c.Do(cmd.Context(), http.MethodGet, "/environments/"+e.ID, nil, nil)
				if err != nil {
					return err
				}
				return printJSON(a.out, raw)
			}
			return fields(a.out, [][2]string{
				{"ID", e.ID},
				{"Name", e.Name},
				{"Stack", e.StackID},
				{"Tier", e.Tier},
				{"Branch", e.Branch},
				{"Autodeploy", yesNo(e.Autodeploy)},
				{"Protected", yesNo(e.Protected)},
				{"Four-eyes", yesNo(e.RequireSecondPairOfEyes)},
				{"Managed state", yesNo(e.ManagedState)},
				{"Allow mock apply", yesNo(e.AllowMockApply)},
				{"Allow fallback apply", yesNo(e.AllowFallbackApply)},
				{"Drift check", yesNo(e.DriftCheckEnabled)},
				{"Drift status", e.DriftStatus},
				{"Head", short(e.HeadSHA)},
				{"Stale", yesNo(e.Stale)},
				{"Locked", yesNo(e.Locked)},
				{"Labels", labelsString(e.Labels)},
				{"Position", strconv.Itoa(e.Position)},
			})
		},
	}
	c.Flags().StringVar(&space, "space", "", "space of the stack, when its name is not unique")
	return c
}

// envFlags are the writable environment fields (EnvironmentCreate / EnvironmentUpdate).
type envFlags struct {
	name, tier, branch, backendConfigFile                                               string
	autodeploy, protected, fourEyes, managedState, allowMock, allowFallback, driftCheck bool
	labels                                                                              []string
	position                                                                            int
}

func (f *envFlags) register(cmd *cobra.Command, forUpdate bool) {
	fl := cmd.Flags()
	if forUpdate {
		fl.StringVar(&f.name, "name", "", "rename the environment")
	}
	fl.StringVar(&f.tier, "tier", "", "tier (a name from the tier catalog)")
	fl.StringVar(&f.branch, "branch", "", "git branch tracked by the environment")
	fl.BoolVar(&f.autodeploy, "autodeploy", false, "apply automatically after a successful plan")
	fl.BoolVar(&f.protected, "protected", false, "force a confirmation and four-eyes review")
	fl.BoolVar(&f.fourEyes, "four-eyes", false, "require a second person to confirm")
	fl.BoolVar(&f.managedState, "managed-state", true, "state held by Stackd's HTTP backend")
	fl.BoolVar(&f.allowMock, "allow-mock-apply", false, "allow applying a plan that consumed mocks")
	fl.BoolVar(&f.allowFallback, "allow-fallback-apply", false, "allow applying a plan that used a secret fallback")
	fl.BoolVar(&f.driftCheck, "drift-check", true, "include in scheduled drift detection")
	fl.StringVar(&f.backendConfigFile, "backend-config-file", "", "backend config file (unmanaged state)")
	fl.StringArrayVar(&f.labels, "label", nil, "label key=value (repeatable; replaces all labels)")
	fl.IntVar(&f.position, "position", 0, "display order within the stack")
}

// body sends only the flags the user set: PATCH semantics on update, API defaults on create.
func (f *envFlags) body(cmd *cobra.Command) (map[string]any, error) {
	b := map[string]any{}
	ch := cmd.Flags().Changed
	str := map[string]struct {
		key string
		val string
	}{
		"name": {"name", f.name}, "tier": {"tier", f.tier}, "branch": {"branch", f.branch},
		"backend-config-file": {"backend_config_file", f.backendConfigFile},
	}
	for flag, kv := range str {
		if ch(flag) {
			b[kv.key] = kv.val
		}
	}
	bools := map[string]struct {
		key string
		val bool
	}{
		"autodeploy": {"autodeploy", f.autodeploy}, "protected": {"protected", f.protected},
		"four-eyes": {"require_second_pair_of_eyes", f.fourEyes}, "managed-state": {"managed_state", f.managedState},
		"allow-mock-apply": {"allow_mock_apply", f.allowMock}, "allow-fallback-apply": {"allow_fallback_apply", f.allowFallback},
		"drift-check": {"drift_check_enabled", f.driftCheck},
	}
	for flag, kv := range bools {
		if ch(flag) {
			b[kv.key] = kv.val
		}
	}
	if ch("position") {
		b["position"] = f.position
	}
	if ch("label") {
		labels, err := parseLabels(f.labels)
		if err != nil {
			return nil, err
		}
		b["labels"] = labels
	}
	return b, nil
}

func envsCreate(a *app) *cobra.Command {
	var f envFlags
	var space string
	c := &cobra.Command{
		Use:     "create <stack> <name>",
		Short:   "Create an environment in a stack (writer)",
		Example: `  stackd envs create network prod --tier prod --branch main --protected`,
		Args:    cobra.ExactArgs(2),
		RunE: func(cmd *cobra.Command, args []string) error {
			c, err := a.client()
			if err != nil {
				return err
			}
			body, err := f.body(cmd)
			if err != nil {
				return err
			}
			body["name"] = args[1]
			s, err := resolveStack(cmd.Context(), c, args[0], space)
			if err != nil {
				return err
			}
			var e client.Environment
			raw, err := c.JSON(cmd.Context(), http.MethodPost, "/stacks/"+s.ID+"/environments", nil, body, &e)
			if err != nil {
				return err
			}
			if a.json() {
				return printJSON(a.out, raw)
			}
			fmt.Fprintf(a.out, "Created environment %s/%s (%s)\n", s.Name, e.Name, e.ID)
			return nil
		},
	}
	f.register(c, false)
	c.Flags().StringVar(&space, "space", "", "space of the stack, when its name is not unique")
	_ = c.MarkFlagRequired("tier")
	_ = c.MarkFlagRequired("branch")
	return c
}

func envsUpdate(a *app) *cobra.Command {
	var f envFlags
	var space string
	c := &cobra.Command{
		Use:   "update <stack>/<env> | <env-id>",
		Short: "Update an environment (only the flags given are changed)",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			c, err := a.client()
			if err != nil {
				return err
			}
			body, err := f.body(cmd)
			if err != nil {
				return err
			}
			if len(body) == 0 {
				return fmt.Errorf("nothing to update: pass at least one flag")
			}
			e, err := resolveEnv(cmd.Context(), c, args[0], space)
			if err != nil {
				return err
			}
			var out client.Environment
			raw, err := c.JSON(cmd.Context(), http.MethodPatch, "/environments/"+e.ID, nil, body, &out)
			if err != nil {
				return err
			}
			if a.json() {
				return printJSON(a.out, raw)
			}
			fmt.Fprintf(a.out, "Updated environment %s (%s)\n", out.Name, out.ID)
			return nil
		},
	}
	f.register(c, true)
	c.Flags().StringVar(&space, "space", "", "space of the stack, when its name is not unique")
	return c
}

func envsDelete(a *app) *cobra.Command {
	var space string
	var yes bool
	c := &cobra.Command{
		Use:   "delete <stack>/<env> | <env-id>",
		Short: "Delete an environment (refused while a run is active; runs no destroy)",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if !yes {
				return fmt.Errorf("pass --yes to confirm the deletion")
			}
			c, err := a.client()
			if err != nil {
				return err
			}
			e, err := resolveEnv(cmd.Context(), c, args[0], space)
			if err != nil {
				return err
			}
			if _, err := c.Do(cmd.Context(), http.MethodDelete, "/environments/"+e.ID, nil, nil); err != nil {
				return err
			}
			if !a.json() {
				fmt.Fprintf(a.out, "Deleted environment %s (%s)\n", e.Name, e.ID)
			}
			return nil
		},
	}
	c.Flags().StringVar(&space, "space", "", "space of the stack, when its name is not unique")
	c.Flags().BoolVar(&yes, "yes", false, "confirm the deletion")
	return c
}
