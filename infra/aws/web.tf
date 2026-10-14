locals {
  valkey_name = "${local.name}-vectors"
  valkey_arn  = "arn:aws:elasticache:${var.aws_region}:${data.aws_caller_identity.current.account_id}:serverlesscache:${local.valkey_name}"
  valkey_host = "hecate-prod-vectors-spcigs.public.serverless.apse2.cache.amazonaws.com"
}

resource "aws_iam_role_policy" "task_cloud_runtime" {
  name = "${local.name}-cloud-runtime"
  role = aws_iam_role.task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = "secretsmanager:GetSecretValue", Resource = local.api_secret_arn },
      { Effect = "Allow", Action = "elasticache:Connect", Resource = [local.valkey_arn, "arn:aws:elasticache:${var.aws_region}:${data.aws_caller_identity.current.account_id}:user:default.iam-user"] },
      { Effect = "Allow", Action = "ecs:RunTask", Resource = aws_ecs_task_definition.forecast.arn, Condition = { ArnEquals = { "ecs:cluster" = aws_ecs_cluster.main.arn } } },
      { Effect = "Allow", Action = ["ecs:DescribeTasks", "ecs:StopTask"], Resource = "arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:task/${local.name}/*" },
      { Effect = "Allow", Action = "iam:PassRole", Resource = [aws_iam_role.task.arn, aws_iam_role.task_execution.arn], Condition = { StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" } } }
    ]
  })
}

resource "aws_cognito_user_pool" "web" {
  name = "${local.name}-users"
  admin_create_user_config {
    allow_admin_create_user_only = true
  }
  password_policy {
    minimum_length    = 16
    require_lowercase = true
    require_uppercase = true
    require_numbers   = true
    require_symbols   = true
  }
  mfa_configuration = "OPTIONAL"
  software_token_mfa_configuration { enabled = true }
  tags = local.common_tags
}

resource "aws_cognito_user_pool_domain" "web" {
  domain       = "hecate-${data.aws_caller_identity.current.account_id}"
  user_pool_id = aws_cognito_user_pool.web.id
}

resource "aws_cognito_user_pool_client" "web" {
  name                                 = "${local.name}-browser"
  user_pool_id                         = aws_cognito_user_pool.web.id
  generate_secret                      = false
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "profile"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = ["${aws_apigatewayv2_api.web.api_endpoint}/"]
  logout_urls                          = ["${aws_apigatewayv2_api.web.api_endpoint}/"]
  explicit_auth_flows                  = ["ALLOW_USER_SRP_AUTH", "ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
  access_token_validity                = 15
  id_token_validity                    = 15
  refresh_token_validity               = 1
  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }
  enable_token_revocation       = true
  prevent_user_existence_errors = "ENABLED"
}

resource "aws_iam_role" "web" {
  name               = "${local.name}-web"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }] })
  tags               = local.common_tags
}

resource "aws_cloudwatch_log_group" "web" {
  name              = "/aws/lambda/${local.name}-web"
  retention_in_days = 30
  tags              = local.common_tags
}

resource "aws_iam_role_policy" "web" {
  name = "${local.name}-web"
  role = aws_iam_role.web.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.web.arn}:*" },
    { Effect = "Allow", Action = "secretsmanager:GetSecretValue", Resource = local.api_secret_arn },
    { Effect = "Allow", Action = "rds-db:connect", Resource = "arn:aws:rds-db:${var.aws_region}:${data.aws_caller_identity.current.account_id}:dbuser:${data.aws_rds_cluster.postgres.cluster_resource_id}/${var.db_username}" },
    { Effect = "Allow", Action = "elasticache:Connect", Resource = [local.valkey_arn, "arn:aws:elasticache:${var.aws_region}:${data.aws_caller_identity.current.account_id}:user:default.iam-user"] }
  ] })
}

resource "aws_lambda_function" "web" {
  function_name = "${local.name}-web"
  role          = aws_iam_role.web.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.batch.repository_url}:rag-20261014-auth"
  timeout       = 30
  memory_size   = 1024
  environment {
    variables = {
      DB_HOST              = data.aws_rds_cluster.postgres.endpoint
      DB_USER              = var.db_username
      DB_NAME              = var.db_name
      DB_AUTH_MODE         = "iam"
      PGSSLMODE            = "verify-full"
      PGSSLROOTCERT        = "/etc/ssl/certs/ca-certificates.crt"
      API_SECRET_ARN       = local.api_secret_arn
      VALKEY_HOST          = local.valkey_host
      VALKEY_NAME          = local.valkey_name
      RAG_PROVIDER         = "openai"
      RAG_MODEL            = "gpt-5-mini"
      RAG_MAX_TOKENS       = "8192"
      RAG_REASONING_EFFORT = "minimal"
      DAILY_ASK_LIMIT      = "50"
      COGNITO_CLIENT_ID    = aws_cognito_user_pool_client.web.id
      COGNITO_DOMAIN       = "https://${aws_cognito_user_pool_domain.web.domain}.auth.${var.aws_region}.amazoncognito.com"
    }
  }
  depends_on = [aws_iam_role_policy.web, aws_cloudwatch_log_group.web]
  tags       = local.common_tags
}

resource "aws_apigatewayv2_api" "web" {
  name          = "${local.name}-web"
  protocol_type = "HTTP"
  tags          = local.common_tags
}

resource "aws_apigatewayv2_integration" "web" {
  api_id                 = aws_apigatewayv2_api.web.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.web.invoke_arn
  payload_format_version = "2.0"
  timeout_milliseconds   = 30000
}

resource "aws_apigatewayv2_authorizer" "web" {
  api_id           = aws_apigatewayv2_api.web.id
  name             = "cognito"
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]
  jwt_configuration {
    audience = [aws_cognito_user_pool_client.web.id]
    issuer   = "https://cognito-idp.${var.aws_region}.amazonaws.com/${aws_cognito_user_pool.web.id}"
  }
}

resource "aws_apigatewayv2_route" "public" {
  for_each  = toset(["GET /", "GET /auth-config", "GET /health"])
  api_id    = aws_apigatewayv2_api.web.id
  route_key = each.value
  target    = "integrations/${aws_apigatewayv2_integration.web.id}"
}

resource "aws_apigatewayv2_route" "protected" {
  for_each             = toset(["POST /ask", "GET /trending", "GET /forecasts", "GET /evaluations"])
  api_id               = aws_apigatewayv2_api.web.id
  route_key            = each.value
  target               = "integrations/${aws_apigatewayv2_integration.web.id}"
  authorization_type   = "JWT"
  authorizer_id        = aws_apigatewayv2_authorizer.web.id
  authorization_scopes = ["openid", "aws.cognito.signin.user.admin"]
}

resource "aws_apigatewayv2_stage" "web" {
  api_id      = aws_apigatewayv2_api.web.id
  name        = "$default"
  auto_deploy = true
  default_route_settings {
    throttling_burst_limit = 5
    throttling_rate_limit  = 2
  }
  tags = local.common_tags
}

resource "aws_lambda_permission" "web" {
  statement_id  = "ApiGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.web.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.web.execution_arn}/*/*"
}

output "web_url" { value = aws_apigatewayv2_api.web.api_endpoint }
output "web_user_pool_id" { value = aws_cognito_user_pool.web.id }
output "web_client_id" { value = aws_cognito_user_pool_client.web.id }
