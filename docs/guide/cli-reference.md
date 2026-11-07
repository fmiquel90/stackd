# CLI reference

<!-- Generated from the command tree by cli/internal/docgen — do not edit by hand.
     Regenerate with: task cli-docs -->

Every `stackd` command and flag, as `stackd <command> --help` prints them. For a guided tour
(install, authentication, common tasks, CI), read the [command-line client guide](cli.md).

## stackd

Command-line client for the Stackd API

**Synopsis**

stackd drives a Stackd instance over its REST API: spaces, stacks, environments,
runs and configuration export/import.

Authentication: a service-account API token (stkd_…) or any bearer token the API accepts.
Set STACKD_URL and STACKD_TOKEN, or pass --url and --token. The token is never printed.

A command can only do what its token is allowed to do. In particular a service account never
confirms an apply or a destroy: that stays a named human's decision (SPECS §23).

**Options**

```
      --allow-http      allow a plain http URL to a non-loopback host (env STACKD_ALLOW_HTTP=1)
  -h, --help            help for stackd
  -o, --output string   output format: text or json (env STACKD_OUTPUT) (default "text")
      --token string    API token (env STACKD_TOKEN)
      --url string      Stackd instance URL (env STACKD_URL)
```

**SEE ALSO**

* [stackd config](#stackd-config)	 - Export and import a space's configuration (stackd.config, SPECS §22)
* [stackd envs](#stackd-envs)	 - Manage a stack's environments
* [stackd runs](#stackd-runs)	 - Trigger plans, follow runs, read logs
* [stackd spaces](#stackd-spaces)	 - List and create spaces
* [stackd stacks](#stackd-stacks)	 - Manage stacks
* [stackd version](#stackd-version)	 - Print the CLI version

## stackd config

Export and import a space's configuration (stackd.config, SPECS §22)

**Synopsis**

A space's configuration as a stackd.config JSON document: stacks, environments,
variables, variable sets, dependencies, hooks, notifications. Never a secret value.

Import merges by name: it creates what is missing and updates what differs. It never deletes,
and it applies all or nothing. Both commands need the space admin role.

**Options**

```
  -h, --help   help for config
```

**Options inherited from parent commands**

```
      --allow-http      allow a plain http URL to a non-loopback host (env STACKD_ALLOW_HTTP=1)
  -o, --output string   output format: text or json (env STACKD_OUTPUT) (default "text")
      --token string    API token (env STACKD_TOKEN)
      --url string      Stackd instance URL (env STACKD_URL)
```

**SEE ALSO**

* [stackd](#stackd)	 - Command-line client for the Stackd API
* [stackd config export](#stackd-config-export)	 - Write a space's configuration document
* [stackd config import](#stackd-config-import)	 - Merge a configuration document into a space

## stackd config export

Write a space's configuration document

```
stackd config export <space> [flags]
```

**Examples**

```
  stackd config export platform -f platform.json
```

**Options**

```
  -f, --file string   write to this file instead of stdout
  -h, --help          help for export
```

**Options inherited from parent commands**

```
      --allow-http      allow a plain http URL to a non-loopback host (env STACKD_ALLOW_HTTP=1)
  -o, --output string   output format: text or json (env STACKD_OUTPUT) (default "text")
      --token string    API token (env STACKD_TOKEN)
      --url string      Stackd instance URL (env STACKD_URL)
```

**SEE ALSO**

* [stackd config](#stackd-config)	 - Export and import a space's configuration (stackd.config, SPECS §22)

## stackd config import

Merge a configuration document into a space

```
stackd config import <space> <file|-> [flags]
```

**Examples**

```
  stackd config import platform platform.json --dry-run
  stackd config import platform platform.json
```

**Options**

```
      --dry-run   compute the report and roll back
  -h, --help      help for import
```

**Options inherited from parent commands**

```
      --allow-http      allow a plain http URL to a non-loopback host (env STACKD_ALLOW_HTTP=1)
  -o, --output string   output format: text or json (env STACKD_OUTPUT) (default "text")
      --token string    API token (env STACKD_TOKEN)
      --url string      Stackd instance URL (env STACKD_URL)
```

**SEE ALSO**

* [stackd config](#stackd-config)	 - Export and import a space's configuration (stackd.config, SPECS §22)

## stackd envs

Manage a stack's environments

**Synopsis**

Manage environments. An environment is named by its id or as <stack>/<environment>
(the stack by name or id); --space disambiguates a stack name used in several spaces.

**Options**

```
  -h, --help   help for envs
```

**Options inherited from parent commands**

```
      --allow-http      allow a plain http URL to a non-loopback host (env STACKD_ALLOW_HTTP=1)
  -o, --output string   output format: text or json (env STACKD_OUTPUT) (default "text")
      --token string    API token (env STACKD_TOKEN)
      --url string      Stackd instance URL (env STACKD_URL)
```

**SEE ALSO**

* [stackd](#stackd)	 - Command-line client for the Stackd API
* [stackd envs create](#stackd-envs-create)	 - Create an environment in a stack (writer)
* [stackd envs delete](#stackd-envs-delete)	 - Delete an environment (refused while a run is active; runs no destroy)
* [stackd envs get](#stackd-envs-get)	 - Show an environment
* [stackd envs list](#stackd-envs-list)	 - List a stack's environments
* [stackd envs update](#stackd-envs-update)	 - Update an environment (only the flags given are changed)

## stackd envs create

Create an environment in a stack (writer)

```
stackd envs create <stack> <name> [flags]
```

**Examples**

```
  stackd envs create network prod --tier prod --branch main --protected
```

**Options**

```
      --allow-fallback-apply         allow applying a plan that used a secret fallback
      --allow-mock-apply             allow applying a plan that consumed mocks
      --autodeploy                   apply automatically after a successful plan
      --backend-config-file string   backend config file (unmanaged state)
      --branch string                git branch tracked by the environment
      --drift-check                  include in scheduled drift detection (default true)
      --four-eyes                    require a second person to confirm
  -h, --help                         help for create
      --label stringArray            label key=value (repeatable; replaces all labels)
      --managed-state                state held by Stackd's HTTP backend (default true)
      --position int                 display order within the stack
      --protected                    force a confirmation and four-eyes review
      --space string                 space of the stack, when its name is not unique
      --tier string                  tier (a name from the tier catalog)
```

**Options inherited from parent commands**

```
      --allow-http      allow a plain http URL to a non-loopback host (env STACKD_ALLOW_HTTP=1)
  -o, --output string   output format: text or json (env STACKD_OUTPUT) (default "text")
      --token string    API token (env STACKD_TOKEN)
      --url string      Stackd instance URL (env STACKD_URL)
```

**SEE ALSO**

* [stackd envs](#stackd-envs)	 - Manage a stack's environments

## stackd envs delete

Delete an environment (refused while a run is active; runs no destroy)

```
stackd envs delete <stack>/<env> | <env-id> [flags]
```

**Options**

```
  -h, --help           help for delete
      --space string   space of the stack, when its name is not unique
      --yes            confirm the deletion
```

**Options inherited from parent commands**

```
      --allow-http      allow a plain http URL to a non-loopback host (env STACKD_ALLOW_HTTP=1)
  -o, --output string   output format: text or json (env STACKD_OUTPUT) (default "text")
      --token string    API token (env STACKD_TOKEN)
      --url string      Stackd instance URL (env STACKD_URL)
```

**SEE ALSO**

* [stackd envs](#stackd-envs)	 - Manage a stack's environments

## stackd envs get

Show an environment

```
stackd envs get <stack>/<env> | <env-id> [flags]
```

**Options**

```
  -h, --help           help for get
      --space string   space of the stack, when its name is not unique
```

**Options inherited from parent commands**

```
      --allow-http      allow a plain http URL to a non-loopback host (env STACKD_ALLOW_HTTP=1)
  -o, --output string   output format: text or json (env STACKD_OUTPUT) (default "text")
      --token string    API token (env STACKD_TOKEN)
      --url string      Stackd instance URL (env STACKD_URL)
```

**SEE ALSO**

* [stackd envs](#stackd-envs)	 - Manage a stack's environments

## stackd envs list

List a stack's environments

```
stackd envs list <stack> [flags]
```

**Options**

```
  -h, --help           help for list
      --space string   space of the stack, when its name is not unique
```

**Options inherited from parent commands**

```
      --allow-http      allow a plain http URL to a non-loopback host (env STACKD_ALLOW_HTTP=1)
  -o, --output string   output format: text or json (env STACKD_OUTPUT) (default "text")
      --token string    API token (env STACKD_TOKEN)
      --url string      Stackd instance URL (env STACKD_URL)
```

**SEE ALSO**

* [stackd envs](#stackd-envs)	 - Manage a stack's environments

## stackd envs update

Update an environment (only the flags given are changed)

```
stackd envs update <stack>/<env> | <env-id> [flags]
```

**Options**

```
      --allow-fallback-apply         allow applying a plan that used a secret fallback
      --allow-mock-apply             allow applying a plan that consumed mocks
      --autodeploy                   apply automatically after a successful plan
      --backend-config-file string   backend config file (unmanaged state)
      --branch string                git branch tracked by the environment
      --clear-labels                 remove every label
      --drift-check                  include in scheduled drift detection (default true)
      --four-eyes                    require a second person to confirm
  -h, --help                         help for update
      --label stringArray            label key=value (repeatable; replaces all labels)
      --managed-state                state held by Stackd's HTTP backend (default true)
      --name string                  rename the environment
      --position int                 display order within the stack
      --protected                    force a confirmation and four-eyes review
      --space string                 space of the stack, when its name is not unique
      --tier string                  tier (a name from the tier catalog)
```

**Options inherited from parent commands**

```
      --allow-http      allow a plain http URL to a non-loopback host (env STACKD_ALLOW_HTTP=1)
  -o, --output string   output format: text or json (env STACKD_OUTPUT) (default "text")
      --token string    API token (env STACKD_TOKEN)
      --url string      Stackd instance URL (env STACKD_URL)
```

**SEE ALSO**

* [stackd envs](#stackd-envs)	 - Manage a stack's environments

## stackd runs

Trigger plans, follow runs, read logs

**Synopsis**

Runs on an environment. An environment is named by its id or as <stack>/<environment>.

There is no 'confirm' command: applying a plan is a named human's decision, taken in the UI
(a service-account token can never confirm, SPECS §23).

**Options**

```
  -h, --help   help for runs
```

**Options inherited from parent commands**

```
      --allow-http      allow a plain http URL to a non-loopback host (env STACKD_ALLOW_HTTP=1)
  -o, --output string   output format: text or json (env STACKD_OUTPUT) (default "text")
      --token string    API token (env STACKD_TOKEN)
      --url string      Stackd instance URL (env STACKD_URL)
```

**SEE ALSO**

* [stackd](#stackd)	 - Command-line client for the Stackd API
* [stackd runs cancel](#stackd-runs-cancel)	 - Cancel a queued or running run
* [stackd runs discard](#stackd-runs-discard)	 - Discard a plan awaiting confirmation
* [stackd runs get](#stackd-runs-get)	 - Show a run
* [stackd runs list](#stackd-runs-list)	 - List an environment's runs, newest first
* [stackd runs logs](#stackd-runs-logs)	 - Print a run's logs (ANSI kept)
* [stackd runs plan](#stackd-runs-plan)	 - Trigger a plan (tracked, or destroy) on an environment

## stackd runs cancel

Cancel a queued or running run

```
stackd runs cancel <run-id> [flags]
```

**Options**

```
  -h, --help   help for cancel
```

**Options inherited from parent commands**

```
      --allow-http      allow a plain http URL to a non-loopback host (env STACKD_ALLOW_HTTP=1)
  -o, --output string   output format: text or json (env STACKD_OUTPUT) (default "text")
      --token string    API token (env STACKD_TOKEN)
      --url string      Stackd instance URL (env STACKD_URL)
```

**SEE ALSO**

* [stackd runs](#stackd-runs)	 - Trigger plans, follow runs, read logs

## stackd runs discard

Discard a plan awaiting confirmation

```
stackd runs discard <run-id> [flags]
```

**Options**

```
  -h, --help   help for discard
```

**Options inherited from parent commands**

```
      --allow-http      allow a plain http URL to a non-loopback host (env STACKD_ALLOW_HTTP=1)
  -o, --output string   output format: text or json (env STACKD_OUTPUT) (default "text")
      --token string    API token (env STACKD_TOKEN)
      --url string      Stackd instance URL (env STACKD_URL)
```

**SEE ALSO**

* [stackd runs](#stackd-runs)	 - Trigger plans, follow runs, read logs

## stackd runs get

Show a run

```
stackd runs get <run-id> [flags]
```

**Options**

```
  -h, --help   help for get
```

**Options inherited from parent commands**

```
      --allow-http      allow a plain http URL to a non-loopback host (env STACKD_ALLOW_HTTP=1)
  -o, --output string   output format: text or json (env STACKD_OUTPUT) (default "text")
      --token string    API token (env STACKD_TOKEN)
      --url string      Stackd instance URL (env STACKD_URL)
```

**SEE ALSO**

* [stackd runs](#stackd-runs)	 - Trigger plans, follow runs, read logs

## stackd runs list

List an environment's runs, newest first

```
stackd runs list <stack>/<env> | <env-id> [flags]
```

**Options**

```
  -h, --help           help for list
      --limit int      rows shown in text output (0 = all) (default 20)
      --space string   space of the stack, when its name is not unique
```

**Options inherited from parent commands**

```
      --allow-http      allow a plain http URL to a non-loopback host (env STACKD_ALLOW_HTTP=1)
  -o, --output string   output format: text or json (env STACKD_OUTPUT) (default "text")
      --token string    API token (env STACKD_TOKEN)
      --url string      Stackd instance URL (env STACKD_URL)
```

**SEE ALSO**

* [stackd runs](#stackd-runs)	 - Trigger plans, follow runs, read logs

## stackd runs logs

Print a run's logs (ANSI kept)

**Synopsis**

Print a run's logs. With --follow, keep printing new lines until the run ends or awaits
confirmation; the exit status is then the run's, as for 'runs plan --wait' (0, or 2 if it failed,
was canceled or discarded). With --output json and --follow, log lines go to stderr and stdout
carries only the run's final JSON.

```
stackd runs logs <run-id> [flags]
```

**Options**

```
  -f, --follow             keep printing new lines until the run ends or awaits confirmation
  -h, --help               help for logs
      --timeout duration   with --follow, give up after this long (0 = no limit) (default 30m0s)
```

**Options inherited from parent commands**

```
      --allow-http      allow a plain http URL to a non-loopback host (env STACKD_ALLOW_HTTP=1)
  -o, --output string   output format: text or json (env STACKD_OUTPUT) (default "text")
      --token string    API token (env STACKD_TOKEN)
      --url string      Stackd instance URL (env STACKD_URL)
```

**SEE ALSO**

* [stackd runs](#stackd-runs)	 - Trigger plans, follow runs, read logs

## stackd runs plan

Trigger a plan (tracked, or destroy) on an environment

**Synopsis**

Trigger a plan. The run is queued; a worker picks it up. Applying it needs a human
confirmation in the UI (or the environment's autodeploy).

With --wait the command follows the run until it awaits confirmation (unconfirmed) or ends, and
exits 0 if it reached unconfirmed or finished, 2 if it failed, was canceled or discarded.

```
stackd runs plan <stack>/<env> | <env-id> [flags]
```

**Examples**

```
  stackd runs plan network/prod --wait --logs
  stackd runs plan network/dev --target module.vpc --output json
```

**Options**

```
      --commit string         plan this commit instead of the branch head
      --destroy               a destroy plan (confirming it needs the destroy right)
  -h, --help                  help for plan
      --logs                  with --wait, stream the run's logs (to stderr with --output json)
      --replace stringArray   resource address passed as -replace (repeatable, tracked runs only)
      --space string          space of the stack, when its name is not unique
      --target stringArray    resource address passed as -target (repeatable)
      --timeout duration      with --wait, give up after this long (0 = no limit) (default 30m0s)
      --wait                  follow the run until it awaits confirmation or ends
      --with-downstream       start a cascade run group through dependent environments
```

**Options inherited from parent commands**

```
      --allow-http      allow a plain http URL to a non-loopback host (env STACKD_ALLOW_HTTP=1)
  -o, --output string   output format: text or json (env STACKD_OUTPUT) (default "text")
      --token string    API token (env STACKD_TOKEN)
      --url string      Stackd instance URL (env STACKD_URL)
```

**SEE ALSO**

* [stackd runs](#stackd-runs)	 - Trigger plans, follow runs, read logs

## stackd spaces

List and create spaces

**Options**

```
  -h, --help   help for spaces
```

**Options inherited from parent commands**

```
      --allow-http      allow a plain http URL to a non-loopback host (env STACKD_ALLOW_HTTP=1)
  -o, --output string   output format: text or json (env STACKD_OUTPUT) (default "text")
      --token string    API token (env STACKD_TOKEN)
      --url string      Stackd instance URL (env STACKD_URL)
```

**SEE ALSO**

* [stackd](#stackd)	 - Command-line client for the Stackd API
* [stackd spaces create](#stackd-spaces-create)	 - Create a space (instance admin only)
* [stackd spaces list](#stackd-spaces-list)	 - List the spaces this token can reach

## stackd spaces create

Create a space (instance admin only)

**Synopsis**

Create a space. The API reserves this to an instance admin: a service-account token,
capped at writer, gets 403 (SPECS §23). The creator becomes the space's admin.

```
stackd spaces create <name> [flags]
```

**Options**

```
      --description string   space description
  -h, --help                 help for create
```

**Options inherited from parent commands**

```
      --allow-http      allow a plain http URL to a non-loopback host (env STACKD_ALLOW_HTTP=1)
  -o, --output string   output format: text or json (env STACKD_OUTPUT) (default "text")
      --token string    API token (env STACKD_TOKEN)
      --url string      Stackd instance URL (env STACKD_URL)
```

**SEE ALSO**

* [stackd spaces](#stackd-spaces)	 - List and create spaces

## stackd spaces list

List the spaces this token can reach

```
stackd spaces list [flags]
```

**Options**

```
  -h, --help   help for list
```

**Options inherited from parent commands**

```
      --allow-http      allow a plain http URL to a non-loopback host (env STACKD_ALLOW_HTTP=1)
  -o, --output string   output format: text or json (env STACKD_OUTPUT) (default "text")
      --token string    API token (env STACKD_TOKEN)
      --url string      Stackd instance URL (env STACKD_URL)
```

**SEE ALSO**

* [stackd spaces](#stackd-spaces)	 - List and create spaces

## stackd stacks

Manage stacks

**Options**

```
  -h, --help   help for stacks
```

**Options inherited from parent commands**

```
      --allow-http      allow a plain http URL to a non-loopback host (env STACKD_ALLOW_HTTP=1)
  -o, --output string   output format: text or json (env STACKD_OUTPUT) (default "text")
      --token string    API token (env STACKD_TOKEN)
      --url string      Stackd instance URL (env STACKD_URL)
```

**SEE ALSO**

* [stackd](#stackd)	 - Command-line client for the Stackd API
* [stackd stacks create](#stackd-stacks-create)	 - Create a stack in a space (writer in that space)
* [stackd stacks delete](#stackd-stacks-delete)	 - Delete a stack and its environments (refused while a run is active)
* [stackd stacks get](#stackd-stacks-get)	 - Show a stack (id or name)
* [stackd stacks list](#stackd-stacks-list)	 - List stacks
* [stackd stacks update](#stackd-stacks-update)	 - Update a stack's settings (only the flags given are changed)

## stackd stacks create

Create a stack in a space (writer in that space)

```
stackd stacks create <name> [flags]
```

**Examples**

```
  stackd stacks create network --space platform \
    --repo-url https://github.com/acme/infra.git --project-root network --tool-version 1.10
```

**Options**

```
      --description string      description
  -h, --help                    help for create
      --label stringArray       label key=value (repeatable; replaces all labels)
      --project-root string     directory of the root module in the repo (default ".")
      --repo-auth-kind string   repository auth: none, token or deploy_key
      --repo-secret-stdin       read the repository token / deploy key from stdin (never pass a secret as a flag)
      --repo-url string         git repository URL
      --space string            target space (id or name); default: the instance's default space
      --tool string             opentofu (default) or terraform
      --tool-version string     MAJOR.MINOR or MAJOR.MINOR.PATCH
```

**Options inherited from parent commands**

```
      --allow-http      allow a plain http URL to a non-loopback host (env STACKD_ALLOW_HTTP=1)
  -o, --output string   output format: text or json (env STACKD_OUTPUT) (default "text")
      --token string    API token (env STACKD_TOKEN)
      --url string      Stackd instance URL (env STACKD_URL)
```

**SEE ALSO**

* [stackd stacks](#stackd-stacks)	 - Manage stacks

## stackd stacks delete

Delete a stack and its environments (refused while a run is active)

```
stackd stacks delete <stack> [flags]
```

**Options**

```
  -h, --help           help for delete
      --space string   space of the stack, when its name is not unique
      --yes            confirm the deletion
```

**Options inherited from parent commands**

```
      --allow-http      allow a plain http URL to a non-loopback host (env STACKD_ALLOW_HTTP=1)
  -o, --output string   output format: text or json (env STACKD_OUTPUT) (default "text")
      --token string    API token (env STACKD_TOKEN)
      --url string      Stackd instance URL (env STACKD_URL)
```

**SEE ALSO**

* [stackd stacks](#stackd-stacks)	 - Manage stacks

## stackd stacks get

Show a stack (id or name)

```
stackd stacks get <stack> [flags]
```

**Options**

```
  -h, --help           help for get
      --space string   space of the stack, when its name is not unique
```

**Options inherited from parent commands**

```
      --allow-http      allow a plain http URL to a non-loopback host (env STACKD_ALLOW_HTTP=1)
  -o, --output string   output format: text or json (env STACKD_OUTPUT) (default "text")
      --token string    API token (env STACKD_TOKEN)
      --url string      Stackd instance URL (env STACKD_URL)
```

**SEE ALSO**

* [stackd stacks](#stackd-stacks)	 - Manage stacks

## stackd stacks list

List stacks

```
stackd stacks list [flags]
```

**Options**

```
  -h, --help           help for list
      --space string   only stacks of this space (id or name)
```

**Options inherited from parent commands**

```
      --allow-http      allow a plain http URL to a non-loopback host (env STACKD_ALLOW_HTTP=1)
  -o, --output string   output format: text or json (env STACKD_OUTPUT) (default "text")
      --token string    API token (env STACKD_TOKEN)
      --url string      Stackd instance URL (env STACKD_URL)
```

**SEE ALSO**

* [stackd stacks](#stackd-stacks)	 - Manage stacks

## stackd stacks update

Update a stack's settings (only the flags given are changed)

```
stackd stacks update <stack> [flags]
```

**Options**

```
      --clear-labels            remove every label
      --description string      description
  -h, --help                    help for update
      --label stringArray       label key=value (repeatable; replaces all labels)
      --name string             rename the stack
      --project-root string     directory of the root module in the repo (default ".")
      --repo-auth-kind string   repository auth: none, token or deploy_key
      --repo-secret-stdin       read the repository token / deploy key from stdin (never pass a secret as a flag)
      --repo-url string         git repository URL
      --space string            space of the stack, when its name is not unique
      --tool string             opentofu (default) or terraform
      --tool-version string     MAJOR.MINOR or MAJOR.MINOR.PATCH
```

**Options inherited from parent commands**

```
      --allow-http      allow a plain http URL to a non-loopback host (env STACKD_ALLOW_HTTP=1)
  -o, --output string   output format: text or json (env STACKD_OUTPUT) (default "text")
      --token string    API token (env STACKD_TOKEN)
      --url string      Stackd instance URL (env STACKD_URL)
```

**SEE ALSO**

* [stackd stacks](#stackd-stacks)	 - Manage stacks

## stackd version

Print the CLI version

```
stackd version [flags]
```

**Options**

```
  -h, --help   help for version
```

**Options inherited from parent commands**

```
      --allow-http      allow a plain http URL to a non-loopback host (env STACKD_ALLOW_HTTP=1)
  -o, --output string   output format: text or json (env STACKD_OUTPUT) (default "text")
      --token string    API token (env STACKD_TOKEN)
      --url string      Stackd instance URL (env STACKD_URL)
```

**SEE ALSO**

* [stackd](#stackd)	 - Command-line client for the Stackd API
