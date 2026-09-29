#!/usr/bin/env bash
# Create harness-box: Ubuntu 24.04, m6i.xlarge, 200 GB gp3, same key and security group as the
# existing boxes. Prints the instance id. Idempotent by Name tag.
#   KEY_NAME=<key pair> SG=sg-... ./launch-harness-box.sh
set -euo pipefail
REGION="${AWS_DEFAULT_REGION:-us-east-2}"
KEY_NAME="${KEY_NAME:?set KEY_NAME (the EC2 key pair name)}"
SG="${SG:?set SG (security group id)}"
TYPE="${TYPE:-m6i.xlarge}"
NAME="${NAME:-harness-box}"
existing="$(aws ec2 describe-instances --region "$REGION" --filters "Name=tag:Name,Values=$NAME" "Name=instance-state-name,Values=pending,running,stopping,stopped" --query 'Reservations[].Instances[].InstanceId' --output text)"
if [ -n "$existing" ]; then echo "$existing"; exit 0; fi
AMI="$(aws ssm get-parameter --region "$REGION" --name /aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id --query Parameter.Value --output text)"
aws ec2 run-instances --region "$REGION" --image-id "$AMI" --instance-type "$TYPE" --key-name "$KEY_NAME" \
  --security-group-ids "$SG" \
  --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":200,"VolumeType":"gp3","DeleteOnTermination":true}}]' \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$NAME},{Key=adv-loop,Value=harness}]" \
  --metadata-options HttpTokens=required \
  --query 'Instances[0].InstanceId' --output text
