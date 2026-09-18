# Governance & Security

## 1. Version pinning

Consumers reference this repository only by immutable ref:

| Ref style | Example | Use |
|---|---|---|
| Semantic tag | `@v1.1.0` | Default for all consumers |
| Commit SHA | `@3f2a9c1…` | Highest assurance; required for third-party actions in `security-and-quality` tier |
| Floating major | `@v1` | Only for internal experiments; never in production repos |
| Branch | `@main` | Forbidden |

Release procedure:

```bash
git tag -a v1.1.0 -m "central-actions v1.1.0"
git push origin v1.1.0
git tag -fa v1 -m "v1 -> v1.1.0" && git push -f origin v1     # move floating major
gh release create v1.1.0 --generate-notes
```

Breaking changes to workflow inputs/secrets bump the major version. Keep `central-actions-ref`
in `deploy-landing-page.yml` callers equal to the workflow tag so the composite actions and the
workflow are always the same revision.

## 2. Keyless GCP authentication (OIDC)

No service-account JSON keys are ever stored in GitHub. The Workload Identity provider trusts the
GitHub OIDC issuer with the attribute condition

```
attribute.repository_owner == 'Autonomous-Agentic-Workflows'
```

so any repository under the org can impersonate `github-deployer@PROJECT.iam.gserviceaccount.com`
and nothing outside the org can. Set up with `scripts/bootstrap-org.sh` (idempotent).

Organization secrets (`Autonomous-Agentic-Workflows > Settings > Secrets and variables > Actions`):

| Secret | Value |
|---|---|
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | `projects/NUMBER/locations/global/workloadIdentityPools/github-pool/providers/github-provider` |
| `GCP_SERVICE_ACCOUNT` | `github-deployer@PROJECT.iam.gserviceaccount.com` |
| `GCP_PROJECT_ID` | `PROJECT` (optional) |

Repos that should stay on the zero-billing tier simply omit the secrets: the deploy workflow
detects their absence and publishes to GitHub Pages without attempting Cloud Run.

## 3. Enforcement via organization rulesets

UI: `Autonomous-Agentic-Workflows > Settings > Repository > Rulesets > New ruleset > New branch ruleset`

- Target: all repositories, default branch
- Rules: require pull request (1 approval), require status checks to pass, block force-push and deletion
- Required check: **`ci / gate`** (the job id `ci` from the consumer workflow + the `gate` job of Central CI)

CLI (same result):

```bash
gh api -X POST orgs/Autonomous-Agentic-Workflows/rulesets --input docs/rulesets/require-central-ci.json
```

Org rulesets require GitHub Team or Enterprise for private repositories; public repos work on Free.

## 4. Reusable workflow access

Reusable workflows in a private/internal repository are callable by other org repos only when
Actions access is set to *organization*:

```bash
gh api -X PUT repos/Autonomous-Agentic-Workflows/central-actions/actions/permissions/access -f access_level=organization
```

Making `central-actions` public removes this requirement. Either way, third-party actions used
inside this repo should be reviewed on every dependency bump.

## 5. Least-privilege tokens

Every workflow declares explicit `permissions`. Consumers must grant at least:

```yaml
permissions:
  contents: write        # gh-pages publish
  id-token: write        # OIDC
  pages: write
  security-events: write # CodeQL upload
  actions: read
```

`GITHUB_TOKEN` is used only for the Pages fallback commit; Cloud Run uses the short-lived OIDC token.
