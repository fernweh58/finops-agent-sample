# -----------------------------------------------------------------------------
# Cross-Account IAM Role in Management Account
# -----------------------------------------------------------------------------
# This role is created in the management/payer account and allows the
# MCP Gateway (in data collection account) to access Cost Explorer and CUR data.
#
# Only created when management_account_profile is set.
# -----------------------------------------------------------------------------

# Get data collection account ID (always available)
data "aws_caller_identity" "data_collection" {}

# Get management account ID (only when cross-account is enabled)
data "aws_caller_identity" "management" {
  count    = var.management_account_profile != "" ? 1 : 0
  provider = aws.management
}

# Generate external ID if not provided
resource "random_id" "cross_account_external_id" {
  count       = var.management_account_profile != "" && var.management_external_id == "" ? 1 : 0
  byte_length = 16
}

locals {
  # Whether cross-account mode is enabled (known at plan time)
  cross_account_enabled = var.management_account_profile != ""

  # Use provided external ID or generate one
  cross_account_external_id = local.cross_account_enabled ? (
    var.management_external_id != "" ? var.management_external_id : random_id.cross_account_external_id[0].hex
  ) : ""

  # Role ARN for Lambda environment variables
  management_role_arn = local.cross_account_enabled ? aws_iam_role.mcp_gateway_cross_account[0].arn : ""
}

# Trust policy - allows data collection account to assume this role
data "aws_iam_policy_document" "cross_account_trust" {
  count = var.management_account_profile != "" ? 1 : 0

  statement {
    sid     = "AllowDataCollectionAccountAssume"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.data_collection.account_id}:root"]
    }

    condition {
      test     = "StringEquals"
      variable = "sts:ExternalId"
      values   = [local.cross_account_external_id]
    }
  }
}

# ReadOnlyAccess — cross-account read for all AWS services.
# CUR data is delivered directly to cost account bucket (方案A), so Athena/Glue/S3
# all run locally in the cost account. This role is used by:
#   - cost-explorer-mcp Lambda: org-wide CE queries
#   - aws-api-cross-account-mcp Lambda: org-wide resource queries (EC2, S3, RDS, etc.)

# IAM Role in management account
resource "aws_iam_role" "mcp_gateway_cross_account" {
  count    = var.management_account_profile != "" ? 1 : 0
  provider = aws.management

  name               = "${var.project_name}-cross-account"
  description        = "Allows MCP Gateway in data collection account read-only access to all AWS services"
  assume_role_policy = data.aws_iam_policy_document.cross_account_trust[0].json

  tags = merge(local.common_tags, {
    Purpose = "mcp-gateway-cross-account"
  })
}

# Attach AWS managed ReadOnlyAccess policy (covers CE + all services)
resource "aws_iam_role_policy_attachment" "mcp_gateway_cross_account_readonly" {
  count      = var.management_account_profile != "" ? 1 : 0
  provider   = aws.management
  role       = aws_iam_role.mcp_gateway_cross_account[0].name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}
