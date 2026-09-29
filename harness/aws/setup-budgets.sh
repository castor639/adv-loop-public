#!/usr/bin/env bash
# Owner-run from the Mac: the outside backstops for the $2,000 AWS pot.
#   adv-aws-monthly   $150/month, alerts at 50/80/100 percent, no action (a monthly stop would
#                     kill a job mid-run)
#   adv-aws-lifetime  $1,800 annual from 2026-09-01, alerts at 50/80/95 percent, and at 100
#                     percent a RUN_SSM_DOCUMENTS action that stops both instances through the
#                     adv-budgets-stop-ec2 role
#   adv-gpu-box-idle-stop  CloudWatch alarm: CPU under 3 percent for 45 minutes stops the GPU box
# Prints every mutating command and runs none unless --apply; each object is checked first.
#   ADV_LOOP_OWNER_EMAIL=you@example.com ADV_LOOP_HARNESS_BOX=i-... ADV_LOOP_GPU_BOX=i-... ./setup-budgets.sh [--apply]
set -euo pipefail
REGION="${AWS_DEFAULT_REGION:-us-east-2}"
EMAIL="${ADV_LOOP_OWNER_EMAIL:?set ADV_LOOP_OWNER_EMAIL}"
HARNESS_BOX="${ADV_LOOP_HARNESS_BOX:?set ADV_LOOP_HARNESS_BOX (the harness-box instance id)}"
GPU_BOX="${ADV_LOOP_GPU_BOX:?set ADV_LOOP_GPU_BOX (the adv-gpu-box instance id)}"
MONTHLY_USD="${ADV_BUDGET_MONTHLY_USD:-150}"
LIFETIME_USD="${ADV_BUDGET_LIFETIME_USD:-1800}"
LIFETIME_START="${ADV_BUDGET_START:-2026-09-01T00:00:00Z}"
ROLE="${ADV_BUDGET_ROLE:-adv-budgets-stop-ec2}"
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

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
ROLE_ARN="arn:aws:iam::$ACCOUNT:role/$ROLE"
SUBSCRIBER="{\"SubscriptionType\":\"EMAIL\",\"Address\":\"$EMAIL\"}"

notification() {
  printf '{"Notification":{"NotificationType":"ACTUAL","ComparisonOperator":"GREATER_THAN","Threshold":%s,"ThresholdType":"PERCENTAGE"},"Subscribers":[%s]}' "$1" "$SUBSCRIBER"
}

if aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
  echo "role $ROLE exists"
else
  run aws iam create-role --role-name "$ROLE" --description "adv-loop budget action: stop the boxes at the lifetime line" \
    --assume-role-policy-document "file://$IAM/adv-budgets-stop-ec2-trust.json"
  run aws iam put-role-policy --role-name "$ROLE" --policy-name "$ROLE" --policy-document "file://$IAM/adv-budgets-stop-ec2-policy.json"
fi

if aws budgets describe-budget --account-id "$ACCOUNT" --budget-name adv-aws-monthly >/dev/null 2>&1; then
  echo "budget adv-aws-monthly exists"
else
  run aws budgets create-budget --account-id "$ACCOUNT" \
    --budget "{\"BudgetName\":\"adv-aws-monthly\",\"BudgetLimit\":{\"Amount\":\"$MONTHLY_USD\",\"Unit\":\"USD\"},\"TimeUnit\":\"MONTHLY\",\"BudgetType\":\"COST\"}" \
    --notifications-with-subscribers "[$(notification 50),$(notification 80),$(notification 100)]"
fi

if aws budgets describe-budget --account-id "$ACCOUNT" --budget-name adv-aws-lifetime >/dev/null 2>&1; then
  echo "budget adv-aws-lifetime exists"
else
  run aws budgets create-budget --account-id "$ACCOUNT" \
    --budget "{\"BudgetName\":\"adv-aws-lifetime\",\"BudgetLimit\":{\"Amount\":\"$LIFETIME_USD\",\"Unit\":\"USD\"},\"TimeUnit\":\"ANNUALLY\",\"TimePeriod\":{\"Start\":\"$LIFETIME_START\"},\"BudgetType\":\"COST\"}" \
    --notifications-with-subscribers "[$(notification 50),$(notification 80),$(notification 95)]"
fi

actions="$(aws budgets describe-budget-actions-for-budget --account-id "$ACCOUNT" --budget-name adv-aws-lifetime \
  --query 'Actions[?ActionType==`RUN_SSM_DOCUMENTS`].ActionId' --output text 2>/dev/null || true)"
if [ -n "$actions" ] && [ "$actions" != "None" ]; then
  echo "lifetime stop action exists ($actions)"
else
  run aws budgets create-budget-action --account-id "$ACCOUNT" --budget-name adv-aws-lifetime \
    --notification-type ACTUAL --action-type RUN_SSM_DOCUMENTS \
    --action-threshold "ActionThresholdValue=100,ActionThresholdType=PERCENTAGE" \
    --definition "{\"SsmActionDefinition\":{\"ActionSubType\":\"STOP_EC2_INSTANCES\",\"Region\":\"$REGION\",\"InstanceIds\":[\"$HARNESS_BOX\",\"$GPU_BOX\"]}}" \
    --execution-role-arn "$ROLE_ARN" --approval-model AUTOMATIC --subscribers "[$SUBSCRIBER]"
fi

alarm="$(aws cloudwatch describe-alarms --region "$REGION" --alarm-names adv-gpu-box-idle-stop \
  --query 'MetricAlarms[0].AlarmName' --output text 2>/dev/null || true)"
if [ "$alarm" = "adv-gpu-box-idle-stop" ]; then
  echo "alarm adv-gpu-box-idle-stop exists"
else
  run aws cloudwatch put-metric-alarm --region "$REGION" --alarm-name adv-gpu-box-idle-stop \
    --alarm-description "adv-gpu-box idle: stop it" --namespace AWS/EC2 --metric-name CPUUtilization \
    --dimensions "Name=InstanceId,Value=$GPU_BOX" --statistic Average --period 300 \
    --evaluation-periods 9 --datapoints-to-alarm 9 --threshold 3 --comparison-operator LessThanThreshold \
    --treat-missing-data notBreaching --alarm-actions "arn:aws:automate:$REGION:ec2:stop"
fi
[ "$APPLY" = "1" ] || echo "dry run only; re-run with --apply to create the objects above"
echo "the budget subscriber $EMAIL must confirm the subscription email before alerts arrive"
