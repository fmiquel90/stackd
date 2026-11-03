output "alb_arn" {
  value = aws_lb.api.arn
}

output "alb_dns_name" {
  value = aws_lb.api.dns_name
}

output "alb_zone_id" {
  value = aws_lb.api.zone_id
}

output "alb_security_group_id" {
  value = aws_security_group.alb.id
}

output "api_security_group_id" {
  value = aws_security_group.api.id
}

output "cluster_name" {
  value = aws_ecs_cluster.this.name
}

output "service_name" {
  value = try(aws_ecs_service.api[0].name, null)
}

output "log_group_name" {
  value = aws_cloudwatch_log_group.api.name
}

output "migrate_command" {
  description = "Runs the migration task by hand (when run_migrations = false)."
  value       = var.enabled ? format(
    "%s bash deploy/terraform/scripts/run-migrations.sh",
    join(" ", [for k, v in local.migrate_env : "${k}='${v}'"]),
  ) : null
}
