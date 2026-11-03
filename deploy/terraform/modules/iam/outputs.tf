output "api_execution_role_arn" {
  value = aws_iam_role.api_execution.arn
}

output "api_task_role_arn" {
  value = aws_iam_role.api_task.arn
}
