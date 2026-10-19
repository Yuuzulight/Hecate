locals {
  name = "${var.project}-${var.environment}"

  common_tags = {
    Project     = var.project
    Environment = var.environment
    ManagedBy   = "terraform"
  }

  db_env = [
    { name = "DB_HOST", value = data.aws_rds_cluster.postgres.endpoint },
    { name = "DB_AUTH_MODE", value = "iam" },
    { name = "AWS_DEFAULT_REGION", value = var.aws_region },
    { name = "DB_PORT", value = "5432" },
    { name = "DB_NAME", value = var.db_name },
    { name = "DB_USER", value = var.db_username },
    { name = "HECATE_EXPORT_BUCKET", value = aws_s3_bucket.exports.bucket },
    { name = "PGSSLMODE", value = "verify-full" },
    { name = "PGSSLROOTCERT", value = "/etc/ssl/certs/ca-certificates.crt" },
  ]

  db_secret = []

  api_secret_arn = aws_secretsmanager_secret.api_credentials.arn
}

data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_vpc" "main" {
  cidr_block           = "10.42.0.0/16"
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = merge(local.common_tags, {
    Name = "${local.name}-vpc"
  })
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = merge(local.common_tags, {
    Name = "${local.name}-igw"
  })
}

resource "aws_subnet" "public" {
  count = 2

  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(aws_vpc.main.cidr_block, 8, count.index)
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = true

  tags = merge(local.common_tags, {
    Name = "${local.name}-public-${count.index + 1}"
  })
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = merge(local.common_tags, {
    Name = "${local.name}-public"
  })
}

resource "aws_route_table_association" "public" {
  count = length(aws_subnet.public)

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_security_group" "ecs" {
  name        = "${local.name}-ecs"
  description = "Hecate ECS tasks"
  vpc_id      = aws_vpc.main.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.common_tags, {
    Name = "${local.name}-ecs"
  })
}

resource "aws_security_group" "db" {
  name        = "${local.name}-db"
  description = "Hecate Aurora PostgreSQL"
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.ecs.id]
  }

  tags = merge(local.common_tags, {
    Name = "${local.name}-db"
  })
}

resource "aws_security_group" "alb" {
  name        = "${local.name}-alb"
  description = "Hecate RAG load balancer"
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = var.rag_allowed_cidr_blocks
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.common_tags, {
    Name = "${local.name}-alb"
  })
}

resource "aws_security_group_rule" "rag_from_alb" {
  type                     = "ingress"
  from_port                = 8001
  to_port                  = 8001
  protocol                 = "tcp"
  security_group_id        = aws_security_group.ecs.id
  source_security_group_id = aws_security_group.alb.id
}

resource "aws_db_subnet_group" "main" {
  name       = local.name
  subnet_ids = aws_subnet.public[*].id

  tags = merge(local.common_tags, {
    Name = "${local.name}-db"
  })
}

data "aws_rds_cluster" "postgres" {
  cluster_identifier = local.name
}

resource "aws_s3_bucket" "exports" {
  bucket_prefix = "${local.name}-exports-"

  tags = merge(local.common_tags, {
    Name = "${local.name}-exports"
  })
}

resource "aws_s3_bucket_public_access_block" "exports" {
  bucket = aws_s3_bucket.exports.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "exports" {
  bucket = aws_s3_bucket.exports.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "exports" {
  bucket = aws_s3_bucket.exports.id

  rule {
    id     = "archive-old-exports"
    status = "Enabled"

    filter {
      prefix = ""
    }

    transition {
      days          = var.export_retention_days
      storage_class = "STANDARD_IA"
    }
  }
}

resource "aws_secretsmanager_secret" "api_credentials" {
  name = "${local.name}-api-credentials"

  tags = local.common_tags
}

resource "aws_secretsmanager_secret_version" "api_credentials" {
  secret_id = aws_secretsmanager_secret.api_credentials.id
  secret_string = jsonencode({
    github_token      = var.github_token
    gitlab_token      = var.gitlab_token
    google_api_key    = var.google_api_key
    anthropic_api_key = var.anthropic_api_key
    openai_api_key    = var.openai_api_key
  })
  lifecycle {
    ignore_changes = [secret_string]
  }
}

resource "aws_ecs_cluster" "main" {
  name = local.name

  setting {
    name  = "containerInsights"
    value = "disabled"
  }

  tags = local.common_tags
}

resource "aws_cloudwatch_log_group" "tasks" {
  name              = "/ecs/${local.name}"
  retention_in_days = 30

  tags = local.common_tags
}

resource "aws_iam_role" "task_execution" {
  name = "${local.name}-task-execution"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Service = "ecs-tasks.amazonaws.com"
      }
      Action = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
        ArnLike      = { "aws:SourceArn" = "arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*" }
      }
    }]
  })

  tags = local.common_tags
}

