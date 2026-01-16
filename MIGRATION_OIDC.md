# Migrating to OIDC Authentication

This guide helps existing users migrate from access key authentication to OIDC authentication.

## Why Migrate?

**OIDC Benefits:**
- ✅ **More Secure**: No long-lived credentials stored in GitHub
- ✅ **Automatic Rotation**: Temporary credentials expire after each run
- ✅ **Better Audit**: CloudTrail shows role assumption events
- ✅ **Compliance**: Follows AWS security best practices
- ✅ **Zero Maintenance**: No manual key rotation needed

**Access Key Drawbacks:**
- ⚠️ Long-lived credentials require manual rotation
- ⚠️ Keys exposed in GitHub Secrets
- ⚠️ If leaked, immediate security incident

## Migration Steps

### Option 1: Update Existing Stack (Recommended)

This updates your existing CloudFormation stack to add OIDC support while keeping access keys working.

```bash
# 1. Update the stack
aws cloudformation update-stack \
  --stack-name ibkr-borrow-collector \
  --template-body file://cloudformation-template.yaml \
  --parameters \
    ParameterKey=UseOIDC,ParameterValue=true \
    ParameterKey=GitHubRepository,ParameterValue=YOUR_USERNAME/ibkr-borrow-collector \
  --capabilities CAPABILITY_NAMED_IAM

# 2. Wait for update to complete
aws cloudformation wait stack-update-complete \
  --stack-name ibkr-borrow-collector

# 3. Get the new Role ARN
export ROLE_ARN=$(aws cloudformation describe-stacks \
  --stack-name ibkr-borrow-collector \
  --query 'Stacks[0].Outputs[?OutputKey==`GitHubActionsRoleArn`].OutputValue' \
  --output text)

echo "Role ARN: $ROLE_ARN"

# 4. Update GitHub Secrets (Settings → Secrets → Actions)
gh secret set AWS_ROLE_ARN --body "$ROLE_ARN"

# 5. Remove old access key secrets (after verifying OIDC works)
gh secret remove AWS_ACCESS_KEY_ID
gh secret remove AWS_SECRET_ACCESS_KEY
```

### Option 2: Deploy New Stack

If you prefer a clean slate:

```bash
# 1. Export data from old bucket (optional)
export OLD_BUCKET=$(aws cloudformation describe-stacks \
  --stack-name ibkr-borrow-collector \
  --query 'Stacks[0].Outputs[?OutputKey==`BucketName`].OutputValue' \
  --output text)

# 2. Delete old stack (will delete bucket unless you set DeletionPolicy: Retain)
aws cloudformation delete-stack --stack-name ibkr-borrow-collector

# 3. Deploy new stack with OIDC
aws cloudformation create-stack \
  --stack-name ibkr-borrow-collector \
  --template-body file://cloudformation-template.yaml \
  --parameters \
    ParameterKey=UseOIDC,ParameterValue=true \
    ParameterKey=GitHubRepository,ParameterValue=YOUR_USERNAME/ibkr-borrow-collector \
  --capabilities CAPABILITY_NAMED_IAM

# 4. Configure GitHub Secrets (see DEPLOYMENT.md)
```

## Verification

After migration, verify OIDC is working:

```bash
# 1. Trigger a manual workflow run
gh workflow run collect.yml

# 2. Check the logs
gh run list --workflow=collect.yml --limit 1

# 3. View detailed logs
gh run view --log

# 4. Verify data in S3
aws s3 ls s3://$(aws cloudformation describe-stacks \
  --stack-name ibkr-borrow-collector \
  --query 'Stacks[0].Outputs[?OutputKey==`BucketName`].OutputValue' \
  --output text)/ibkr/borrow/$(date +%Y-%m-%d)/ --human-readable
```

## Rollback

If you need to rollback to access keys:

