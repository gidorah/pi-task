# Issue tracker: GitHub

Issues and PRDs for this repository live as GitHub issues. Use the `gh` CLI for all operations.

A GitHub remote must be configured before performing issue operations.

## Conventions

- Create: `gh issue create --title "..." --body "..."`
- Read: `gh issue view <number> --comments`
- List: `gh issue list --state open --json number,title,body,labels,comments`
- Comment: `gh issue comment <number> --body "..."`
- Apply/remove labels: `gh issue edit <number> --add-label "..."` or `--remove-label "..."`
- Close: `gh issue close <number> --comment "..."`

Infer the repository from `git remote -v`; `gh` handles this automatically inside the repository.

## Pull requests as a triage surface

**PRs as a request surface: no.**

## When a skill says "publish to the issue tracker"

Create a GitHub issue.

## When a skill says "fetch the relevant ticket"

Run `gh issue view <number> --comments`.

## Wayfinding operations

- **Map:** one issue labelled `wayfinder:map`.
- **Child ticket:** a GitHub sub-issue linked to the map and labelled `wayfinder:<type>`.
- **Blocking:** use GitHub’s native issue dependencies where available.
- **Frontier:** choose the first open, unblocked, unassigned child in map order.
- **Claim:** `gh issue edit <number> --add-assignee @me`.
- **Resolve:** comment with the answer, close the issue, and update the map’s Decisions-so-far.
