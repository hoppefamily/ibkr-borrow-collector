# Deployment Guide

This guide covers deploying the IBKR borrow collector infrastructure on AWS and configuring automated data collection.

## Prerequisites

- AWS account with appropriate permissions
- AWS CLI installed and configured
- Docker installed (for testing)
- GitHub repository access (for GitHub Actions deployment)

## Quick Deploy with CloudFormation

### 1. Deploy Infrastructure

Deploy the CloudFormation stack to create the S3 bucket and IAM resources with OIDC authentication:

```bash
# Basic deployment with OIDC authentication
aws cloudformation create-stack \
  --stack-name ibkr-borrow-collector \
  --template-body file://cloudformation-template.yaml \
  --parameters \
    ParameterKey=GitHubRepository,ParameterValue=YOUR_GITHUB_USERNAME/ibkr-borrow-collector \
  --capabilities CAPABILITY_NAMED_IAM

# With custom bucket name and retention settings
aws cloudformation create-stack \
  --stack-name ibkr-borrow-collector \
  --template-body file://cloudformation-template.yaml \
  --parameters \
    ParameterKey=GitHubRepository,ParameterValue=YOUR_GITHUB_USERNAME/ibkr-borrow-collector \
    ParameterKey=BucketName,ParameterValue=my-borrow-data-bucket \
    ParameterKey=DataRetentionDays,ParameterValue=730 \
    ParameterKey=TransitionToGlacierDays,ParameterValue=180 \
  --capabilities CAPABILITY_NAMED_IAM
```

### 2. Wait for Stack Creation

```bash
# Wait for stack to complete (takes ~1-2 minutes)
aws cloudformation wait stack-create-complete \
  --stack-name ibkr-borrow-collector

# Check status
aws cloudformation describe-stacks \
  --stack-name ibkr-borrow-collector \
  --query 'Stacks[0].StackStatus'
```

### 3. Get Outputs

```bash
# Get all outputs
aws cloudformation describe-stacks \
  --stack-name ibkr-borrow-collector \
  --query 'Stacks[0].Outputs' \
  --output table
```

### 4. Configure GitHub Secrets

Add the following secrets to your GitHub repository (Settings → Secrets → Actions):

```bash
# Get the Role ARN from CloudFormation
export ROLE_ARN=$(aws cloudformation describe-stacks \
  --stack-name ibkr-borrow-collector \
  --query 'Stacks[0].Outputs[?OutputKey==`GitHubActionsRoleArn`].OutputValue' \
  --output text)

export AWS_REGION=$(aws cloudformation describe-stacks \
  --stack-name ibkr-borrow-collector \
  --query 'Stacks[0].Outputs[?OutputKey==`Region`].OutputValue' \
  --output text)

export S3_BUCKET=$(aws cloudformation describe-stacks \
  --stack-name ibkr-borrow-collector \
  --query 'Stacks[0].Outputs[?OutputKey==`BucketName`].OutputValue' \
  --output text)

# Add to GitHub (requires gh CLI)
gh secret set AWS_ROLE_ARN --body "$ROLE_ARN"
gh secret set AWS_REGION --body "$AWS_REGION"
gh secret set S3_BUCKET --body "$S3_BUCKET"

# Or manually add these three secrets via GitHub UI:
# AWS_ROLE_ARN: (the role ARN from above)
# AWS_REGION: us-east-1 (or your region)
# S3_BUCKET: (the bucket name from above)
```

✅ **No access keys needed!** GitHub authenticates via OIDC.

### 5. Test Collection

Trigger the GitHub Actions workflow manually:

```bash
# Via GitHub CLI
gh workflow run collect.yml

# Or via web UI: Actions → Collect IBKR Borrow Data → Run workflow
```

### 6. Verify Data Collection

