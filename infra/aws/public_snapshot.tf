# Public, read-only statistics snapshot (weekly-growth.svg, summary.json) for the
# README and portfolio. A separate bucket from the private exports bucket, so the
# backups there stay private. Only the snapshot/ prefix is publicly readable.

resource "aws_s3_bucket" "public" {
  bucket_prefix = "${local.name}-public-"

  tags = merge(local.common_tags, {
    Name = "${local.name}-public"
  })
}

resource "aws_s3_bucket_public_access_block" "public" {
  bucket = aws_s3_bucket.public.id

  block_public_acls       = true
  ignore_public_acls      = true
  block_public_policy     = false
  restrict_public_buckets = false
}

resource "aws_s3_bucket_policy" "public" {
  bucket = aws_s3_bucket.public.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "PublicReadSnapshot"
      Effect    = "Allow"
      Principal = "*"
      Action    = "s3:GetObject"
      Resource  = "${aws_s3_bucket.public.arn}/snapshot/*"
    }]
  })

  depends_on = [aws_s3_bucket_public_access_block.public]
}

resource "aws_s3_bucket_cors_configuration" "public" {
  bucket = aws_s3_bucket.public.id

  cors_rule {
    allowed_methods = ["GET"]
    allowed_origins = ["*"]
    max_age_seconds = 3600
  }
}

resource "aws_iam_role_policy" "task_public_snapshot" {
  name = "${local.name}-public-snapshot"
  role = aws_iam_role.task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "s3:PutObject"
      Resource = "${aws_s3_bucket.public.arn}/snapshot/*"
    }]
  })
}
