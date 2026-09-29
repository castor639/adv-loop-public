#!/usr/bin/env bash
# Create adv-gpu-box once from the Mac: g5.xlarge (one A10G) from the pinned DLAMI, 200 GB gp3,
# tagged adv-loop=gpu on the instance and its volume, IMDSv2 with hop limit 1, and an OS
# poweroff mapped to an EC2 stop so the box-side watchdog can stop it. Idempotent by Name tag.
# The harness-box public key (adv-harness gpu keygen) is baked into cloud-config restricted to
# the VPC, so only harness-box can reach the box over ssh.
#   KEY_NAME=<key pair> SG=sg-... GPU_AUTHORIZED_KEY="$(ssh harness-box cat /srv/adv-loop/state/gpu-box.key.pub)" ./launch-gpu-box.sh
# Tries each AZ in AZS in turn on InsufficientInstanceCapacity. Prints: <id> <az> <private ip>.
set -euo pipefail
REGION="${AWS_DEFAULT_REGION:-us-east-2}"
KEY_NAME="${KEY_NAME:?set KEY_NAME (the EC2 key pair name)}"
SG="${SG:?set SG (security group id)}"
NAME="${NAME:-adv-gpu-box}"
TYPE="${TYPE:-g5.xlarge}"
AMI="${AMI:-ami-07639bdb1dc95014c}"
AZS="${AZS:-us-east-2a us-east-2b us-east-2c}"
VOLUME_GB="${VOLUME_GB:-200}"
VPC_CIDR="${VPC_CIDR:-172.31.0.0/16}"
HERE="$(cd "$(dirname "$0")" && pwd)"
TEMPLATE="${USER_DATA_TEMPLATE:-$HERE/gpu-box-user-data.yaml}"

if [ -z "${GPU_AUTHORIZED_KEY:-}" ] && [ -n "${GPU_AUTHORIZED_KEY_FILE:-}" ]; then
  GPU_AUTHORIZED_KEY="$(cat "$GPU_AUTHORIZED_KEY_FILE")"
fi
: "${GPU_AUTHORIZED_KEY:?set GPU_AUTHORIZED_KEY (the harness-box gpu-box.key.pub line) or GPU_AUTHORIZED_KEY_FILE}"
case "$GPU_AUTHORIZED_KEY" in
  ssh-ed25519\ *) ;;
  *) echo "GPU_AUTHORIZED_KEY must be an ssh-ed25519 public key line" >&2; exit 2 ;;
esac

existing="$(aws ec2 describe-instances --region "$REGION" \
  --filters "Name=tag:Name,Values=$NAME" "Name=instance-state-name,Values=pending,running,stopping,stopped" \
  --query 'Reservations[].Instances[].InstanceId' --output text)"
if [ -n "$existing" ] && [ "$existing" != "None" ]; then
  echo "$existing already exists" >&2
  echo "$existing"
  exit 0
fi

rendered="$(mktemp -t gpu-box-user-data.XXXXXX)"
trap 'rm -f "$rendered"' EXIT
# sed replacement text: escape the three characters that mean something there.
key_escaped="$(printf '%s' "$GPU_AUTHORIZED_KEY" | sed -e 's/[\\&|]/\\&/g')"
cidr_escaped="$(printf '%s' "$VPC_CIDR" | sed -e 's/[\\&|]/\\&/g')"
sed -e "s|__GPU_AUTHORIZED_KEY__|$key_escaped|" -e "s|__VPC_CIDR__|$cidr_escaped|" "$TEMPLATE" > "$rendered"
if grep -q '__GPU_AUTHORIZED_KEY__\|__VPC_CIDR__' "$rendered"; then
  echo "user-data placeholders not rendered" >&2
  exit 2
fi

tags="[{Key=Name,Value=$NAME},{Key=adv-loop,Value=gpu}]"
instance=""
for az in $AZS; do
  subnet="$(aws ec2 describe-subnets --region "$REGION" \
    --filters "Name=availability-zone,Values=$az" "Name=default-for-az,Values=true" \
    --query 'Subnets[0].SubnetId' --output text)"
  if [ -z "$subnet" ] || [ "$subnet" = "None" ]; then
    echo "no default subnet in $az; skipping" >&2
    continue
  fi
  err="$(mktemp -t gpu-box-run.XXXXXX)"
  if out="$(aws ec2 run-instances --region "$REGION" --image-id "$AMI" --instance-type "$TYPE" \
      --key-name "$KEY_NAME" --security-group-ids "$SG" --subnet-id "$subnet" \
      --block-device-mappings "[{\"DeviceName\":\"/dev/sda1\",\"Ebs\":{\"VolumeSize\":$VOLUME_GB,\"VolumeType\":\"gp3\",\"DeleteOnTermination\":true}}]" \
      --tag-specifications "ResourceType=instance,Tags=$tags" "ResourceType=volume,Tags=$tags" \
      --metadata-options "HttpTokens=required,HttpPutResponseHopLimit=1,HttpEndpoint=enabled" \
      --instance-initiated-shutdown-behavior stop \
      --user-data "file://$rendered" \
      --query 'Instances[0].InstanceId' --output text 2>"$err")"; then
    instance="$out"
    rm -f "$err"
    break
  fi
  if grep -q 'InsufficientInstanceCapacity\|Unsupported' "$err"; then
    echo "no $TYPE capacity in $az; trying the next AZ" >&2
    rm -f "$err"
    continue
  fi
  cat "$err" >&2
  rm -f "$err"
  exit 1
done
if [ -z "$instance" ] || [ "$instance" = "None" ]; then
  echo "no AZ had $TYPE capacity; retry later" >&2
  exit 3
fi

aws ec2 wait instance-running --region "$REGION" --instance-ids "$instance"
aws ec2 describe-instances --region "$REGION" --instance-ids "$instance" \
  --query 'Reservations[0].Instances[0].[InstanceId,Placement.AvailabilityZone,PrivateIpAddress]' --output text
echo "next: set ADV_LOOP_GPU_BOX=$instance in /etc/adv-loop/env.d/harness on harness-box, restart adv-harness, then adv-ctl gpu bootstrap" >&2
