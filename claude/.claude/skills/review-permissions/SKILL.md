---
name: review-permissions
description: Security review of permissions.allow rules in Claude Code settings.json
---

TRIGGER when: `permissions.allow` rules are added or modified in any `.claude/settings.json`, the code-review skill detects permission scope changes, or the user asks to review permission rules or allowed commands.

Review each `permissions.allow` entry against the security checklist below. The goal is to ensure allowed commands cannot be exploited for code execution, data exfiltration, secret exposure, or privilege escalation.

## Steps

1. Read the `permissions.allow` array from the settings.json being reviewed
2. For each rule, evaluate against every applicable checklist item
3. Report findings with the rule, the attack vector, and a concrete fix

## Checklist

### Command chaining and injection

1. **Compound command injection** — Does the glob pattern allow shell
   metacharacters (`; | & \`` `) in the matched portion? `Bash(ls:*)`
   may match `ls; rm -rf /` depending on how Claude Code evaluates the
   glob. Prefer explicit commands (`Bash(ls)`, `Bash(ls -la)`) over
   open-ended globs.

2. **Newline injection** — Could a multi-line command match the pattern?
   `git status\nrm -rf /` looks like `git status` to a line-based
   matcher. If the permission system doesn't reject newlines, flag
   any pattern that relies on prefix matching.

3. **Shell expansion** — Does the allowed command permit `$VAR`,
   `${VAR}`, or `$(cmd)` expansion? `echo $AWS_SECRET_ACCESS_KEY`
   would match a rule allowing `Bash(echo:*)`. Flag rules that allow
   commands which accept arbitrary arguments containing `$`.

### Environment and path manipulation

4. **Env var prefix injection** — Can `LD_PRELOAD=`, `PATH=`, or other
   env var prefixes be prepended to hijack an allowed command?
   `LD_PRELOAD=/tmp/evil.so cat file` looks like `cat` but executes
   arbitrary code. Flag rules that don't account for env var prefixes.

5. **Absolute path evasion** — Could `/usr/bin/env bash -c 'payload'`
   match a rule allowing `env`? Or could `/bin/cat` match differently
   than `cat`? Check whether rules are sensitive to path prefixes.

### Flag injection

6. **Dangerous flag injection** — Does `*` in the pattern allow flags
   that change a read-only command into a write or execute operation?
   Examples:
   - `git log --output=/path` writes to arbitrary files
   - `git diff --ext-diff` executes external programs
   - `git branch -D main` deletes branches
   - `go env -w KEY=VALUE` writes persistent config
   - Flag abbreviations: git accepts `--out` for `--output`
   Flag any glob pattern on git, go, cargo, or similar commands that
   permits arbitrary flags.

7. **SSRF via registry/index flags** — Do npm/pip/cargo rules allow
   `--registry`, `--index-url`, or `--extra-index-url`? These redirect
   HTTP requests to attacker-controlled servers, leaking IP, auth
   tokens, and package queries. Flag `Bash(npm view:*)`,
   `Bash(pip install:*)`, etc.

### Data exfiltration and secret exposure

8. **Sensitive file reads** — Do rules allowing `cat`, `head`, `tail`,
   `file`, `stat`, or `less` permit reading sensitive paths?
   `cat ~/.ssh/id_rsa`, `cat /proc/self/environ`, `head ~/.aws/credentials`,
   `cat .env` all expose secrets. Flag open-ended read rules.

9. **Environment variable exposure** — Do rules allow `env`, `printenv`,
   `set`, or `export`? These dump environment variables which commonly
   contain API keys, database URLs, and tokens.

10. **PII exposure** — Do rules allow `whoami`, `hostname`, `id`?
    These expose usernames, machine names, and group memberships.
    `uname` and `locale` can fingerprint machines or reveal
    geographic information.

### Code execution

11. **Project code execution** — Do rules allow commands that execute
    project-controlled code?
    - Test runners (`jest`, `vitest`, `pytest`, `go test`, etc.)
      execute config files, setup scripts, and test code
    - Build tools (`make`, `cargo build`, `go build`, `npm run build`)
      execute Makefiles, build.rs, cgo, or package.json scripts
    - `cargo check`, `cargo clippy`, `go vet` also run build scripts
    - `npm test`, `npm run *` are indirection into arbitrary scripts
    Flag any rule that auto-allows these without project-specific
    justification.

12. **Arbitrary binary execution** — Do rules allow running any binary
    with certain flags (e.g., `Bash(*:--version)`)? A malicious binary
    in `$PATH` ignores `--version` and runs its payload. Prefer
    explicit binary names.

### Shared resource conflicts

13. **Integration test runners** — Do rules allow test commands that may
    hit shared dependencies (databases, APIs)? Multiple concurrent
    sessions can conflict on shared test databases. Test runners that
    commonly run integration tests (`deno test`, `pytest`, `go test`,
    `cargo test`, `mvn test`, `gradle test`) should not be auto-allowed
    globally. Even `npm test` is indirection that may run integration
    tests.

### Scope

14. **Global vs project scope** — Are the rules in a global
    `~/.claude/settings.json` or a project `.claude/settings.json`?
    Global rules apply across all projects and should be maximally
    restrictive. Project-level rules can be more permissive because
    the risk context is known.

15. **Blanket allows** — Are there unscoped rules like `"Bash"` (allows
    all bash commands) or `"Edit"` (allows all file edits)? These
    defeat the permission system entirely. Flag and recommend scoped
    alternatives.

### Non-Bash tool permissions

16. **Write/Edit to sensitive paths** — Do rules allow `Write` or `Edit`
    to paths outside the project directory? `Write(/etc/*)`,
    `Edit(~/.bashrc)`, or unscoped `Write` could overwrite system
    files, shell configs, or SSH keys. Scope to the project directory.

17. **Read of secrets** — Do rules allow `Read` of sensitive paths?
    `Read(~/.ssh/*)`, `Read(.env)`, `Read(/proc/*/environ)` expose
    credentials. Same concern as checklist item 8 but for the Read tool.

## Output format

For each finding, state:

1. **Which rule** — the exact `permissions.allow` entry
2. **Which checklist item** (by number and name)
3. **Attack vector** — a concrete command that exploits the rule
4. **Suggested fix** — a tighter rule or alternative approach

If no issues are found, say: "No issues found."