```bash
# Set S3_BUCKET variable from CloudFormation output
export S3_BUCKET=$(aws cloudformation describe-stacks \
  --stack-name ibkr-borrow-collector \
  --query 'Stacks[0].Outputs[?OutputKey==`BucketName`].OutputValue' \
  --output text)

# List collected data
aws s3 ls s3://$S3_BUCKET/ibkr/borrow/ --recursive --human-readable

# Download a sample file (using timestamped filename)
aws s3 ls s3://$S3_BUCKET/ibkr/borrow/$(date +%Y-%m-%d)/ | tail -1 | awk '{print $4}' | xargs -I {} aws s3 cp s3://$S3_BUCKET/ibkr/borrow/$(date +%Y-%m-%d)/{} - 2>/dev/null | gunzip | head -20
```

## CloudFormation Parameters

| Parameter | Description | Default | Notes |
|-----------|-------------|---------|-------|
| `GitHubRepository` | GitHub repo (owner/repo) | hoppefamily/ibkr-borrow-collector | Required for OIDC trust policy |
| `BucketName` | S3 bucket name | (auto-generated) | Must be globally unique |
| `DataRetentionDays` | Days to keep data | 365 | 0 = never expire |
| `TransitionToGlacierDays` | Days before Glacier transition | 90 | 0 = disabled |

### Storage Lifecycle

The CloudFormation template includes intelligent storage lifecycle policies:

1. **First 30 days**: Standard storage (fast access)
2. **30-90 days**: Intelligent-Tiering (automatic cost optimization)
3. **90+ days** (optional): Glacier Instant Retrieval (70% cheaper, millisecond access)
4. **After retention period**: Automatic deletion

**Cost Impact**:
- Standard storage: $0.023/GB/month
- Intelligent-Tiering: $0.0025/GB monitoring + tiered storage
- Glacier Instant Retrieval: $0.004/GB/month (83% cheaper)

For 5.5 GB/year with default settings:
- Month 1: $0.13 (standard)
- Months 2-3: $0.05 (intelligent-tiering)
- Months 4-12: $0.02/month (Glacier IR)
- **Total Year 1**: ~$0.60 (vs $1.52 without lifecycle)

## Alternative: AWS Lambda Deployment

Use the IAM role created by CloudFormation for serverless deployment:

### 1. Build and Push Container to ECR

```bash
# Get Lambda role ARN from CloudFormation
LAMBDA_ROLE=$(aws cloudformation describe-stacks \
  --stack-name ibkr-borrow-collector \
  --query 'Stacks[0].Outputs[?OutputKey==`LambdaRoleArn`].OutputValue' \
  --output text)

# Create ECR repository
aws ecr create-repository --repository-name ibkr-borrow-collector

# Build and push container
aws ecr get-login-password --region us-east-1 | \
  docker login --username AWS --password-stdin $AWS_ACCOUNT.dkr.ecr.us-east-1.amazonaws.com

docker build -t ibkr-borrow-collector .
docker tag ibkr-borrow-collector:latest \
  $AWS_ACCOUNT.dkr.ecr.us-east-1.amazonaws.com/ibkr-borrow-collector:latest
docker push $AWS_ACCOUNT.dkr.ecr.us-east-1.amazonaws.com/ibkr-borrow-collector:latest
```

### 2. Create Lambda Function

```bash
# Get bucket name
S3_BUCKET=$(aws cloudformation describe-stacks \
  --stack-name ibkr-borrow-collector \
  --query 'Stacks[0].Outputs[?OutputKey==`BucketName`].OutputValue' \
  --output text)

# Create Lambda function
aws lambda create-function \
  --function-name ibkr-borrow-collector \
  --package-type Image \
  --code ImageUri=$AWS_ACCOUNT.dkr.ecr.us-east-1.amazonaws.com/ibkr-borrow-collector:latest \
  --role $LAMBDA_ROLE \
  --timeout 300 \
  --memory-size 512 \
  --environment Variables="{S3_BUCKET=$S3_BUCKET}"
```

### 3. Create EventBridge Schedule

```bash
# Create schedule rule (every 15 minutes)
aws events put-rule \
  --name ibkr-collector-schedule \
  --schedule-expression "rate(15 minutes)" \
  --state ENABLED

# Add Lambda permission
aws lambda add-permission \
  --function-name ibkr-borrow-collector \
  --statement-id EventBridgeInvoke \
  --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn arn:aws:events:us-east-1:$AWS_ACCOUNT:rule/ibkr-collector-schedule

# Add Lambda as target
aws events put-targets \
  --rule ibkr-collector-schedule \
  --targets "Id"="1","Arn"="arn:aws:lambda:us-east-1:$AWS_ACCOUNT:function:ibkr-borrow-collector"
```

