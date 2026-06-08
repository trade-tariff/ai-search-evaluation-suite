#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Stop or terminate the EC2 eval host with cost guardrails.

Run export_eval_state.sh on the VM first. This script only controls AWS
infrastructure; it does not connect to the VM or pull logs.

Usage:
  aws_teardown_eval_host.sh --instance-id i-... --region REGION --mode stop|terminate [options]

Options:
  --snapshot-root                 Create an EBS snapshot of the root volume.
  --release-eip-allocation-id ID  Release an Elastic IP after teardown.
  --yes                           Skip confirmation prompt.
  -h, --help                      Show this help.

Behavior:
  stop       Stops compute billing while keeping the instance and root EBS.
  terminate Preserves the root EBS by setting DeleteOnTermination=false first,
            then terminates the instance.
USAGE
}

instance_id=""
region="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
mode=""
snapshot_root=0
release_eip_allocation_id=""
assume_yes=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --instance-id)
      instance_id="${2:-}"
      shift 2
      ;;
    --region)
      region="${2:-}"
      shift 2
      ;;
    --mode)
      mode="${2:-}"
      shift 2
      ;;
    --snapshot-root)
      snapshot_root=1
      shift
      ;;
    --release-eip-allocation-id)
      release_eip_allocation_id="${2:-}"
      shift 2
      ;;
    --yes)
      assume_yes=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [ -z "$instance_id" ] || [ -z "$region" ] || [ -z "$mode" ]; then
  usage >&2
  exit 2
fi

if [ "$mode" != "stop" ] && [ "$mode" != "terminate" ]; then
  echo "--mode must be stop or terminate" >&2
  exit 2
fi

if ! command -v aws >/dev/null 2>&1; then
  echo "Missing required command: aws" >&2
  exit 127
fi

root_device="$(
  aws ec2 describe-instances \
    --region "$region" \
    --instance-ids "$instance_id" \
    --query 'Reservations[0].Instances[0].RootDeviceName' \
    --output text
)"

root_volume_id="$(
  aws ec2 describe-instances \
    --region "$region" \
    --instance-ids "$instance_id" \
    --query "Reservations[0].Instances[0].BlockDeviceMappings[?DeviceName=='$root_device'].Ebs.VolumeId | [0]" \
    --output text
)"

if [ -z "$root_device" ] || [ "$root_device" = "None" ] || [ -z "$root_volume_id" ] || [ "$root_volume_id" = "None" ]; then
  echo "Could not resolve root device/volume for $instance_id" >&2
  exit 1
fi

echo "Instance: $instance_id"
echo "Region: $region"
echo "Mode: $mode"
echo "Root device: $root_device"
echo "Root volume: $root_volume_id"
echo "Snapshot root: $snapshot_root"
if [ -n "$release_eip_allocation_id" ]; then
  echo "Elastic IP allocation to release: $release_eip_allocation_id"
fi

if [ "$assume_yes" -ne 1 ]; then
  printf 'Continue? Type "teardown" to proceed: '
  read -r confirmation
  if [ "$confirmation" != "teardown" ]; then
    echo "Aborted."
    exit 1
  fi
fi

if [ "$mode" = "terminate" ]; then
  aws ec2 modify-instance-attribute \
    --region "$region" \
    --instance-id "$instance_id" \
    --block-device-mappings "[{\"DeviceName\":\"$root_device\",\"Ebs\":{\"DeleteOnTermination\":false}}]"
fi

if [ "$snapshot_root" -eq 1 ]; then
  aws ec2 stop-instances --region "$region" --instance-ids "$instance_id" >/dev/null
  aws ec2 wait instance-stopped --region "$region" --instance-ids "$instance_id"
  snapshot_id="$(
    aws ec2 create-snapshot \
      --region "$region" \
      --volume-id "$root_volume_id" \
      --description "ai-search-evaluation-suite eval host root backup $(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      --tag-specifications "ResourceType=snapshot,Tags=[{Key=Project,Value=ai-search-evaluation-suite},{Key=Purpose,Value=classification-evals-teardown},{Key=SourceInstance,Value=$instance_id}]" \
      --query 'SnapshotId' \
      --output text
  )"
  echo "Snapshot created: $snapshot_id"
elif [ "$mode" = "stop" ]; then
  aws ec2 stop-instances --region "$region" --instance-ids "$instance_id" >/dev/null
  aws ec2 wait instance-stopped --region "$region" --instance-ids "$instance_id"
fi

if [ "$mode" = "terminate" ]; then
  aws ec2 terminate-instances --region "$region" --instance-ids "$instance_id" >/dev/null
  aws ec2 wait instance-terminated --region "$region" --instance-ids "$instance_id"
  echo "Instance terminated; root volume preserved: $root_volume_id"
else
  echo "Instance stopped; root volume remains attached: $root_volume_id"
fi

if [ -n "$release_eip_allocation_id" ]; then
  aws ec2 release-address --region "$region" --allocation-id "$release_eip_allocation_id"
  echo "Elastic IP released: $release_eip_allocation_id"
fi
