# Three subnet tiers per AZ:
#   public   NAT gateways only (nothing else is internet-facing: CloudFront reaches the ALB through
#            a VPC origin)
#   private  internal ALB, CloudFront VPC origin ENIs, API tasks, RDS
#   workers  worker hosts, kept apart so the API trusts proxy headers from the private tier only
#            (a worker or a job container cannot pass for a client by forging X-Forwarded-For)
locals {
  az_count      = length(var.availability_zones)
  public_cidrs  = [for i, _ in var.availability_zones : cidrsubnet(var.vpc_cidr, 8, i)]
  private_cidrs = [for i, _ in var.availability_zones : cidrsubnet(var.vpc_cidr, 8, i + 10)]
  worker_cidrs  = [for i, _ in var.availability_zones : cidrsubnet(var.vpc_cidr, 8, i + 20)]
}

data "aws_region" "current" {}

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = { Name = var.name }
}

# No rule: the default security group of the VPC denies everything.
resource "aws_default_security_group" "this" {
  vpc_id = aws_vpc.this.id
}

resource "aws_subnet" "public" {
  count = local.az_count

  vpc_id                  = aws_vpc.this.id
  cidr_block              = local.public_cidrs[count.index]
  availability_zone       = var.availability_zones[count.index]
  map_public_ip_on_launch = false

  tags = { Name = "${var.name}-public-${count.index + 1}" }
}

resource "aws_subnet" "private" {
  count = local.az_count

  vpc_id            = aws_vpc.this.id
  cidr_block        = local.private_cidrs[count.index]
  availability_zone = var.availability_zones[count.index]

  tags = { Name = "${var.name}-private-${count.index + 1}" }
}

resource "aws_subnet" "worker" {
  count = local.az_count

  vpc_id            = aws_vpc.this.id
  cidr_block        = local.worker_cidrs[count.index]
  availability_zone = var.availability_zones[count.index]

  tags = { Name = "${var.name}-workers-${count.index + 1}" }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = var.name }
}

resource "aws_eip" "nat" {
  count  = local.az_count
  domain = "vpc"
  tags   = { Name = "${var.name}-nat-${count.index + 1}" }
}

resource "aws_nat_gateway" "this" {
  count = local.az_count

  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index].id

  tags = { Name = "${var.name}-${count.index + 1}" }

  depends_on = [aws_internet_gateway.this]
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "${var.name}-public" }
}

resource "aws_route" "public_internet" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.this.id
}

resource "aws_route_table_association" "public" {
  count = local.az_count

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# One route table per AZ (its own NAT), shared by the private and worker subnets of that AZ.
resource "aws_route_table" "private" {
  count  = local.az_count
  vpc_id = aws_vpc.this.id
  tags   = { Name = "${var.name}-private-${count.index + 1}" }
}

resource "aws_route" "private_nat" {
  count = local.az_count

  route_table_id         = aws_route_table.private[count.index].id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.this[count.index].id
}

resource "aws_route_table_association" "private" {
  count = local.az_count

  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}

resource "aws_route_table_association" "worker" {
  count = local.az_count

  subnet_id      = aws_subnet.worker[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}

# S3 gateway endpoint: S3 traffic stays off the NAT gateways (free).
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${data.aws_region.current.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = aws_route_table.private[*].id

  tags = { Name = "${var.name}-s3" }
}
