#!/usr/bin/env python3
"""Explicit optional producer entry point; ordinary TWILL stays offline."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import argparse
import json

from twill_archive_feed import InvalidPolicy, _policy, _private_read, reap, run


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--reap", action="store_true", help="reclaim verified consumed local cache without network access")
    args = parser.parse_args(argv)
    client = None
    try:
        policy = _policy(_private_read(args.policy))
        if policy["enabled"] and not args.reap:
            import boto3
            from botocore.config import Config

            credentials = _private_read(policy["credentials_file"])
            if not isinstance(credentials, dict) or any(
                not isinstance(credentials.get(k), str) or not credentials[k]
                for k in ("access_key", "secret_key")
            ):
                raise InvalidPolicy()
            client = boto3.client(
                "s3", endpoint_url=policy["endpoint_url"],
                aws_access_key_id=credentials["access_key"],
                aws_secret_access_key=credentials["secret_key"],
                region_name=policy.get("region_name", "us-east-1"),
                config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                              retries={"max_attempts": 0},
                              connect_timeout=min(5, policy["max_seconds"]),
                              # Cold listing pages may take several seconds;
                              # retain the overall producer and service caps.
                              read_timeout=min(15, policy["max_seconds"])),
            )
    except Exception:
        policy = None
    report = reap(policy) if args.reap else run(policy, client=client)
    print(json.dumps(report, sort_keys=True))
    return 1 if report.get("errors") or report.get("retention_unresolved") else 0

if __name__ == "__main__":
    raise SystemExit(main())
