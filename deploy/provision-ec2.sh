#!/usr/bin/env bash
# Provision one Kalshi capture instance. Idempotent: safe to re-run.
#   ./deploy/provision-ec2.sh us-east-1 use1
set -euo pipefail

REGION="${1:-}"
SITE="${2:-}"
if [ -z "$REGION" ] || [ -z "$SITE" ]; then
    echo "usage: $0 <region> <site-label>   e.g. $0 us-east-1 use1" >&2
    exit 2
fi

PROFILE="${KALSHI_AWS_PROFILE:-kalshibot}"
EXPECTED_ACCOUNT="073158194660"
INSTANCE_TYPE="${INSTANCE_TYPE:-t4g.small}"
VOLUME_GB="${VOLUME_GB:-200}"
KEY_NAME="${KEY_NAME:-kalshibot}"
PUBLIC_KEY="${PUBLIC_KEY:-$HOME/.ssh/id_ed25519.pub}"
SG_NAME="kalshibot-capture"
AMI_ALIAS="resolve:ssm:/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"

ec2() { aws ec2 --profile "$PROFILE" --region "$REGION" "$@"; }

echo "==> verifying account"
account=$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text)
if [ "$account" != "$EXPECTED_ACCOUNT" ]; then
    echo "refusing to continue: profile '$PROFILE' is account $account, expected $EXPECTED_ACCOUNT" >&2
    exit 1
fi
echo "    account $account via profile $PROFILE"

echo "==> key pair"
if ec2 describe-key-pairs --key-names "$KEY_NAME" >/dev/null 2>&1; then
    echo "    reusing $KEY_NAME"
else
    [ -f "$PUBLIC_KEY" ] || { echo "no public key at $PUBLIC_KEY" >&2; exit 1; }
    ec2 import-key-pair \
        --key-name "$KEY_NAME" \
        --public-key-material "fileb://$PUBLIC_KEY" \
        --tag-specifications "ResourceType=key-pair,Tags=[{Key=Project,Value=kalshibot}]" \
        >/dev/null
    echo "    imported $KEY_NAME from $PUBLIC_KEY"
fi

echo "==> security group"
vpc=$(ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)
[ "$vpc" != "None" ] || { echo "no default VPC in $REGION" >&2; exit 1; }

sg=$(ec2 describe-security-groups \
        --filters "Name=group-name,Values=$SG_NAME" "Name=vpc-id,Values=$vpc" \
        --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || echo "None")
if [ "$sg" = "None" ] || [ -z "$sg" ]; then
    sg=$(ec2 create-security-group \
            --group-name "$SG_NAME" \
            --description "Kalshi Phase 1 capture: SSH in, HTTPS out" \
            --vpc-id "$vpc" \
            --tag-specifications "ResourceType=security-group,Tags=[{Key=Project,Value=kalshibot}]" \
            --query GroupId --output text)
    echo "    created $sg in $vpc"
else
    echo "    reusing $sg"
fi

myip=$(curl -fsS --max-time 10 https://checkip.amazonaws.com | tr -d '[:space:]')
echo "    authorizing SSH from $myip/32"
ec2 authorize-security-group-ingress \
    --group-id "$sg" --protocol tcp --port 22 --cidr "$myip/32" \
    >/dev/null 2>&1 || echo "    (rule already present)"

echo "==> launching $INSTANCE_TYPE in $REGION (site=$SITE)"
instance=$(ec2 run-instances \
    --image-id "$AMI_ALIAS" \
    --instance-type "$INSTANCE_TYPE" \
    --key-name "$KEY_NAME" \
    --security-group-ids "$sg" \
    --metadata-options "HttpTokens=required,HttpEndpoint=enabled" \
    --block-device-mappings "DeviceName=/dev/xvda,Ebs={VolumeSize=$VOLUME_GB,VolumeType=gp3,DeleteOnTermination=true,Encrypted=true}" \
    --tag-specifications \
        "ResourceType=instance,Tags=[{Key=Project,Value=kalshibot},{Key=Site,Value=$SITE},{Key=Name,Value=kalshibot-capture-$SITE}]" \
        "ResourceType=volume,Tags=[{Key=Project,Value=kalshibot},{Key=Site,Value=$SITE}]" \
    --query 'Instances[0].InstanceId' --output text)
echo "    $instance"

echo "==> waiting for running state"
ec2 wait instance-running --instance-ids "$instance"
ip=$(ec2 describe-instances --instance-ids "$instance" \
        --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)

cat <<SUMMARY

    instance : $instance
    site     : $SITE
    region   : $REGION
    address  : $ip

Next:

  1. rsync the repo up (excludes venv, data and secrets):
       rsync -av --exclude .venv --exclude data --exclude '*.pem' \\
             ./ ec2-user@$ip:~/kalshibot/

  2. copy the Kalshi private key:
       ssh ec2-user@$ip 'mkdir -p ~/.kalshi && chmod 700 ~/.kalshi'
       scp ~/.kalshi/kalshi_key.pem ec2-user@$ip:~/.kalshi/kalshi_key.pem

  3. bootstrap and start:
       ssh ec2-user@$ip 'bash ~/kalshibot/deploy/bootstrap-instance.sh $SITE'

SUMMARY
