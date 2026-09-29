#!/usr/bin/env bash
# Owner-run from the Mac: give harness-box an instance profile that can describe instances,
# start and stop only instances tagged adv-loop=gpu, and read Cost Explorer. adv-gpu-box gets
# no profile. Prints every mutating command and runs none of them unless --apply is given;
# each object is checked first so a second --apply changes nothing.
#   ADV_LOOP_HARNESS_BOX=i-... ./setup-iam.sh            # dry run
#   ADV_LOOP_HARNESS_BOX=i-... ./setup-iam.sh --apply
set -euo pipefail
REGION="${AWS_DEFAULT_REGION:-us-east-2}"
HARNESS_BOX="${ADV_LOOP_HARNESS_BOX:?set ADV_LOOP_HARNESS_BOX (the harness-box instance id)}"
ROLE="${ADV_IAM_ROLE:-adv-harness-box}"
POLICY_NAME="${ADV_IAM_POLICY:-adv-harness-box-gpu}"
PROFILE="${ADV_IAM_PROFILE:-adv-harness-box}"
HERE="$(cd "$(dirname "$0")" && pwd)"
IAM="$HERE/iam"
APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1

run() {
  if [ "$APPLY" = "1" ]; then
    echo "+ $*" >&2
    "$@"
  else
    echo "dry-run: $*"
  fi
}

if aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
  echo "role $ROLE exists"
else
  run aws iam create-role --role-name "$ROLE" --description "adv-loop harness-box: start/stop the GPU box, read Cost Explorer" \
    --assume-role-policy-document "file://$IAM/adv-harness-box-trust.json"
fi
if aws iam get-role-policy --role-name "$ROLE" --policy-name "$POLICY_NAME" >/dev/null 2>&1; then
  echo "inline policy $POLICY_NAME exists (re-put to refresh)"
  run aws iam put-role-policy --role-name "$ROLE" --policy-name "$POLICY_NAME" --policy-document "file://$IAM/adv-harness-box-policy.json"
else
  run aws iam put-role-policy --role-name "$ROLE" --policy-name "$POLICY_NAME" --policy-document "file://$IAM/adv-harness-box-policy.json"
fi
if aws iam get-instance-profile --instance-profile-name "$PROFILE" >/dev/null 2>&1; then
  echo "instance profile $PROFILE exists"
else
  run aws iam create-instance-profile --instance-profile-name "$PROFILE"
fi
attached="$(aws iam get-instance-profile --instance-profile-name "$PROFILE" --query 'InstanceProfile.Roles[].RoleName' --output text 2>/dev/null || true)"
case " $attached " in
  *" $ROLE "*) echo "role $ROLE is in profile $PROFILE" ;;
  *) run aws iam add-role-to-instance-profile --instance-profile-name "$PROFILE" --role-name "$ROLE"
     run sleep 10 ;;
esac
association="$(aws ec2 describe-iam-instance-profile-associations --region "$REGION" \
  --filters "Name=instance-id,Values=$HARNESS_BOX" "Name=state,Values=associating,associated" \
  --query 'IamInstanceProfileAssociations[0].IamInstanceProfile.Arn' --output text 2>/dev/null || true)"
case "$association" in
  *":instance-profile/$PROFILE") echo "$HARNESS_BOX already has profile $PROFILE" ;;
  ""|None) run aws ec2 associate-iam-instance-profile --region "$REGION" --instance-id "$HARNESS_BOX" --iam-instance-profile "Name=$PROFILE" ;;
  *) echo "$HARNESS_BOX has another profile ($association); replace it by hand" >&2; exit 2 ;;
esac
[ "$APPLY" = "1" ] || echo "dry run only; re-run with --apply to create the objects above"
echo "verify after the next harness-box start:  ssh harness-box aws sts get-caller-identity"
