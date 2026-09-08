# -----------------------------------------------------------------------------
# Dedicated Athena Workgroup for the FinOps MCP agent (patch 3)
# -----------------------------------------------------------------------------
# The athena-mcp Lambda defaults to this workgroup. enforce_workgroup_configuration
# guarantees results land in a bucket the Lambda role can write, and the
# bytes-scanned cutoff caps runaway LLM-generated queries against the CUR table.
# -----------------------------------------------------------------------------

resource "aws_athena_workgroup" "finops" {
  name        = var.project_name
  description = "Athena workgroup for the FinOps MCP agent (LLM-generated queries)"

  configuration {
    enforce_workgroup_configuration    = true
    publish_cloudwatch_metrics_enabled = true
    bytes_scanned_cutoff_per_query     = var.athena_bytes_scanned_cutoff

    result_configuration {
      output_location = local.athena_output_location
      encryption_configuration {
        encryption_option = "SSE_S3"
      }
    }
  }

  tags = local.common_tags
}
