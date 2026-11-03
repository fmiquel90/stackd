output "autoscaling_group_name" {
  value = aws_autoscaling_group.worker.name
}

output "security_group_id" {
  value = aws_security_group.worker.id
}

output "log_group_name" {
  value = aws_cloudwatch_log_group.worker.name
}
