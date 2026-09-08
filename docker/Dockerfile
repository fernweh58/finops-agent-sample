# ARM64 image for awslabs aws-api-mcp-server, replacing the delisted
# AWS Marketplace container. Bedrock AgentCore Runtime requires linux/arm64.
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.12-slim
ARG MCP_VERSION=1.4.1
# Pin mcp<2: aws-api-mcp-server 1.4.1 imports `McpError` from mcp.shared.exceptions,
# which mcp 2.0 renamed to `MCPError`. The package only constrains mcp>=1.23.0, so an
# unpinned install pulls mcp 2.x and crashes at import. mcp<2 keeps the 1.x API.
RUN pip install --no-cache-dir "awslabs.aws-api-mcp-server==${MCP_VERSION}" "mcp<2"
ENV AUTH_TYPE=no-auth \
    AWS_API_MCP_HOST=0.0.0.0 \
    AWS_API_MCP_PORT=8000 \
    AWS_API_MCP_TRANSPORT=streamable-http \
    AWS_API_MCP_STATELESS_HTTP=true \
    AWS_API_MCP_ALLOWED_HOSTS=* \
    AWS_API_MCP_ALLOWED_ORIGINS=* \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
EXPOSE 8000
CMD ["awslabs.aws-api-mcp-server"]
