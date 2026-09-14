terraform {
  required_version = ">= 1.14.0"

  # State and runs live in Terraform Cloud (FlamaCorp/ziftbook). The workspace is VCS-driven on this repo with
  # working directory infra/terraform: pull requests get speculative plans, merges to main queue a run that waits
  # for Confirm & Apply. The only workspace variable is GITHUB_TOKEN (environment, sensitive); see README.md.
  cloud {
    organization = "FlamaCorp"
    workspaces {
      name = "ziftbook"
    }
  }

  required_providers {
    github = {
      source  = "integrations/github"
      version = "~> 6.13"
    }
  }
}

# Reads GITHUB_TOKEN from the environment.
provider "github" {
  owner = "fjcloudaiconsulting"
}