resource "aws_iam_role_policy_attachment" "task_execution" {
  role       = aws_iam_role.task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "task_execution_secrets" {
  name = "${local.name}-task-execution-secrets"
  role = aws_iam_role.task_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue"
        ]
        Resource = [
          aws_secretsmanager_secret.api_credentials.arn
        ]
      }
    ]
  })
}

resource "aws_iam_role" "task" {
  name = "${local.name}-task"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Service = "ecs-tasks.amazonaws.com"
      }
      Action = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
        ArnLike      = { "aws:SourceArn" = "arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*" }
      }
    }]
  })

  tags = local.common_tags
}

resource "aws_iam_role_policy" "task_exports" {
  name = "${local.name}-exports"
  role = aws_iam_role.task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:ListBucket"
        ]
        Resource = aws_s3_bucket.exports.arn
      },
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:AbortMultipartUpload"
        ]
        Resource = "${aws_s3_bucket.exports.arn}/*"
      }
    ]
  })
}

resource "aws_iam_role" "scheduler" {
  name = "${local.name}-scheduler"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Service = "scheduler.amazonaws.com"
      }
      Action = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
        ArnEquals    = { "aws:SourceArn" = "arn:aws:scheduler:${var.aws_region}:${data.aws_caller_identity.current.account_id}:schedule-group/default" }
      }
    }]
  })

  tags = local.common_tags
}

resource "aws_iam_role_policy" "scheduler" {
  name = "${local.name}-scheduler"
  role = aws_iam_role.scheduler.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ecs:RunTask"
        ]
        Resource = [
          aws_ecs_task_definition.daily.arn,
          aws_ecs_task_definition.dbt.arn,
          aws_ecs_task_definition.dbt_full.arn,
          aws_ecs_task_definition.embed.arn,
          aws_ecs_task_definition.forecast.arn
        ]
        Condition = { ArnEquals = { "ecs:cluster" = aws_ecs_cluster.main.arn } }
      },
      {
        Effect = "Allow"
        Action = [
          "iam:PassRole"
        ]
        Resource = [
          aws_iam_role.task_execution.arn,
          aws_iam_role.task.arn
        ]
        Condition = { StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" } }
      }
    ]
  })
}

module "daily_task" {
  source = "./modules/task_definition"

  family             = "${local.name}-daily"
  image              = var.pipeline_image
  cpu                = 1024
  memory             = 2048
  execution_role_arn = aws_iam_role.task_execution.arn
  task_role_arn      = aws_iam_role.task.arn
  log_group_name     = aws_cloudwatch_log_group.tasks.name
  aws_region         = var.aws_region

  environment = concat(local.db_env, [
    { name = "BATCH_SIZE", value = tostring(var.batch_size) },
    { name = "API_SECRET_ARN", value = local.api_secret_arn },
    { name = "VALKEY_HOST", value = local.valkey_host },
    { name = "VALKEY_NAME", value = local.valkey_name },
    { name = "ENABLE_EMBEDDINGS", value = "1" },
    { name = "ENABLE_FORECAST", value = "1" },
    { name = "HECATE_PUBLIC_BUCKET", value = aws_s3_bucket.public.bucket },
    { name = "ECS_CLUSTER", value = aws_ecs_cluster.main.arn },
    { name = "FORECAST_TASK", value = aws_ecs_task_definition.forecast.arn },
    { name = "FORECAST_NETWORK", value = jsonencode({ awsvpcConfiguration = { subnets = aws_subnet.public[*].id, securityGroups = [aws_security_group.ecs.id], assignPublicIp = "ENABLED" } }) }
  ])

  secrets = concat(local.db_secret, [
    { name = "GITHUB_TOKEN", valueFrom = "${local.api_secret_arn}:github_token::" },
    { name = "GITLAB_TOKEN", valueFrom = "${local.api_secret_arn}:gitlab_token::" }
  ])
}

resource "aws_ecs_task_definition" "daily" {
  family                   = module.daily_task.family
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = module.daily_task.cpu
  memory                   = module.daily_task.memory
  execution_role_arn       = module.daily_task.execution_role_arn
  task_role_arn            = module.daily_task.task_role_arn
  container_definitions    = module.daily_task.container_definitions

  tags = local.common_tags
}

module "dbt_task" {
  source = "./modules/task_definition"

