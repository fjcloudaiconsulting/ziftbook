# Terraform

Configuration that lives outside the code: today, the GitHub repository settings the delivery process depends on
(merge rules, branch protection on `main`, Actions permissions, secret scanning). State and runs are in Terraform
Cloud, workspace `FlamaCorp/ziftbook`.

GHCR packages are not here: GitHub has no API to create a package or change its visibility. A package is created by
the first push from `release.yml` and stays private.

## Workspace (one-time, by the owner)

1. **Workspace:** New workspace → Version control workflow → GitHub (HCP Terraform GitHub App) →
   `fjcloudaiconsulting/ziftbook`. If the app is installed for selected repositories only, add `ziftbook` to it.
   Name `ziftbook`.
2. **Settings → General:** Terraform version: latest 1.16.x. Apply method: Manual apply.
3. **Settings → Version Control:** Terraform working directory `infra/terraform`. Automatic run triggering: only
   when files in specified paths change, pattern `infra/terraform/**`. Automatic speculative plans: on.
4. **Variables:** `GITHUB_TOKEN`, category *Environment variable*, marked *Sensitive*, with the token below.

## GitHub token

A fine-grained personal access token (github.com → Settings → Developer settings → Fine-grained tokens):

- Resource owner: `fjcloudaiconsulting`
- Expiration: 1 year (renew by generating a new token and replacing the workspace variable)
- Repository access: Only select repositories → `ziftbook`
- Repository permissions: **Administration: Read and write** (Metadata: Read-only is added automatically). Nothing
  else.

## Changes

Edit the `.tf` files in a PR; the workspace posts a speculative plan on it. After merge, confirm the queued run in
Terraform Cloud. The repository has `prevent_destroy`, and `archive_on_destroy` as a second guard.