```bash
# 1. Update stack to disable OIDC
aws cloudformation update-stack \
  --stack-name ibkr-borrow-collector \
  --template-body file://cloudformation-template.yaml \
  --parameters \
    ParameterKey=UseOIDC,ParameterValue=false \
  --capabilities CAPABILITY_NAMED_IAM

# 2. Get new access keys
export AWS_KEY=$(aws cloudformation describe-stacks \
  --stack-name ibkr-borrow-collector \
  --query 'Stacks[0].Outputs[?OutputKey==`AccessKeyId`].OutputValue' \
  --output text)

export AWS_SECRET=$(aws cloudformation describe-stacks \
  --stack-name ibkr-borrow-collector \
  --query 'Stacks[0].Outputs[?OutputKey==`SecretAccessKey`].OutputValue' \
  --output text)

# 3. Update GitHub Secrets
gh secret set AWS_ACCESS_KEY_ID --body "$AWS_KEY"
gh secret set AWS_SECRET_ACCESS_KEY --body "$AWS_SECRET"
gh secret remove AWS_ROLE_ARN
```

## Troubleshooting

### Error: "User: anonymous is not authorized to perform: sts:AssumeRoleWithWebIdentity"

**Cause**: GitHub repository parameter doesn't match your actual repository

**Fix**:
```bash
# Update with correct repository name
aws cloudformation update-stack \
  --stack-name ibkr-borrow-collector \
  --template-body file://cloudformation-template.yaml \
  --parameters \
    ParameterKey=GitHubRepository,ParameterValue=YOUR_ACTUAL_USERNAME/YOUR_ACTUAL_REPO \
  --capabilities CAPABILITY_NAMED_IAM
```

### Error: "The OIDC provider already exists"

**Cause**: OIDC provider can only be created once per AWS account

**Fix**: This is normal if you've used OIDC before. The stack update will succeed - the provider is shared across stacks.

### Workflow fails with "Could not assume role"

**Checks**:
1. Verify `AWS_ROLE_ARN` secret is set correctly
2. Check repository name in CloudFormation matches GitHub repo
3. Ensure workflow has `permissions: id-token: write`
4. Verify IAM role trust policy includes your repository

```bash
# Check role trust policy
aws iam get-role \
  --role-name ibkr-borrow-collector-github-actions-role \
  --query 'Role.AssumeRolePolicyDocument'
```

## FAQ

**Q: Can I keep both OIDC and access keys?**

A: Not simultaneously. The stack creates either OIDC resources or access keys based on the `UseOIDC` parameter. However, the GitHub Actions workflow supports both methods - it will use OIDC if `AWS_ROLE_ARN` is set, otherwise falls back to access keys.

**Q: Does OIDC cost more?**

A: No, OIDC is free. In fact, it's slightly cheaper since you don't store credentials in Secrets Manager (if you were doing that).

**Q: What about Lambda/ECS deployments?**

A: Those use separate IAM roles (LambdaExecutionRole, ECSTaskRole) that already follow best practices. This migration only affects GitHub Actions authentication.

**Q: Do I need to update my collector code?**

A: No, boto3 automatically detects whether it's using access keys or temporary credentials from role assumption. The collector works with both methods.

**Q: Is this a breaking change?**

A: No, it's backward compatible. Existing deployments with access keys continue working unchanged. OIDC is opt-in via the `UseOIDC` parameter.

## Security Notes

- **OIDC tokens expire after 6 hours** (AWS default for web identity federation)
- **Each workflow run gets a unique session** with fresh credentials
- **CloudTrail logs show which GitHub repo/branch assumed the role**
- **Role can only be assumed by your specific GitHub repository** (enforced by trust policy)
- **Principle of least privilege**: Role only has S3 access to borrow data prefix

## Next Steps

After migrating to OIDC:

1. ✅ Remove old access keys from AWS (if not using for anything else)
2. ✅ Update documentation for your team
3. ✅ Consider rotating to OIDC for other GitHub Actions workflows
4. ✅ Monitor CloudTrail for role assumption events

For more details, see:
- [AWS IAM documentation on OIDC](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_providers_create_oidc.html)
- [GitHub Actions OIDC guide](https://docs.github.com/en/actions/deployment/security-hardening-your-deployments/configuring-openid-connect-in-amazon-web-services)
