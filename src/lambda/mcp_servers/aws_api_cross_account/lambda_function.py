"""
AWS API Cross-Account MCP Server - Lambda Implementation for Amazon Bedrock AgentCore Gateway

Provides cross-account AWS resource query capabilities by assuming a ReadOnlyAccess
role in the management/payer account. This is the Lambda equivalent of the aws-api-mcp
Runtime container, but with built-in cross-account support via shared/cross_account.py.

Tools:
- call_aws: Execute AWS API calls via boto3 (cross-account via assumed role)
- suggest_aws_commands: Suggest AWS CLI commands for a natural language query

Architecture:
    Client -> Gateway (OAuth+MCP) -> Lambda (JSON) -> STS AssumeRole -> Payer boto3 API
"""

import json
import re

# Cross-account support - shared module is packaged alongside lambda_function.py
try:
    from shared.cross_account import get_aws_client
except ImportError:
    import boto3
    def get_aws_client(service_name, region_name=None, **kwargs):
        client_kwargs = {"region_name": region_name} if region_name else {}
        client_kwargs.update(kwargs)
        return boto3.client(service_name, **client_kwargs)


# ---------------------------------------------------------------------------
# CLI command parser: converts "aws <service> <operation> --flags" to boto3
# ---------------------------------------------------------------------------

# Map CLI service names to boto3 service names where they differ
SERVICE_NAME_MAP = {
    "s3api": "s3",
    "s3": "s3",
    "logs": "logs",
    "ce": "ce",
    "elbv2": "elbv2",
    "elb": "elb",
    "autoscaling": "autoscaling",
    "application-autoscaling": "application-autoscaling",
    "resource-groups": "resource-groups",
    "service-quotas": "service-quotas",
    "compute-optimizer": "compute-optimizer",
}

# High-level S3 commands that map to specific boto3 calls
S3_HIGH_LEVEL_MAP = {
    "ls": ("list_buckets", {}),  # s3 ls without a path
}


def kebab_to_snake(name):
    """Convert kebab-case to snake_case: describe-instances -> describe_instances."""
    return name.replace("-", "_")


def parse_cli_command(cli_command):
    """Parse an AWS CLI command string into (service, operation, params, region).

    Supports:
      - aws <service> <operation> --param-name value --flag
      - aws s3api <operation> ...
      - aws s3 ls (high-level, limited)

    Returns:
        tuple: (service_name, operation_name, params_dict, region_or_None)
    """
    # Strip leading "aws " if present
    cmd = cli_command.strip()
    if cmd.startswith("aws "):
        cmd = cmd[4:]

    tokens = _tokenize(cmd)
    if len(tokens) < 1:
        raise ValueError(f"Cannot parse CLI command: {cli_command}")

    service = tokens[0]
    boto3_service = SERVICE_NAME_MAP.get(service, service)

    # High-level s3 commands (s3 ls)
    if service == "s3" and len(tokens) >= 2 and not tokens[1].startswith("-"):
        op = tokens[1]
        if op in S3_HIGH_LEVEL_MAP:
            mapped_op, default_params = S3_HIGH_LEVEL_MAP[op]
            return "s3", mapped_op, dict(default_params), None
        raise ValueError(
            f"High-level 's3 {op}' is not supported. Use 's3api' equivalent instead."
        )

    if len(tokens) < 2:
        raise ValueError(f"Missing operation for service '{service}': {cli_command}")

    operation = tokens[1]
    boto3_operation = kebab_to_snake(operation)

    # Parse --flags
    params = {}
    region = None
    i = 2
    while i < len(tokens):
        token = tokens[i]
        if token == "--region" and i + 1 < len(tokens):
            region = tokens[i + 1]
            i += 2
            continue
        if token == "--output" and i + 1 < len(tokens):
            # Skip --output (we always return JSON)
            i += 2
            continue
        if token == "--profile" and i + 1 < len(tokens):
            # Skip --profile (we use cross-account role)
            i += 2
            continue
        if token == "--query" and i + 1 < len(tokens):
            # Skip --query (JMESPath filter, handled client-side)
            i += 2
            continue
        if token.startswith("--"):
            param_name = kebab_to_snake(token[2:])
            # Check if next token is a value or another flag
            if i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
                value = tokens[i + 1]
                # Try to parse JSON values
                params[param_name] = _parse_value(value)
                i += 2
            else:
                # Boolean flag
                params[param_name] = True
                i += 1
        else:
            i += 1

    return boto3_service, boto3_operation, params, region