## Option 4: AWS ECS/Fargate Deployment

Use the IAM roles created by CloudFormation:

### 1. Create Task Definition

```bash
# Get role ARNs
TASK_EXECUTION_ROLE=$(aws cloudformation describe-stacks \
  --stack-name ibkr-borrow-collector \
  --query 'Stacks[0].Outputs[?OutputKey==`ECSTaskExecutionRoleArn`].OutputValue' \
  --output text)

TASK_ROLE=$(aws cloudformation describe-stacks \
  --stack-name ibkr-borrow-collector \
  --query 'Stacks[0].Outputs[?OutputKey==`ECSTaskRoleArn`].OutputValue' \
  --output text)

S3_BUCKET=$(aws cloudformation describe-stacks \
  --stack-name ibkr-borrow-collector \
  --query 'Stacks[0].Outputs[?OutputKey==`BucketName`].OutputValue' \
  --output text)

# Create task definition
cat > task-definition.json << EOF
{
  "family": "ibkr-borrow-collector",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "256",
  "memory": "512",
  "executionRoleArn": "$TASK_EXECUTION_ROLE",
  "taskRoleArn": "$TASK_ROLE",
  "containerDefinitions": [
    {
      "name": "collector",
      "image": "$AWS_ACCOUNT.dkr.ecr.us-east-1.amazonaws.com/ibkr-borrow-collector:latest",
      "essential": true,
      "environment": [
        {"name": "S3_BUCKET", "value": "$S3_BUCKET"}
      ],
      "logConfiguration": {
        "logDriver": "awslogs",
        "options": {
          "awslogs-group": "/ecs/ibkr-borrow-collector",
          "awslogs-region": "us-east-1",
          "awslogs-stream-prefix": "collector"
        }
      }
    }
  ]
}
EOF

# Create log group
aws logs create-log-group --log-group-name /ecs/ibkr-borrow-collector

# Register task definition
aws ecs register-task-definition --cli-input-json file://task-definition.json
```

### 2. Create EventBridge Schedule for ECS

```bash
# Create ECS cluster (if needed)
aws ecs create-cluster --cluster-name ibkr-collector

# Create schedule
aws events put-rule \
  --name ibkr-collector-ecs-schedule \
  --schedule-expression "rate(15 minutes)" \
  --state ENABLED

# Add ECS task as target
aws events put-targets \
  --rule ibkr-collector-ecs-schedule \
  --targets file://ecs-target.json
```

## Testing and Validation

### Test Local Collection

```bash
# Test FTP connection (no AWS needed)
python collector.py --test-connection --dry-run

# Test with AWS (uploads to S3)
python collector.py \
  --s3-bucket $S3_BUCKET \
  --s3-prefix ibkr/borrow
```

### Verify Delta Compression

```bash
# Check if xdelta3 is available in container
docker run --rm ibkr-borrow-collector xdelta3 -V

# Run collector and check output
docker run --rm \
  -e AWS_ACCESS_KEY_ID \
  -e AWS_SECRET_ACCESS_KEY \
  -e AWS_DEFAULT_REGION=us-east-1 \
  ibkr-borrow-collector \
  --s3-bucket $S3_BUCKET \
  --log-json | jq '.statistics'
```

### Monitor Data Collection

```bash
# Check latest collection
aws s3 ls s3://$S3_BUCKET/ibkr/borrow/$(date +%Y-%m-%d)/ --human-readable

# Check storage usage
aws s3 ls s3://$S3_BUCKET/ibkr/borrow/ --recursive --summarize | tail -2

# Download and inspect data
aws s3 cp s3://$S3_BUCKET/ibkr/borrow/$(date +%Y-%m-%d)/usa-latest.txt.gz - | \
  gunzip | head -20
```

## Cost Monitoring

### Set Up Billing Alerts

