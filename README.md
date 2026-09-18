# Autonomous-Agentic-Workflows / central-actions

Centralized reusable workflows and composite actions for every repository in the **Autonomous-Agentic-Workflows** (208 Fence and Gate)
organization: one CI definition, one IRUE repository scan, one dual-mode deployment engine.

```
Autonomous-Agentic-Workflows/central-actions/
├── .github/workflows/
│   ├── central-ci.yml               # reusable: stack detection, lint, unit tests, CodeQL, "gate" check
│   └── deploy-landing-page.yml      # reusable: IRUE scan -> Cloud Run (OIDC) -> GitHub Pages fallback
├── actions/
│   ├── irue-scan/                   # composite: IRUE scanner + WebMCP manifest compiler
│   │   ├── action.yml
│   │   ├── scripts/irue_scanner.py
│   │   ├── scripts/landing_page_generator.py
│   │   └── serve/{Dockerfile,nginx.conf}   # static server used on Cloud Run
│   └── setup-gcp-oidc/action.yml    # composite: keyless GCP auth + gcloud
├── templates/consumer-deploy.yml    # drop-in caller workflow for product repos
├── scripts/bootstrap-org.sh         # creates repo, tags, WIF pool/provider, org secrets
└── docs/
    ├── GOVERNANCE.md                # pinning, OIDC, rulesets, least privilege
    └── rulesets/require-central-ci.json
```

## Consumer usage

Copy [`templates/consumer-deploy.yml`](templates/consumer-deploy.yml) to
`.github/workflows/deploy.yml` in a product repository (for example `Autonomous-Agentic-Workflows/MasterRecoveryAgents`):

```yaml
jobs:
  ci:
    uses: Autonomous-Agentic-Workflows/central-actions/.github/workflows/central-ci.yml@v1.1.0
    with:
      environment: production

  deploy-landing:
    needs: ci
    uses: Autonomous-Agentic-Workflows/central-actions/.github/workflows/deploy-landing-page.yml@v1.1.0
    with:
      service-name: masterrecoveryagents-landing-page
      enable-fallback: true
    secrets:
      GCP_WORKLOAD_IDENTITY_PROVIDER: ${{ secrets.GCP_WORKLOAD_IDENTITY_PROVIDER }}
      GCP_SERVICE_ACCOUNT: ${{ secrets.GCP_SERVICE_ACCOUNT }}
```

Required status check for branch rulesets: **`ci / gate`**.

## Reusable workflow: `central-ci.yml`

| Input | Default | Purpose |
|---|---|---|
| `environment` | `production` | Label in job summaries |
| `python-version` / `node-version` | `3.11` / `20` | Toolchain versions |
| `run-codeql` | `true` | Disable for private repos without GitHub Advanced Security |
| `codeql-languages` | auto | Comma-separated override (`python,javascript-typescript`) |
| `working-directory` | `.` | Monorepo sub-project |
| `fail-on-lint` | `true` | Downgrade lint errors to warnings |

Jobs: `detect` → `python` (ruff, pytest) / `node` (npm lint, test) / `codeql` (matrix) → `gate`.
The `gate` job always runs and fails if any upstream job failed, giving rulesets a single stable
check name regardless of which languages a repo contains.

## Reusable workflow: `deploy-landing-page.yml`

| Input | Default | Purpose |
|---|---|---|
| `service-name` | required | Cloud Run service |
| `gcp-region` | `us-central1` | Cloud Run region |
| `enable-fallback` | `true` | Publish to GitHub Pages when Cloud Run is unavailable |
| `force-fallback` | `false` | Skip Cloud Run entirely (zero-billing mode) |
| `output-dir` | `public` | Bundle directory |
| `central-actions-ref` | `v1.1.0` | Revision of composite actions; keep equal to the workflow tag |
| `max-instances` | `10` | Cloud Run scaling cap |

Secrets `GCP_WORKLOAD_IDENTITY_PROVIDER`, `GCP_SERVICE_ACCOUNT` (and optional `GCP_PROJECT_ID`) are
**optional**. Decision tree inside the `deploy` job:

```
secrets present? ──no──▶ GitHub Pages (gh-pages branch)
      │yes
OIDC auth ok? ─────no──▶ GitHub Pages
      │yes
Cloud Run deploy ok? ─no─▶ GitHub Pages
      │yes
   Cloud Run URL
```

Outputs `deployment-tier` (`cloud-run` | `github-pages`) and `deployment-url`. The job fails only
if every tier failed. For the Pages tier, enable Pages once per repo with source = `gh-pages` branch.

## Composite action: `actions/irue-scan`

Runs `irue_scanner.py` (stdlib only) to build `repo_profile.json` with languages, manifests,
Python AST (public classes/functions with signatures and docstrings), CI workflows, docs and
license, then `landing_page_generator.py` renders:

- `index.html` — self-contained, responsive landing page (stats, language bar, agent tools, API surface, manifests, workflows)
- `.well-known/webmcp.json` (+ `mcp.json` alias) — WebMCP manifest listing callable tool candidates derived from console scripts, npm scripts, Makefile targets and documented Python functions
- `repo_profile.json`, `.nojekyll`

The page registers the tools with `navigator.modelContext` when the browser supports WebMCP.

Local run:

```bash
python3 actions/irue-scan/scripts/irue_scanner.py . > repo_profile.json
python3 actions/irue-scan/scripts/landing_page_generator.py repo_profile.json --output-dir public
python3 -m http.server -d public 8080
```

## Composite action: `actions/setup-gcp-oidc`

Validates the provider/service-account format, resolves the project id, authenticates with
`google-github-actions/auth@v2` (Workload Identity Federation, no JSON keys) and installs `gcloud`.
Outputs `project-id`, `access-token`, `credentials-file`.

## Bootstrap the organization

```bash
ORG=Autonomous-Agentic-Workflows GCP_PROJECT=<gcp-project-id> ./scripts/bootstrap-org.sh
gh api -X POST orgs/Autonomous-Agentic-Workflows/rulesets --input docs/rulesets/require-central-ci.json
```

See [docs/GOVERNANCE.md](docs/GOVERNANCE.md) for tag pinning, OIDC trust, rulesets and token scopes.

## References

- Reusing workflows: https://docs.github.com/actions/using-workflows/reusing-workflows
- Composite actions: https://docs.github.com/actions/creating-actions/creating-a-composite-action
- google-github-actions/auth (WIF): https://github.com/google-github-actions/auth
- google-github-actions/deploy-cloudrun: https://github.com/google-github-actions/deploy-cloudrun
- peaceiris/actions-gh-pages: https://github.com/peaceiris/actions-gh-pages
- CodeQL action: https://github.com/github/codeql-action
- Organization rulesets: https://docs.github.com/organizations/managing-organization-settings/managing-rulesets-for-repositories-in-your-organization
- WebMCP (W3C Web Machine Learning CG): https://github.com/webmachinelearning/webmcp