def _tokenize(cmd):
    """Split command respecting quoted strings."""
    tokens = []
    current = []
    in_quote = None
    for ch in cmd:
        if ch in ('"', "'") and in_quote is None:
            in_quote = ch
        elif ch == in_quote:
            in_quote = None
        elif ch == " " and in_quote is None:
            if current:
                tokens.append("".join(current))
                current = []
            continue
        else:
            current.append(ch)
    if current:
        tokens.append("".join(current))
    return tokens


def _parse_value(value):
    """Try to parse a CLI value: JSON object/array, integer, or string."""
    if not value:
        return value
    # JSON object or array
    if (value.startswith("{") and value.endswith("}")) or (
        value.startswith("[") and value.endswith("]")
    ):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            pass
    # Integer
    try:
        return int(value)
    except ValueError:
        pass
    # Boolean strings
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    return value


# ---------------------------------------------------------------------------
# Tool: call_aws — execute AWS API via boto3 (cross-account)
# ---------------------------------------------------------------------------

# Read-only operations only — deny any write/mutate actions
DENIED_PREFIXES = (
    "create_", "delete_", "put_", "update_", "modify_",
    "terminate_", "stop_", "start_", "reboot_",
    "remove_", "detach_", "deregister_", "disable_",
    "enable_", "attach_", "associate_", "disassociate_",
    "run_", "execute_", "invoke_", "send_", "publish_",
    "tag_", "untag_", "import_", "export_",
    "allocate_", "release_", "revoke_", "authorize_",
    "cancel_", "restore_", "copy_", "move_",
)

ALLOWED_PREFIXES = (
    "describe_", "list_", "get_", "batch_get_",
    "lookup_", "search_", "head_",
)


def is_read_only(operation_name):
    """Check if a boto3 operation is read-only."""
    op = operation_name.lower()
    # Explicit allow list
    if any(op.startswith(p) for p in ALLOWED_PREFIXES):
        return True
    # Explicit deny list
    if any(op.startswith(p) for p in DENIED_PREFIXES):
        return False
    # Default deny for unknown operations
    return False


def handle_call_aws(event):
    """Execute an AWS API call via boto3 with cross-account assumed role."""
    cli_command = event.get("cli_command", "")
    if not cli_command:
        return {"error": "cli_command parameter is required"}

    try:
        service, operation, params, region = parse_cli_command(cli_command)
    except ValueError as e:
        return {"error": str(e)}

    # Safety: read-only operations only
    if not is_read_only(operation):
        return {
            "error": f"Operation '{operation}' is not allowed. Only read-only operations "
                     f"(describe_*, list_*, get_*) are permitted on cross-account resources.",
            "cli_command": cli_command,
        }

    try:
        client = get_aws_client(service, region_name=region)
        api_method = getattr(client, operation, None)
        if api_method is None:
            return {
                "error": f"Operation '{operation}' not found on service '{service}'.",
                "hint": f"Check 'aws {service} help' for available operations.",
            }

        # Call the API
        if params:
            response = api_method(**params)
        else:
            response = api_method()

        # Remove ResponseMetadata (noise)
        response.pop("ResponseMetadata", None)

        # Handle pagination: if there's a NextToken/Marker, note it
        result = _serialize_response(response)
        return {"cli_command": cli_command, "result": result}

    except client.exceptions.ClientError as e:
        return {
            "error": str(e),
            "cli_command": cli_command,
            "error_code": e.response["Error"]["Code"],
        }
    except Exception as e:
        return {"error": str(e), "cli_command": cli_command}


