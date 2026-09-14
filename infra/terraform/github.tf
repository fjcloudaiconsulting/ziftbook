# The repository and the settings the delivery process depends on. Imported, not created: the repo predates this.

import {
  to = github_repository.ziftbook
  id = "ziftbook"
}

import {
  to = github_branch_protection.main
  id = "ziftbook:main"
}

import {
  to = github_workflow_repository_permissions.ziftbook
  id = "ziftbook"
}

resource "github_repository" "ziftbook" {
  name       = "ziftbook"
  visibility = "public"

  has_issues      = true
  has_projects    = true
  has_wiki        = true
  has_discussions = false

  # Squash only, titled by the PR: the PR title is the Conventional Commit on main that release-please reads.
  allow_merge_commit          = false
  allow_rebase_merge          = false
  allow_squash_merge          = true
  squash_merge_commit_title   = "PR_TITLE"
  squash_merge_commit_message = "PR_BODY"
  allow_auto_merge            = true
  allow_update_branch         = true
  delete_branch_on_merge      = true

  # Free for public repositories: block pushes that contain known secret formats.
  security_and_analysis {
    secret_scanning {
      status = "enabled"
    }
    secret_scanning_push_protection {
      status = "enabled"
    }
  }

  archive_on_destroy = true

  lifecycle {
    prevent_destroy = true
  }
}

resource "github_branch_protection" "main" {
  repository_id = github_repository.ziftbook.node_id
  pattern       = "main"

  # History on main is never rewritten or removed, including by admins and the review bypass below.
  allows_force_pushes = false
  allows_deletions    = false

  required_pull_request_reviews {
    required_approving_review_count = 1
    # The owner merges their own PRs.
    pull_request_bypassers = ["/flamarion"]
  }

  required_status_checks {
    strict = true
  }
}

# release.yml opens Release PRs with GITHUB_TOKEN, which needs "Allow GitHub Actions to create and approve pull
# requests". Every job still starts from read-only and asks for what it needs.
resource "github_workflow_repository_permissions" "ziftbook" {
  repository                       = github_repository.ziftbook.name
  default_workflow_permissions     = "read"
  can_approve_pull_request_reviews = true
}
