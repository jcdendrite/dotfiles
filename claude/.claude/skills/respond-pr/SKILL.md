---
name: respond-pr
description: Read and respond to PR review comments on the current branch's pull request
---

Fetch all review comments on the current branch's open pull request and address them.

## Steps

1. Identify the PR number for the current branch: `gh pr view --json number -q '.number'`
2. Fetch all review comments: `gh api repos/{owner}/{repo}/pulls/{number}/comments`
3. For each unresolved comment:
   - Read the referenced file and line to understand the context
   - Determine if it requires a code change, a reply, or both
   - If a code change is needed, make the change and note it in the reply
   - If it's a question or discussion point, draft a clear response
4. Post replies using the GitHub API with `in_reply_to` (use `-F` for integer IDs)
5. Commit and push any code changes in a single commit

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
