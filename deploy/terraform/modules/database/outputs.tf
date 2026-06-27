output "security_group_id" {
  value = aws_security_group.db.id
}

output "endpoint" {
  value = aws_db_instance.this.endpoint
}

output "database_url" {
  value     = "postgresql+asyncpg://stackd:${random_password.db.result}@${aws_db_instance.this.endpoint}/stackd"
  sensitive = true
}
