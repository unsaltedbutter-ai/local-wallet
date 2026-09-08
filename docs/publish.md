# Publishing local-wallet

This document is a checklist for the **maintainer** to publish the repository
publicly at `github.com/unsaltedbutter-ai/local-wallet`. Publishing is a
manual, user-owned action — nothing here is automated.

## 0. Preflight sanity (read-only audit)

Before pushing anything public, confirm the tree and history are clean:

```sh
git status                 # working tree clean (or only intended changes)
git log --oneline | wc -l  # confirm history length looks right
git remote -v              # confirm origin is the expected remote
```

Check the tracked tree for anything that must not be public:

```sh
git grep -I -iE "(xpub|vpub|zpub)[A-Za-z0-9]{20,}"
git grep -I -nE "bc1[0-9a-zA-Z]{20,}"
git grep -I -nE "(notible|192\.168\.|10\.0\.|172\.1[6-9]\.)"
```

Fixture keys and eval/test addresses are expected and fine; flag anything
that is a **real** xpub or a **private hostname / internal IP** you don't
want public (e.g. `notible.local`, `192.168.x.x`). If you find any, scrub
and rewrite history **before** publishing — the repo has no other secrets by
design (watch-only).

Also confirm untracked private files are not accidentally added:

```sh
git status --porcelain   # .venv/, localwallet.db*, models/, etc. are gitignored
```

## 1. Choose the destination remote

The current `origin` points at a private GitHub host:

```
origin  git@github.unsaltedbutter:unsaltedbutter-ai/local-wallet.git
```

To publish publicly, push to `github.com`. Add a second remote (do not
overwrite the private one):

```sh
git remote add public git@github.com:unsaltedbutter-ai/local-wallet.git
git remote -v   # confirm both
```

> If you'd rather make `github.com` the sole origin, change `origin`'s URL —
> but be sure you no longer need the private host before removing it.

## 2. Push

The primary development branch is `dev/plan-run-1`. Push it, then create and
push `main` (the supported/stable branch per `SECURITY.md`):

```sh
git push public dev/plan-run-1
git push public dev/plan-run-1:main
```

Subsequent maintenance branches can be pushed as appropriate:

```sh
git push public <branch>
```

## 3. GitHub repository settings

On `github.com/unsaltedbutter-ai/local-wallet` (Settings), enable/confirm:

- **Default branch** → `main` (set once `main` exists).
- **General → Pull Requests**: require pull requests before merging, with
  status checks passing. Use **branch protection** on `main`:
  - Require a pull request before merging.
  - Require status checks to pass (select the CI checks: `test`,
    `Lint (ruff)`, `Network-import lint`, `Test (pytest)`).
  - Require branches to be up to date before merging.
- **Actions → General → Actions permissions**: enabled (the CI workflow runs
  on push/PR).
- **Security → Security advisories**: ensure they are enabled (used for
  private reporting; see `SECURITY.md`).

After enabling, confirm CI runs green on the pushed branches:

```sh
# Actions tab → CI run → all four jobs green
```

## 4. Final checks

- **LICENSE** — MIT, © 2026 unsaltedbutter-ai (already committed).
- **README badge** — points at the public Actions URL; will render once
  Actions runs.
- **SECURITY.md** — replace the placeholder email address before relying on
  it (or rely on GitHub Security Advisories only).

## Not automated / left to you

- Creating the GitHub repository and any org/visibility settings.
- Replacing the `SECURITY.md` email placeholder.
- Deciding whether to keep the private `github.unsaltedbutter` remote or drop
  it after the public push.
- Scrub-and-rewrite of history if the audit above turns up anything real.