  family             = "${local.name}-dbt"
  image              = var.dbt_image
  cpu                = 1024
  memory             = 2048
  execution_role_arn = aws_iam_role.task_execution.arn
  task_role_arn      = aws_iam_role.task.arn
  log_group_name     = aws_cloudwatch_log_group.tasks.name
  aws_region         = var.aws_region
  environment        = local.db_env
  secrets            = local.db_secret
}

resource "aws_ecs_task_definition" "dbt" {
  family                   = module.dbt_task.family
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = module.dbt_task.cpu
  memory                   = module.dbt_task.memory
  execution_role_arn       = module.dbt_task.execution_role_arn
  task_role_arn            = module.dbt_task.task_role_arn
  container_definitions    = module.dbt_task.container_definitions

  tags = local.common_tags
}

module "dbt_full_task" {
  source = "./modules/task_definition"

  family             = "${local.name}-dbt-full"
  image              = var.dbt_image
  command            = ["dbt", "build", "--full-refresh", "--project-dir", "/app/dbt", "--profiles-dir", "/app/dbt"]
  cpu                = 1024
  memory             = 2048
  execution_role_arn = aws_iam_role.task_execution.arn
  task_role_arn      = aws_iam_role.task.arn
  log_group_name     = aws_cloudwatch_log_group.tasks.name
  aws_region         = var.aws_region
  environment        = local.db_env
  secrets            = local.db_secret
}

resource "aws_ecs_task_definition" "dbt_full" {
  family                   = module.dbt_full_task.family
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = module.dbt_full_task.cpu
  memory                   = module.dbt_full_task.memory
  execution_role_arn       = module.dbt_full_task.execution_role_arn
  task_role_arn            = module.dbt_full_task.task_role_arn
  container_definitions    = module.dbt_full_task.container_definitions

  tags = local.common_tags
}

module "embed_task" {
  source = "./modules/task_definition"

  family             = "${local.name}-embed"
  image              = var.rag_image
  command            = ["python", "-m", "pipeline.rag.embeddings"]
  cpu                = 512
  memory             = 1024
  execution_role_arn = aws_iam_role.task_execution.arn
  task_role_arn      = aws_iam_role.task.arn
  log_group_name     = aws_cloudwatch_log_group.tasks.name
  aws_region         = var.aws_region
  environment        = local.db_env
  secrets = concat(local.db_secret, [
    { name = "OPENAI_API_KEY", valueFrom = "${local.api_secret_arn}:openai_api_key::" }
  ])
}

resource "aws_ecs_task_definition" "embed" {
  family                   = module.embed_task.family
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = module.embed_task.cpu
  memory                   = module.embed_task.memory
  execution_role_arn       = module.embed_task.execution_role_arn
  task_role_arn            = module.embed_task.task_role_arn
  container_definitions    = module.embed_task.container_definitions

  tags = local.common_tags
}

module "forecast_task" {
  source = "./modules/task_definition"

  family             = "${local.name}-forecast"
  image              = var.forecast_image
  cpu                = 2048
  memory             = 4096
  execution_role_arn = aws_iam_role.task_execution.arn
  task_role_arn      = aws_iam_role.task.arn
  log_group_name     = aws_cloudwatch_log_group.tasks.name
  aws_region         = var.aws_region
  environment        = local.db_env
  secrets            = local.db_secret
}

resource "aws_ecs_task_definition" "forecast" {
  family                   = module.forecast_task.family
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = module.forecast_task.cpu
  memory                   = module.forecast_task.memory
  execution_role_arn       = module.forecast_task.execution_role_arn
  task_role_arn            = module.forecast_task.task_role_arn
  container_definitions    = module.forecast_task.container_definitions

  tags = local.common_tags
}

module "rag_task" {
  source = "./modules/task_definition"

  family             = "${local.name}-rag"
  image              = var.rag_image
  cpu                = 1024
  memory             = 2048
  execution_role_arn = aws_iam_role.task_execution.arn
  task_role_arn      = aws_iam_role.task.arn
  log_group_name     = aws_cloudwatch_log_group.tasks.name
  aws_region         = var.aws_region
  port_mappings      = [{ containerPort = 8001, protocol = "tcp" }]

  environment = concat(local.db_env, [
    { name = "RAG_ENABLED", value = "1" },
    { name = "RAG_PROVIDER", value = lower(var.rag_provider) }
  ])

