# Contributing

This is a solo portfolio project. The conventions below are what every change follows and what any future contributor would be expected to follow. The doc evolves with the project — sections like local dev setup, test commands, and deploy procedures will land as the corresponding code does.

## Workflow

- Every change goes on a feature branch off `main`
- Every change is opened as a PR — even solo
- `main` is protected: direct pushes blocked, CI must pass, PR required
- Merges to `main` use **squash-merge** so one logical change = one commit on `main`
- Branches auto-delete on merge

## Branch naming

Slash-prefixed by domain:

| Prefix | When to use |
|---|---|
| `feat/` | New feature |
| `fix/` | Bug fix |
| `refactor/` | Internal restructuring with no behavior change |
| `docs/` | Documentation changes |
| `ci/` | Build / CI pipeline changes |
| `test/` | Test-only changes |
| `chore/` | Housekeeping (deps, config, tooling) |
| `perf/` | Performance work |

Examples: `feat/8k-classifier`, `fix/edgar-rate-limit`, `docs/sharpen-positioning`, `ci/pin-markdownlint-version`.

## Commit messages

[Conventional Commits](https://www.conventionalcommits.org). Format:

```text
<type>(<optional-scope>): <imperative-mood description>

<optional body explaining why>
```

Types: `feat`, `fix`, `chore`, `docs`, `style`, `refactor`, `test`, `perf`, `ci`, `build`, `revert`.

A `!` after the type or scope marks a breaking change (e.g., `refactor(parser)!: ...`).

Examples:

- `ci: add gitleaks and markdownlint workflows`
- `feat(8k): classify executive-departure events`
- `fix(edgar): retry on 429 with exponential backoff`
- `refactor(parser)!: extract item-extraction into a trait`

## CI gates

Every PR must pass before merge:

- **`scan-secrets`** — `gitleaks` scan of working tree and history (CI uses `gitleaks` v8.18.4)
- **`lint-docs`** — `markdownlint-cli2` over all `.md` files (CI uses `markdownlint-cli2` v0.22.1)

Language-specific gates (Python lint/test, Go build/test, Rust check/test) will be added in follow-up PRs as the corresponding code lands.

### Running the gates locally

To match CI exactly (same versions, same rules), use the pinned commands:

```bash
# Markdown lint
npx --yes markdownlint-cli2@0.22.1 "**/*.md"

# Secrets scan (install gitleaks v8.18.4 first via brew, apt, or the GitHub release)
gitleaks detect --no-banner --verbose
```

If a local run passes and CI fails (or vice versa), the version pin has drifted — fix the pin, don't silence the rule.

## PR descriptions

Each PR body answers three questions:

1. **What** changed — the surface-level diff summary
2. **Why** — the motivation; the durable record that survives long after the diff is forgotten
3. **Notes** — caveats, follow-ups, anything a reviewer should know

For solo work, the **Why** section is the most important. It captures decision context that the code itself can't.

## Architecture decisions

Substantial decisions (technology choice, design pattern, scope cut, positioning) are recorded as [ADRs](docs/decisions/) — short, numbered, dated markdown files capturing context, the decision, the alternatives that were rejected, and the consequences accepted.

When a PR involves an ADR-worthy decision, include the new ADR in the same PR as the change it justifies. Use [docs/decisions/template.md](docs/decisions/template.md) as a starting point.