def _serialize_response(obj):
    """Make boto3 response JSON-serializable (handle datetime, bytes, etc.)."""
    if isinstance(obj, dict):
        return {k: _serialize_response(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_serialize_response(i) for i in obj]
    elif hasattr(obj, "isoformat"):
        return obj.isoformat()
    elif isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    else:
        return obj


# ---------------------------------------------------------------------------
# Tool: suggest_aws_commands — natural language to CLI suggestions
# ---------------------------------------------------------------------------

# Common resource query patterns
COMMAND_SUGGESTIONS = {
    "s3": [
        "aws s3api list-buckets",
        "aws s3api list-objects-v2 --bucket <bucket-name>",
        "aws s3api get-bucket-location --bucket <bucket-name>",
        "aws s3api get-bucket-tagging --bucket <bucket-name>",
    ],
    "ec2": [
        "aws ec2 describe-instances",
        "aws ec2 describe-instances --region <region>",
        "aws ec2 describe-security-groups",
        "aws ec2 describe-vpcs",
        "aws ec2 describe-subnets",
        "aws ec2 describe-volumes",
    ],
    "rds": [
        "aws rds describe-db-instances",
        "aws rds describe-db-clusters",
        "aws rds describe-db-snapshots",
    ],
    "lambda": [
        "aws lambda list-functions",
        "aws lambda list-functions --region <region>",
        "aws lambda get-function --function-name <name>",
    ],
    "iam": [
        "aws iam list-roles",
        "aws iam list-users",
        "aws iam list-policies --scope Local",
    ],
    "ecs": [
        "aws ecs list-clusters",
        "aws ecs list-services --cluster <cluster>",
        "aws ecs describe-services --cluster <cluster> --services <service>",
    ],
    "eks": [
        "aws eks list-clusters",
        "aws eks describe-cluster --name <cluster>",
    ],
    "cloudwatch": [
        "aws cloudwatch list-metrics --namespace AWS/EC2",
        "aws cloudwatch describe-alarms",
    ],
    "organizations": [
        "aws organizations list-accounts",
        "aws organizations describe-organization",
    ],
    "compute-optimizer": [
        "aws compute-optimizer get-ec2-instance-recommendations",
        "aws compute-optimizer get-ebs-volume-recommendations",
        "aws compute-optimizer get-lambda-function-recommendations",
    ],
    "cost-explorer": [
        "aws ce get-cost-and-usage --time-period Start=2026-08-01,End=2026-09-01 --granularity MONTHLY --metrics UnblendedCost",
    ],
}


def handle_suggest_aws_commands(event):
    """Suggest AWS CLI commands based on a natural language query."""
    query = event.get("query", "")
    if not query:
        return {"error": "query parameter is required"}

    query_lower = query.lower()
    suggestions = []

    for service, commands in COMMAND_SUGGESTIONS.items():
        if service in query_lower or any(
            kw in query_lower
            for kw in _service_keywords(service)
        ):
            suggestions.extend(commands)

    # If no specific match, return general exploration commands
    if not suggestions:
        suggestions = [
            "aws organizations list-accounts",
            "aws ec2 describe-instances",
            "aws s3api list-buckets",
            "aws rds describe-db-instances",
            "aws lambda list-functions",
            "aws iam list-roles",
        ]

    return {
        "query": query,
        "suggestions": suggestions[:10],
        "note": "These commands will execute in the payer/management account via cross-account assumed role. Only read-only operations are allowed.",
    }


def _service_keywords(service):
    """Return common keywords for a service."""
    keywords = {
        "s3": ["bucket", "object", "storage"],
        "ec2": ["instance", "server", "vm", "compute", "volume", "ebs", "vpc", "subnet", "security group"],
        "rds": ["database", "db", "mysql", "postgres", "aurora"],
        "lambda": ["function", "serverless"],
        "iam": ["role", "user", "policy", "permission"],
        "ecs": ["container", "task", "fargate"],
        "eks": ["kubernetes", "k8s", "cluster"],
        "cloudwatch": ["metric", "alarm", "monitoring", "log"],
        "organizations": ["account", "organization", "org", "member"],
        "compute-optimizer": ["rightsizing", "optimization", "recommendation", "utilization"],
        "cost-explorer": ["cost", "spend", "billing"],
    }
    return keywords.get(service, [])


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------

def lambda_handler(event, context):
    """Main Lambda handler — routes to tool handlers based on Gateway context."""
    print(f"Event: {json.dumps(event)}")

    extended_tool_name = context.client_context.custom["bedrockAgentCoreToolName"]
    tool_name = extended_tool_name.split("___")[1]
    print(f"Tool name: {tool_name}")

    handlers = {
        "call_aws": handle_call_aws,
        "suggest_aws_commands": handle_suggest_aws_commands,
    }

    handler = handlers.get(tool_name)
    if handler:
        return handler(event)
    else:
        return {"error": f"Unknown tool: {tool_name}", "available_tools": list(handlers.keys())}
