#!/bin/bash
set -e

echo "=== Dotfiles Setup ==="

# System packages
echo "Installing system packages..."
sudo apt update
sudo apt install -y build-essential curl git keychain stow

# fnm (Fast Node Manager)
if ! command -v fnm &> /dev/null; then
    echo "Installing fnm..."
    curl -fsSL https://fnm.vercel.app/install | bash -s -- --skip-shell
fi

# Starship prompt
if ! command -v starship &> /dev/null; then
    echo "Installing starship..."
    curl -sS https://starship.rs/install.sh | sh -s -- -y
fi

# Stow packages
DOTFILES_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "Stowing dotfiles from $DOTFILES_DIR..."

cd "$DOTFILES_DIR"
stow -v --adopt -t "$HOME" bash
stow -v --adopt -t "$HOME" starship

echo ""
echo "Done! Open a new terminal or run: source ~/.bashrc"
echo ""
echo "Create ~/.bashrc.local for machine-specific config. See README.md for details."
