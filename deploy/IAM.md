# IAM setup for kalshibot

A dedicated IAM user in account **073158194660**, separate from the `kin` user that
belongs to another project. Separate credentials mean this project can be rotated or
revoked on its own, and CloudTrail attributes actions to the right work.

## What the policy allows

`deploy/iam-policy.json` is scoped four ways:

1. **Region** — every statement is conditioned on `aws:RequestedRegion` being
   `us-east-1` or `us-west-2`. The other project defaults to `us-east-2`, so the two
   do not overlap.
2. **Tag on create** — `RunInstances` only succeeds if the instance and its volumes
   carry `Project=kalshibot`. An untagged launch is denied.
3. **Tag on manage** — stop, start, reboot, terminate and volume resize only apply to
   resources already tagged `Project=kalshibot`. It cannot touch the other project's
   instances even inside the capture regions.
4. **No role passing** — an explicit `Deny` on `iam:PassRole` and instance-profile
   association. The capture box talks only to Kalshi and needs no AWS API access at
   all, so it must never be handed a role. This is a deny, so it cannot be overridden
   by a later policy attachment.

Because tag-on-create and tag-on-manage use the same tag, there is no footgun where
you create something you cannot later delete.

**Known looseness:** security group and key pair management is region-scoped but not
tag-scoped, because those resources must exist before an instance can reference them.
If the other project ever moves into `us-east-1` or `us-west-2`, tighten this.

## Console steps (as root)

Sign in to account **073158194660** as root.

1. **IAM → Policies → Create policy**. Pick the **JSON** tab, replace the contents
   with `deploy/iam-policy.json`, click Next. Name it `KalshibotCapture`. Create.
2. **IAM → Users → Create user**. Name it `kalshibot`. Leave *"Provide user access to
   the AWS Management Console"* **unchecked** — this is a programmatic-only identity.
   Next.
3. **Permissions options → Attach policies directly**. Search `KalshibotCapture`,
   check it, Next, Create user.
4. Open the new user → **Security credentials** → **Create access key**. Choose
   **Command Line Interface (CLI)**, acknowledge the recommendation, Next, Create.
5. Copy the **Access key ID** and **Secret access key**. The secret is shown once.

While you are in the console as root: if root does not have MFA enabled, turn it on.
Root in an account holding real credentials is worth protecting.

## Local configuration

```bash
aws configure --profile kalshibot
```

Enter the access key ID, the secret, `us-east-1` as the default region, and `json`.

Verify it is the right identity before creating anything:

```bash
aws sts get-caller-identity --profile kalshibot
```

Expect account `073158194660` and an ARN ending `:user/kalshibot`.

Confirm the region guard actually works — this should be **denied**:

```bash
aws ec2 describe-instances --profile kalshibot --region eu-west-1
```

An `UnauthorizedOperation` there means the policy is doing its job.

## Never

- Do not use the `default` profile. It is root on a **different** account
  (536697265987).
- Do not use the `kin` profile. Right account, wrong project.
- Do not attach an instance role to the capture boxes. They need no AWS access.
- Do not commit the access key. `.gitignore` covers `.env` and `*.pem`, but the safest
  place is `~/.aws/credentials`, which is outside the repo.