```bash
# Create SNS topic for billing alerts
aws sns create-topic --name ibkr-collector-billing-alerts

# Subscribe email
aws sns subscribe \
  --topic-arn arn:aws:sns:us-east-1:$AWS_ACCOUNT:ibkr-collector-billing-alerts \
  --protocol email \
  --notification-endpoint your-email@example.com

# Create billing alarm
aws cloudwatch put-metric-alarm \
  --alarm-name ibkr-collector-monthly-cost \
  --alarm-description "Alert if monthly cost exceeds $5" \
  --metric-name EstimatedCharges \
  --namespace AWS/Billing \
  --statistic Maximum \
  --period 21600 \
  --evaluation-periods 1 \
  --threshold 5.0 \
  --comparison-operator GreaterThanThreshold \
  --alarm-actions arn:aws:sns:us-east-1:$AWS_ACCOUNT:ibkr-collector-billing-alerts
```

### Check Current Costs

```bash
# Get S3 storage metrics
aws cloudwatch get-metric-statistics \
  --namespace AWS/S3 \
  --metric-name BucketSizeBytes \
  --dimensions Name=BucketName,Value=$S3_BUCKET Name=StorageType,Value=StandardStorage \
  --start-time $(date -u -d '7 days ago' +%Y-%m-%dT%H:%M:%S) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%S) \
  --period 86400 \
  --statistics Average \
  --output table
```

## Troubleshooting

### GitHub Actions Not Running

```bash
# Check workflow status
gh run list --workflow=collect.yml --limit 5

# View logs
gh run view <run-id> --log
```

### Lambda Timeout Issues

```bash
# Increase timeout
aws lambda update-function-configuration \
  --function-name ibkr-borrow-collector \
  --timeout 600

# Check CloudWatch logs
aws logs tail /aws/lambda/ibkr-borrow-collector --follow
```

### S3 Access Denied

```bash
# Verify IAM permissions
aws iam simulate-principal-policy \
  --policy-source-arn arn:aws:iam::$AWS_ACCOUNT:user/ibkr-collector-github \
  --action-names s3:PutObject s3:GetObject s3:ListBucket \
  --resource-arns arn:aws:s3:::$S3_BUCKET/ibkr/borrow/test.txt

# Check bucket policy
aws s3api get-bucket-policy --bucket $S3_BUCKET
```

## Cleanup

### Delete CloudFormation Stack

```bash
# This will delete the bucket, IAM user, roles, and all associated resources
# WARNING: This is permanent!
aws cloudformation delete-stack --stack-name ibkr-borrow-collector

# Wait for deletion
aws cloudformation wait stack-delete-complete --stack-name ibkr-borrow-collector
```

### Manual Cleanup

If not using CloudFormation:

```bash
# Delete S3 bucket (must be empty first)
aws s3 rm s3://$S3_BUCKET --recursive
aws s3api delete-bucket --bucket $S3_BUCKET

# Delete IAM user
aws iam delete-access-key --user-name ibkr-collector-github --access-key-id <KEY_ID>
aws iam delete-user-policy --user-name ibkr-collector-github --policy-name S3BorrowDataAccess
aws iam delete-user --user-name ibkr-collector-github

# Delete Lambda function (if created)
aws lambda delete-function --function-name ibkr-borrow-collector

# Delete ECR repository (if created)
aws ecr delete-repository --repository-name ibkr-borrow-collector --force
```

## Security Best Practices

1. **Rotate Access Keys**: Rotate IAM access keys every 90 days
2. **Enable MFA**: Enable MFA for AWS account root user
3. **Use Secrets Manager**: Store credentials in AWS Secrets Manager instead of GitHub Secrets
4. **Enable CloudTrail**: Monitor API calls for security auditing
5. **Review IAM Policies**: Regularly review and minimize IAM permissions
6. **Enable S3 Access Logging**: Track all S3 access for security monitoring

## Support

For issues or questions:
- GitHub Issues: https://github.com/hoppefamily/ibkr-borrow-collector/issues
- Documentation: See [README.md](README.md) and [EXAMPLES.md](EXAMPLES.md)
