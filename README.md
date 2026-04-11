# Dotfiles

My development environment config for Linux, managed with [GNU Stow](https://www.gnu.org/software/stow/).

## Setup

```bash
git clone git@github.com:jared/dotfiles.git ~/dotfiles
cd ~/dotfiles
./install.sh
```

The install script will:
1. Install system packages (`build-essential`, `curl`, `git`, `stow`)
2. Install [fnm](https://github.com/Schniz/fnm) (Fast Node Manager)
3. Install [Starship](https://starship.rs/) prompt
4. Symlink all config packages into `$HOME`

## Packages

| Package | What it configures |
|---|---|
| `bash` | `.bashrc` — shell settings, history, aliases, fnm, starship init |
| `claude` | `.claude/` — Claude Code global instructions, custom skills, settings, and hooks |
| `starship` | `.config/starship.toml` — prompt theme and disabled modules |

To stow individual packages:

```bash
stow -v -t "$HOME" bash
stow -v -t "$HOME" claude
stow -v -t "$HOME" starship
```

## Claude Code package

The `claude` package configures [Claude Code](https://claude.ai/claude-code) globally:

- **`CLAUDE.md`** — baseline engineering instructions applied to all projects (judgment heuristics, working style, safety rules)
- **`settings.json`** — global settings including a custom statusline and hooks
- **`hooks/`** — PreToolUse gates that redirect Claude to the relevant review skill:
  - `require-code-review.sh` — blocks `git commit` (including chained forms like `git add . && git commit`) until `/code-review` has run on the current staged state. Verified via sha256 marker in `~/.claude/review-markers/<repo-hash>`, which auto-invalidates the moment the staging area changes.
  - `require-respond-pr.sh` — blocks PR comment reads and posts (`gh api .../pulls|issues/N/{comments,reviews}`, `gh pr comment`, `gh pr review`) and redirects to `/respond-pr`, so all three comment types get fetched and replies carry the `[Claude Code]` attribution prefix. Honors a 60-minute bypass marker at `~/.claude/.respond-pr-active` that the skill sets on entry and removes on exit.
  - `ask-review-permissions.sh` — asks before `Edit`/`Write`/`MultiEdit` to `.claude/settings*.json`, nudging you to run `/review-permissions` if the edit touches `permissions.allow`.
- **`hooks/tests/hook-tests.sh`** — end-to-end tests for all three hooks. Spins up a temp git repo, feeds each hook realistic JSON input, and verifies the allow/deny/ask decisions (36 cases). Run with:

  ```bash
  bash claude/.claude/hooks/tests/hook-tests.sh
  ```

  Exits non-zero on failure. Safe to run — it backs up and restores any existing `~/.claude/.respond-pr-active` marker and confines its repo work to a temp directory.
- **`statusline-command.sh`** — custom status bar showing model, context usage, session cost, working directory, and git branch
- **`commands/code-review.md`** — `/code-review` skill: principal engineer code review checklist (11 items covering correctness, hygiene, clarity, security, and scope)
- **`commands/respond-pr.md`** — `/respond-pr` skill: fetch and address PR review comments, with `[Claude Code]` attribution on all replies
- **`commands/read-docx-comments.md`** — `/read-docx-comments` skill: extract comments from .docx files with their anchored text context

Machine-specific Claude Code permissions belong in `~/.claude/settings.local.json` (not tracked).

## What goes in `.bashrc` vs `.bashrc.local`

The `.bashrc` in this repo is **portable across any Linux machine** that has the install script's dependencies set up. It contains shell fundamentals, standard aliases, and tools that install identically everywhere (fnm, starship).

**Machine-specific config belongs in `~/.bashrc.local`**, which is sourced at the end of `.bashrc` but not tracked in this repo. The litmus test: if the config is tied to a specific machine's filesystem, hardware, or distro-specific packages, it goes in `.bashrc.local`.

Examples of what belongs in `.bashrc.local`:

```bash
# Java — path varies by distro and version
export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64

# Yarn — only needed if a project requires it
export PATH="$HOME/.yarn/bin:$HOME/.config/yarn/global/node_modules/.bin:$PATH"

# Android SDK — machine-specific mount point
export GRADLE_USER_HOME=$HOME/.gradle
export ANDROID_HOME=/media/jared/DATA/Android/Sdk
export PATH=$PATH:$ANDROID_HOME/platform-tools
export PATH=$PATH:$ANDROID_HOME/tools
export PATH=$PATH:$HOME/.maestro/bin

# earlyoom monitoring — Ubuntu/Fedora only
alias checkmem='journalctl -u earlyoom -f'

# Desktop notification for long commands — requires notify-send (desktop Linux only)
alias alert='notify-send --urgency=low -i "$([ $? = 0 ] && echo terminal || echo error)" "$(history|tail -n1|sed -e '\''s/^\s*[0-9]\+\s*//;s/[;&|]\s*alert$//'\'')"'
```
