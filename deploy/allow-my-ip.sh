#!/usr/bin/env bash
# Authorise the current public IP for SSH on the capture security groups, and
# drop any previously authorised IPs. Public IPs change with the network you are
# on, and a stale allowlist is both a lockout and a slow accumulation of holes.
set -euo pipefail

PROFILE="${KALSHI_AWS_PROFILE:-kalshibot}"
REGIONS="${KALSHI_REGIONS:-us-east-1 us-west-2}"
SG_NAME="kalshibot-capture"

myip=$(curl -fsS --max-time 10 https://checkip.amazonaws.com | tr -d '[:space:]')
[ -n "$myip" ] || { echo "could not determine public IP" >&2; exit 1; }
echo "current public IP: $myip"

for region in $REGIONS; do
    sg=$(aws ec2 describe-security-groups --profile "$PROFILE" --region "$region" \
            --filters "Name=group-name,Values=$SG_NAME" \
            --query 'SecurityGroups[0].GroupId' --output text)
    [ "$sg" != "None" ] || { echo "  $region: no $SG_NAME group"; continue; }

    existing=$(aws ec2 describe-security-groups --profile "$PROFILE" --region "$region" \
                --group-ids "$sg" \
                --query "SecurityGroups[0].IpPermissions[?FromPort==\`22\`].IpRanges[].CidrIp" \
                --output text)

    if ! printf '%s\n' $existing | grep -qx "$myip/32"; then
        aws ec2 authorize-security-group-ingress --profile "$PROFILE" --region "$region" \
            --group-id "$sg" --protocol tcp --port 22 --cidr "$myip/32" >/dev/null
        echo "  $region $sg: authorised $myip/32"
    else
        echo "  $region $sg: already authorised"
    fi

    for cidr in $existing; do
        [ "$cidr" = "$myip/32" ] && continue
        aws ec2 revoke-security-group-ingress --profile "$PROFILE" --region "$region" \
            --group-id "$sg" --protocol tcp --port 22 --cidr "$cidr" >/dev/null 2>&1 \
            && echo "  $region $sg: revoked stale $cidr"
    done
done
