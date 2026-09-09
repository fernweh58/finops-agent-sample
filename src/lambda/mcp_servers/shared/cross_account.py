"""Cross-account AWS session management for MCP (Model Context Protocol) Lambda functions.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

Used when deploying to data collection account with access to
Cost Explorer and CUR data in the management/payer account.

Supports two modes:
1. Default (no account_id): assume CROSS_ACCOUNT_ROLE_ARN (Payer role)
2. Dynamic (account_id provided): assume finops-mcp-readonly role in that account
   using MEMBER_ROLE_NAME and MEMBER_EXTERNAL_ID env vars.

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

# Module-level cache: keyed by account_id (None = Payer default)
_cached_sessions = {}  # {account_id_or_none: (session, expiry_timestamp)}
_REFRESH_BUFFER_SECONDS = 300  # Refresh 5 minutes before actual expiry
_DURATION_SECONDS = 3600


def _assume_role(role_arn, external_id, session_suffix=""):
    """Assume an IAM role and return (boto3.Session, expiry_timestamp)."""
    sts = boto3.client("sts")
    func_name = os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "unknown")
    session_name = f"mcp-{func_name}{session_suffix}"[:64]

    params = {
        "RoleArn": role_arn,
        "RoleSessionName": session_name,
        "DurationSeconds": _DURATION_SECONDS,
    }
    if external_id:
        params["ExternalId"] = external_id

    response = sts.assume_role(**params)
    creds = response["Credentials"]
    session = boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )
    expiry = creds["Expiration"].timestamp()
    return session, expiry


def get_cross_account_session(account_id=None):
    """Get boto3 session with assumed role credentials.

    Args:
        account_id: Target AWS account ID. None = use default CROSS_ACCOUNT_ROLE_ARN
                     (Payer). If provided, assumes MEMBER_ROLE_NAME in that account.

    Returns:
        boto3.Session with assumed role credentials, or None if not configured.
    """
    global _cached_sessions

    if account_id:
        # Dynamic mode: assume role in the specified member account
        role_name = os.environ.get("MEMBER_ROLE_NAME", "finops-mcp-readonly")
        external_id = os.environ.get("MEMBER_EXTERNAL_ID", "")
        role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"
        cache_key = account_id
    else:
        # Default mode: assume the Payer cross-account role
        role_arn = os.environ.get("CROSS_ACCOUNT_ROLE_ARN", "")
        external_id = os.environ.get("CROSS_ACCOUNT_EXTERNAL_ID", "")
        cache_key = None

    if not role_arn:
        return None

    # Return cached session if still valid (with buffer)
    cached = _cached_sessions.get(cache_key)
    if cached:
        session, expiry = cached
        if time.time() < (expiry - _REFRESH_BUFFER_SECONDS):
            return session

    target_desc = f"account {account_id}" if account_id else "Payer (default)"
    logger.info("Assuming role %s for %s (new or refreshed)", role_arn, target_desc)

    try:
        suffix = f"-{account_id[-4:]}" if account_id else ""
        session, expiry = _assume_role(role_arn, external_id, suffix)
        _cached_sessions[cache_key] = (session, expiry)
        return session
    except Exception:
        logger.exception("Failed to assume role: %s", role_arn)
        # Clear cache on failure so next call retries
        _cached_sessions.pop(cache_key, None)
        raise


def get_aws_client(service_name, region_name=None, account_id=None, **kwargs):
    """Get boto3 client - uses cross-account role if configured, else execution role.

    Args:
        service_name: AWS service name (e.g., 'ce', 'athena', 's3')
        region_name: Optional AWS region
        account_id: Optional target account ID for dynamic assume
        **kwargs: Additional arguments passed to boto3.client()

    Returns:
        boto3 client for the specified service
    """
    session = get_cross_account_session(account_id=account_id)
    client_kwargs = {"region_name": region_name} if region_name else {}
    client_kwargs.update(kwargs)

    if session:
        return session.client(service_name, **client_kwargs)
    return boto3.client(service_name, **client_kwargs)
