output "vpc_id" {
  value = aws_vpc.this.id
}

output "vpc_cidr" {
  value = aws_vpc.this.cidr_block
}

output "private_subnet_ids" {
  value = aws_subnet.private[*].id
}

output "private_subnet_cidrs" {
  value = local.private_cidrs
}

output "worker_subnet_ids" {
  value = aws_subnet.worker[*].id
}

output "worker_subnet_cidrs" {
  value = local.worker_cidrs
}