  secrets = concat(local.db_secret, [
    { name = "GOOGLE_API_KEY", valueFrom = "${local.api_secret_arn}:google_api_key::" },
    { name = "ANTHROPIC_API_KEY", valueFrom = "${local.api_secret_arn}:anthropic_api_key::" },
    { name = "OPENAI_API_KEY", valueFrom = "${local.api_secret_arn}:openai_api_key::" }
  ])
}

resource "aws_ecs_task_definition" "rag" {
  family                   = module.rag_task.family
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = module.rag_task.cpu
  memory                   = module.rag_task.memory
  execution_role_arn       = module.rag_task.execution_role_arn
  task_role_arn            = module.rag_task.task_role_arn
  container_definitions    = module.rag_task.container_definitions

  tags = local.common_tags
}

module "daily_schedule" {
  source = "./modules/scheduled_task"

  name                = "${local.name}-daily"
  enabled             = var.enable_schedules
  schedule_expression = "cron(0 4 * * ? *)"
  schedule_timezone   = var.schedule_timezone
  cluster_arn         = aws_ecs_cluster.main.arn
  task_definition_arn = aws_ecs_task_definition.daily.arn
  scheduler_role_arn  = aws_iam_role.scheduler.arn
  subnet_ids          = aws_subnet.public[*].id
  security_group_ids  = [aws_security_group.ecs.id]
}

module "dbt_schedule" {
  source = "./modules/scheduled_task"

  name                = "${local.name}-dbt"
  enabled             = false
  schedule_expression = "cron(0 5 * * ? *)"
  schedule_timezone   = var.schedule_timezone
  cluster_arn         = aws_ecs_cluster.main.arn
  task_definition_arn = aws_ecs_task_definition.dbt.arn
  scheduler_role_arn  = aws_iam_role.scheduler.arn
  subnet_ids          = aws_subnet.public[*].id
  security_group_ids  = [aws_security_group.ecs.id]
}

module "embed_schedule" {
  source = "./modules/scheduled_task"

  name                = "${local.name}-embed"
  enabled             = var.enable_schedules && var.enable_embed_schedule
  schedule_expression = "cron(30 5 * * ? *)"
  schedule_timezone   = var.schedule_timezone
  cluster_arn         = aws_ecs_cluster.main.arn
  task_definition_arn = aws_ecs_task_definition.embed.arn
  scheduler_role_arn  = aws_iam_role.scheduler.arn
  subnet_ids          = aws_subnet.public[*].id
  security_group_ids  = [aws_security_group.ecs.id]
}

module "forecast_schedule" {
  source = "./modules/scheduled_task"

  name                = "${local.name}-forecast"
  enabled             = var.enable_schedules && var.enable_forecast_schedule
  schedule_expression = "cron(45 5 * * ? *)"
  schedule_timezone   = var.schedule_timezone
  cluster_arn         = aws_ecs_cluster.main.arn
  task_definition_arn = aws_ecs_task_definition.forecast.arn
  scheduler_role_arn  = aws_iam_role.scheduler.arn
  subnet_ids          = aws_subnet.public[*].id
  security_group_ids  = [aws_security_group.ecs.id]
}

module "dbt_full_schedule" {
  source = "./modules/scheduled_task"

  name                = "${local.name}-dbt-full"
  enabled             = false
  schedule_expression = "cron(0 7 ? * SUN *)"
  schedule_timezone   = var.schedule_timezone
  cluster_arn         = aws_ecs_cluster.main.arn
  task_definition_arn = aws_ecs_task_definition.dbt_full.arn
  scheduler_role_arn  = aws_iam_role.scheduler.arn
  subnet_ids          = aws_subnet.public[*].id
  security_group_ids  = [aws_security_group.ecs.id]
}

resource "aws_lb" "rag" {
  count = var.enable_rag_api ? 1 : 0

  name               = "${local.name}-rag"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id

  tags = local.common_tags
}

resource "aws_lb_target_group" "rag" {
  count = var.enable_rag_api ? 1 : 0

  name        = "${local.name}-rag"
  port        = 8001
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = aws_vpc.main.id

  health_check {
    enabled             = true
    path                = "/health"
    matcher             = "200"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  tags = local.common_tags
}

resource "aws_lb_listener" "rag" {
  count = var.enable_rag_api ? 1 : 0

  load_balancer_arn = aws_lb.rag[0].arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.rag[0].arn
  }
}

resource "aws_ecs_service" "rag" {
  count = var.enable_rag_api ? 1 : 0

  name            = "${local.name}-rag"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.rag.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.ecs.id]
    assign_public_ip = true
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.rag[0].arn
    container_name   = module.rag_task.container_name
    container_port   = 8001
  }

  depends_on = [aws_lb_listener.rag]

  tags = local.common_tags
}
