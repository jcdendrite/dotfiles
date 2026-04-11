---
name: respond-pr
description: Read and respond to PR review comments on the current branch's pull request
argument-hint: "[PR number]"
---

TRIGGER when: replying to PR review comments, OR posting any comment/reply to a GitHub pull request — even as part of a larger task. Enforces required attribution prefix.

Fetch all review comments on the current branch's open pull request and address them.

## Steps

0. **Enable hook bypass.** Run `mkdir -p ~/.claude && touch ~/.claude/.respond-pr-active`. The `require-respond-pr.sh` PreToolUse hook checks for this marker and lets this skill's own `gh` commands through while the marker is fresh (<60 minutes old). Without this step, every `gh api` call below will be blocked by the very gate that redirected you here.
1. Identify the PR number for the current branch: `gh pr view --json number -q '.number'`
2. Fetch **all three** types of comments (Claude commonly fetches only the first and misses real feedback):
   - **Inline file comments:** `gh api repos/{owner}/{repo}/pulls/{number}/comments`
   - **Top-level review comments:** `gh api repos/{owner}/{repo}/pulls/{number}/reviews --jq '.[] | select(.body != "")'`
   - **Issue-level comments:** `gh api repos/{owner}/{repo}/issues/{number}/comments`
3. For each unresolved comment:
   - Read the referenced file and line to understand the context
   - Determine if it requires a code change, a reply, or both
   - If a code change is needed, make the change and note it in the reply
   - If it's a question or discussion point, draft a clear response
4. Post replies using the GitHub API with `in_reply_to` (use `-F` for integer IDs)
5. Commit and push any code changes in a single commit
6. **Remove the hook bypass marker:** `rm -f ~/.claude/.respond-pr-active`. If the skill errors out before reaching this step, don't manually clean up — the hook's 60-minute staleness cutoff handles orphaned markers automatically.

## Attribution

**CRITICAL:** All PR comment replies are posted through the user's GitHub token and will appear as the user's account. To avoid confusion, **always** prefix every reply body with `**[Claude Code]**` followed by the response content. This makes it clear the response is AI-generated.

Example:
```
gh api repos/owner/repo/pulls/4/comments \
  -F body='**[Claude Code]** Moved the utility functions to the shared module as suggested.' \
  -F in_reply_to=12345678
```

## Guidelines

- Group related code changes into a single commit
- Be concise in replies — state what was done, not lengthy explanations
- If you disagree with a comment, explain why clearly but defer to the reviewer's judgment
- Do not resolve review threads — let the reviewer verify and resolve them
