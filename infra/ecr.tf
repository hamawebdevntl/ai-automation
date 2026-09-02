locals {
  images = ["activities", "media", "trends"]
}

resource "aws_ecr_repository" "img" {
  for_each             = toset(local.images)
  name                 = "${var.name}-${each.key}"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration { scan_on_push = true }
}

# Renders are large and images accumulate; keep the registry from growing
# without bound.
resource "aws_ecr_lifecycle_policy" "img" {
  for_each   = aws_ecr_repository.img
  repository = each.value.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last 10 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 10 }
      action       = { type = "expire" }
    }]
  })
}
