data "aws_caller_identity" "current" {}

resource "aws_iam_role_policy" "database_connect" {
  name = "${local.name}-database-connect"
  role = aws_iam_role.task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "rds-db:connect"
      Resource = "arn:aws:rds-db:${var.aws_region}:${data.aws_caller_identity.current.account_id}:dbuser:${data.aws_rds_cluster.postgres.cluster_resource_id}/${var.db_username}"
    }]
  })
}
