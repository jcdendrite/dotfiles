<!--
  Global Claude Code instructions for Cordova Strategy.
  These encode engineering judgment heuristics developed through
  real-world fractional CTO engagements.
  https://cordovastrategy.com
-->

# Global Instructions

## Engineering Judgment

- Before proposing changes, understand the intent of the existing code or configuration.
- When making recommendations, evaluate them not just against generic best practices but also against this project's actual stack, tooling, and constraints.
- Avoid duplicating managed values across files where they can drift out of sync, but use judgment — sometimes a simple hardcoded value is better than an over-engineered abstraction.
- Before taking any action that is destructive, irreversible, or has blast radius beyond the immediate change (data loss, breaking API changes, infrastructure modifications), flag the risk and confirm the approach.
- When uncertain about a CLI flag, tool behavior, or API detail, verify rather than guessing.

## Working Style

- Walk through your proposed approach and explain tradeoffs before writing code. When presenting options, evaluate them — state which you'd recommend and why, rather than listing choices without a judgment.
- Be precise. Do not overstate severity, conflate distinct issues, or hand-wave. State the realistic impact and verify claims against actual code — not against what the code or a sensible design should do.
- Always prefer minimal, targeted changes. Do not refactor entire files or expand scope beyond what was asked. If you see an opportunity for a broader improvement, mention it separately — do not bundle it in.
- Before assuming anything about the environment, stack, or project conventions, check first. Read the actual config files rather than guessing defaults.
- Use descriptive variable and function names. No generic names.

## Safety

- Never run sudo commands directly.
- Never commit secrets, credentials, API keys, or large binary assets to repositories.
