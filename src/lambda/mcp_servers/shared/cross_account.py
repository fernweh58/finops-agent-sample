"""Cross-account AWS session management for MCP (Model Context Protocol) Lambda functions.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

Used when deploying to data collection account with access to
Cost Explorer and CUR data in the management/payer account.

Fixed: replaced @lru_cache with time-based refresh. STS temporary credentials
expire after DurationSeconds (default 3600s), but Lambda containers can live
longer. The original lru_cache would serve expired credentials indefinitely.
Now refreshes 5 minutes before expiry.
"""

import os
import time
import logging

import boto3

logger = logging.getLogger(__name__)

# Module-level cache (survives across Lambda invocations in the same container)
_cached_session = None
_cached_expiry = 0  # Unix timestamp when credentials expire
_REFRESH_BUFFER_SECONDS = 300  # Refresh 5 minutes before actual expiry
_DURATION_SECONDS = 3600


def get_cross_account_session():
    """Get boto3 session with assumed role credentials.

    Caches the session and automatically refreshes when credentials are
    within 5 minutes of expiry. Safe for long-lived Lambda containers.

    Returns:
        boto3.Session with assumed role credentials, or None if not configured.
    """
    global _cached_session, _cached_expiry

    role_arn = os.environ.get("CROSS_ACCOUNT_ROLE_ARN", "")
    external_id = os.environ.get("CROSS_ACCOUNT_EXTERNAL_ID", "")

    if not role_arn:
        return None

    # Return cached session if still valid (with buffer)
    if _cached_session and time.time() < (_cached_expiry - _REFRESH_BUFFER_SECONDS):
        return _cached_session

    logger.info("Assuming cross-account role (new or refreshed credentials)")

    sts = boto3.client("sts")
    params = {
        "RoleArn": role_arn,
        "RoleSessionName": f"mcp-{os.environ.get('AWS_LAMBDA_FUNCTION_NAME', 'unknown')}"[:64],
        "DurationSeconds": _DURATION_SECONDS,
    }
    if external_id:
        params["ExternalId"] = external_id

    try:
        response = sts.assume_role(**params)
        creds = response["Credentials"]
        _cached_session = boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
        )
        # Use the actual Expiration from STS response
        _cached_expiry = creds["Expiration"].timestamp()
        return _cached_session
    except Exception:
        logger.exception("Failed to assume cross-account role: %s", role_arn)
        # Clear cache on failure so next call retries
        _cached_session = None
        _cached_expiry = 0
        raise


def get_aws_client(service_name, region_name=None, **kwargs):
    """Get boto3 client - uses cross-account role if configured, else execution role.

    Args:
        service_name: AWS service name (e.g., 'ce', 'athena', 's3')
        region_name: Optional AWS region
        **kwargs: Additional arguments passed to boto3.client()

    Returns:
        boto3 client for the specified service
    """
    session = get_cross_account_session()
    client_kwargs = {"region_name": region_name} if region_name else {}
    client_kwargs.update(kwargs)

    if session:
        return session.client(service_name, **client_kwargs)
    return boto3.client(service_name, **client_kwargs)
