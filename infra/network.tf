# Network.
#
# The shape here is driven by one decision: MoneyPrinterTurbo must not be
# reachable from the internet. Its only authentication is a single shared
# secret, and while that is compared in constant time, its /docs, /openapi.json
# and /ping routes are unauthenticated by design. Postiz is the same story with
# a larger surface.
#
# Keeping them private means the Lambdas must be in the VPC to reach them,
# which in turn means the Lambdas need a NAT gateway for their own egress to
# Supabase, Postiz's platform APIs and the Anthropic API. That is one NAT
# gateway -- roughly $32/month -- against $300-600/month of render spend at ten
# reels a day. Cheap insurance, and the alternative is a public render service
# whose docs endpoint anyone can read.

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  azs         = slice(data.aws_availability_zones.available.names, 0, 2)
  vpc_cidr    = "10.42.0.0/16"
  public_cidr = [for i, _ in local.azs : cidrsubnet(local.vpc_cidr, 8, i)]
  privat_cidr = [for i, _ in local.azs : cidrsubnet(local.vpc_cidr, 8, i + 10)]
}

resource "aws_vpc" "main" {
  cidr_block           = local.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = "${var.name}-vpc" }
}

resource "aws_internet_gateway" "igw" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${var.name}-igw" }
}

resource "aws_subnet" "public" {
  count                   = length(local.azs)
  vpc_id                  = aws_vpc.main.id
  cidr_block              = local.public_cidr[count.index]
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = true
  tags                    = { Name = "${var.name}-public-${local.azs[count.index]}" }
}

resource "aws_subnet" "private" {
  count             = length(local.azs)
  vpc_id            = aws_vpc.main.id
  cidr_block        = local.privat_cidr[count.index]
  availability_zone = local.azs[count.index]
  tags              = { Name = "${var.name}-private-${local.azs[count.index]}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.igw.id
  }
  tags = { Name = "${var.name}-public" }
}

resource "aws_route_table_association" "public" {
  count          = length(aws_subnet.public)
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# One NAT gateway, not one per AZ. At ten videos a day the saving is worth more
# than the extra availability: a NAT outage delays a render, it does not lose
# one, because every job is a durable row that the reconcilers pick back up.
resource "aws_eip" "nat" {
  domain = "vpc"
  tags   = { Name = "${var.name}-nat" }
}

resource "aws_nat_gateway" "nat" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public[0].id
  depends_on    = [aws_internet_gateway.igw]
  tags          = { Name = "${var.name}-nat" }
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.nat.id
  }
  tags = { Name = "${var.name}-private" }
}

resource "aws_route_table_association" "private" {
  count          = length(aws_subnet.private)
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# S3 as a gateway endpoint so ECR image layer pulls -- which are S3 GETs, and
# by far the largest traffic these services generate -- skip the NAT and its
# per-GB charge entirely. The Playwright image alone is well over a gigabyte.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${data.aws_region.here.name}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]
}

# --- Security groups ---------------------------------------------------------

resource "aws_security_group" "lambda" {
  name        = "${var.name}-lambda"
  description = "Pipeline Lambdas. Egress only."
  vpc_id      = aws_vpc.main.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "tasks" {
  name        = "${var.name}-tasks"
  description = "ECS tasks: MPT, Postiz and the trend scout."
  vpc_id      = aws_vpc.main.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "alb" {
  name        = "${var.name}-alb"
  description = "Internal ALB. Reachable only from inside the VPC."
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port       = 80
    to_port         = 80
    protocol        = "tcp"
    security_groups = [aws_security_group.lambda.id, aws_security_group.tasks.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# The ALB reaches the services; nothing else can.
resource "aws_security_group_rule" "tasks_from_alb" {
  type                     = "ingress"
  from_port                = 0
  to_port                  = 65535
  protocol                 = "tcp"
  security_group_id        = aws_security_group.tasks.id
  source_security_group_id = aws_security_group.alb.id
}

resource "aws_security_group" "data" {
  name        = "${var.name}-data"
  description = "Redis and EFS. Reachable only from the tasks."
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.tasks.id]
  }

  ingress {
    from_port       = 2049 # NFS, for the EFS mount
    to_port         = 2049
    protocol        = "tcp"
    security_groups = [aws_security_group.tasks.id]
  }
}
