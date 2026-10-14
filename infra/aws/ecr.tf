resource "aws_ecr_repository" "batch" {
  name                 = "${local.name}-batch"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration {
    scan_on_push = true
  }
  tags = local.common_tags
}

output "batch_repository_url" {
  value = aws_ecr_repository.batch.repository_url
}
