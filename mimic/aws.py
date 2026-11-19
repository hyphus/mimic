import boto3
import ipaddress
import logging

from botocore.exceptions import ClientError
from mimic.settings import load_runtime_settings

log = logging.getLogger(__name__)


def normalize_domain(domain: str) -> str:
    # strip port and trailing colon if present
    return domain.split(":", 1)[0].rstrip(".").lower()


def validate_aws_credentials(key: str = "", secret: str = "") -> bool:
    client = None
    if key and secret:
        client = boto3.client("sts", aws_access_key_id=key, aws_secret_access_key=secret)
    else:
        # Default to IAM profile
        client = boto3.client("sts")
    try:
        resp = client.get_caller_identity()  # This will throw if the creds are invalid
        log.info(f"AWS credentials are valid. Authenticated as '{resp.get("Arn")}'")
        return True
    except Exception as e:
        log.error(f"Failed to validate AWS credentials:\n{e}")
        return False


def _get_aws_client(service_name: str) -> boto3.client:
    aws = load_runtime_settings().aws
    if not aws.use_iam_role:
        log.debug("Authenticating with configured AWS access keys")
        return boto3.client(
            service_name,
            aws_access_key_id=aws.access_key_id,
            aws_secret_access_key=aws.secret_access_key,
        )
    else:
        log.debug("Authenticating with IAM role credentials")
        return boto3.client(service_name)


def _get_dns_zone(domain: str) -> str:
    try:
        client = _get_aws_client("route53")
        # TODO: support pagination to get all zones, unlikely to be needed but should support it
        resp = client.list_hosted_zones()

        # Find the most specific matching zone
        zones = {
            zone.get("Name").rstrip(".").lower(): zone
            for zone in resp.get("HostedZones", "")
        }

        labels = domain.split(".")

        for i in range(len(labels) - 1):
            zone = ".".join(labels[i:])
            if zone in zones:
                return zones[zone].get("Id", "")
    except ClientError as e:
        log.error(f"Failed to get Route53 zone for '{domain}':\n{e}")


def _is_valid_ip(ip_str):
    try:
        ipaddress.ip_address(ip_str)
        return True
    except ValueError:
        return False


def _get_dns_record(record: str, zone_id: str, ):
    client = _get_aws_client("route53")

    try:
        resp = client.list_resource_record_sets(
            HostedZoneId=zone_id,
            StartRecordName=record,
            MaxItems='1'
        )

        return resp.get('ResourceRecordSets', [])

    except Exception as e:
        log.error(f"Failed to get DNS record '{record}':\n{e}")
        return []


def create_dns_record(record: str, target: str, type: str = "") -> bool:
    record = normalize_domain(record)
    zone_id = _get_dns_zone(record)
    if not zone_id:
        log.error(f"Unable to find Route53 zone for {record}")
        return False

    client = _get_aws_client("route53")

    existing_records = _get_dns_record(record, zone_id)
    if len(existing_records) > 0:
        for existing in existing_records:
            if existing.get("Name", "").rstrip(".") == record:
                existing_values = [val["Value"] for val in existing.get("ResourceRecords", [])]
                if len(existing.get("ResourceRecords", [])) == 1 and target in existing_values:
                    log.debug("Record already exists, skipping")
                    return True

    try:
        if not type:
            if _is_valid_ip(target):
                type = "A"
            else:
                type = "CNAME"

        #
        if type == "TXT":
            target = f'"{target}"'

        client.change_resource_record_sets(HostedZoneId=zone_id, ChangeBatch={
            "Comment": "Created by Mimic",
            "Changes": [
                {
                    "Action": "UPSERT",
                    "ResourceRecordSet": {
                        "Name": record,
                        "Type": type,
                        "TTL": 60,
                        "ResourceRecords": [
                            {
                                "Value": target
                            }
                        ]
                    }
                }
            ]
        })

        log.info(f"Created DNS {type!r} record {record!r} -> {target!r}")
        return True
    except ClientError as e:
        log.error(f"Failed to create DNS record '{record}':\n{e}")


def delete_dns_record(record: str):
    record = normalize_domain(record)
    zone_id = _get_dns_zone(record)
    if not zone_id:
        log.error(f"Unable to find Route53 zone for {record}")
        return

    client = _get_aws_client("route53")
    log.debug(f"Deleting DNS record {record!r}")

    # list_resource_record_sets starts at the given name, so the result may be a
    # different record entirely.
    matching = [
        existing for existing in _get_dns_record(record, zone_id)
        if existing.get("Name", "").rstrip(".") == record
    ]
    if not matching:
        log.debug(f"Record '{record}' does not exist")
        return

    try:
        client.change_resource_record_sets(HostedZoneId=zone_id, ChangeBatch={
            "Changes": [
                {
                    "Action": "DELETE",
                    "ResourceRecordSet": existing,
                }
                for existing in matching
            ]
        })
        log.debug(f"Removed DNS record '{record}'")
    except ClientError as e:
        log.error(f"Failed to delete DNS record '{record}':\n{e}")
