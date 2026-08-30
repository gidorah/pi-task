# Omarchy mise tool management

Research date: 2026-08-30

## Conclusion

Mise is Omarchy's intended manager for its pre-wired command-line agents and developer tools. Omarchy creates small launchers in `~/.local/bin`; the first invocation installs the selected tool through mise, and later Omarchy updates keep it current. Keeping `codex`, `pi`, and `gh` under mise therefore matches the Omarchy default.

The problems on this machine come from two integration conflicts, not from choosing mise:

1. Shell startup code prepends `~/.local/bin` after Omarchy activates mise. This puts Omarchy's lazy wrapper ahead of mise's real executable and can make the wrapper call itself.
2. T3 Code's standalone Codex updater replaces `~/.local/bin/codex` with its own symlink and keeps a separate release store. Arch also has a system `gh`, so those tools have competing owners.

## Intended Omarchy behavior

The official Omarchy AI manual describes agent commands as lazy-loaded, mise-managed stubs in `~/.local/bin`. It says nothing downloads until first use, and `omarchy update` or `mup` keeps these tools current. Selecting a default agent from the menu installs it if necessary.

Source: [Omarchy AI manual](https://github.com/omacom/omarchy/blob/quattro/manual/17-ai.md)

Current Omarchy source provisions `codex`, `gh`, `pi`, Playwright, and other CLIs with `omarchy-mise-install`.

Source: [Omarchy mise install list](https://github.com/omacom/omarchy/blob/quattro/install/user/mise.sh)

The generated launcher runs `mise use -g --quiet`, then executes the package through `mise x`. The global selection gives `omarchy update` a single set of tools to update, while the wrapper provides first-run installation.

Source: [omarchy-mise-install](https://github.com/omacom/omarchy/blob/quattro/bin/omarchy-mise-install)

## Known wrapper problems

Omarchy issue 6349 documented infinite recursion when a wrapper resolves its own command name because the mise-managed executable is not ahead of `~/.local/bin`. Omarchy merged PR 6350 to run the command through `mise exec`, making resolution less dependent on ambient PATH order.

Sources: [issue 6349](https://github.com/omacom/omarchy/issues/6349), [merged PR 6350](https://github.com/omacom/omarchy/pull/6350)

The current wrapper is an improvement, but mise still expects its activated tool paths to remain ahead of competing paths. On this machine, `mise doctor` warns that mise tool paths are not first and specifically recommends running `mise activate` after other PATH changes.

Mise documents shell activation as the mechanism that changes PATH when the directory or tool configuration changes. Shims are the alternative for environments where activation is not available.

Source: [mise dev tools documentation](https://mise.jdx.dev/dev-tools/)

## Duplicate ownership

Local inspection found:

- Codex 0.151.0 under mise, about 331 MB.
- Codex 0.151.0 under T3 Code's standalone store, plus 12 older standalone releases. The store totals about 4.2 GB.
- `~/.local/bin/codex` points to the standalone store, so T3's installer currently wins command resolution while mise still updates its unused copy.
- GitHub CLI 2.98.0 exists both under mise and as Arch package `github-cli`.
- Pi has one real installation under mise. Its wrapper and shim are dispatchers, not extra installations.

An open Omarchy issue also documents `gh` credential helpers retaining a version-specific mise path after upgrades. The newer wrapper's quiet mode fixes the separate problem where mise status output polluted the Git credential protocol, but stable credential-helper resolution remains important.

Source: [Omarchy issue 7712](https://github.com/omacom/omarchy/issues/7712)

## Practical default-first setup

Use mise as the owner of Omarchy's tool list, including `codex`, `gh`, `pi`, and Playwright. This follows the official manual and avoids maintaining a custom split that future Omarchy migrations may undo.

First fix shell initialization so all custom PATH additions happen before Omarchy's mise activation, or reactivate mise once at the end of `.bashrc`. Remove repeated `~/.local/bin` prepends. A fresh interactive shell should make `mise doctor` report no PATH-order warning.

For Codex, the T3 desktop application creates a real ownership conflict. If T3 requires its standalone Codex payload, keep that payload for the application but do not treat its `~/.local/bin/codex` symlink as the shell CLI owner. Restore Omarchy's mise wrapper for terminal use. The standalone release cleanup should use the application's supported retention behavior rather than deleting its active release manually.

For `gh`, keeping the Arch package installed is harmless as a fallback, but it is a duplicate. A strict Omarchy-default setup can remove the Arch package after verifying mise-managed `gh`, authentication, Git credential configuration, and PR operations. If other packages require `github-cli`, keep it installed and let PATH select mise's copy.

## Verification

After adjusting the shell and restoring wrappers:

```bash
mise doctor
command -v codex
command -v gh
command -v pi
codex --version
gh auth status
gh pr status
pi --version
```

The commands must complete without repeated `mise ... tools:` messages. `mise doctor` must not warn that other paths precede mise's tool paths.
