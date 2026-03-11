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
| `starship` | `.config/starship.toml` — prompt theme and disabled modules |

To stow individual packages:

```bash
stow -v -t "$HOME" bash
stow -v -t "$HOME" starship
```

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
