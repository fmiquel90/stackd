locals {
  use_custom_domain   = var.domain_name != null
  use_existing_cert   = var.certificate_arn != null
  use_r53_validation  = local.use_custom_domain && !local.use_existing_cert && var.route53_zone_id != null
  create_cert         = local.use_custom_domain && !local.use_existing_cert

  resolved_cert_arn = local.use_existing_cert ? var.certificate_arn : (
    local.create_cert ? aws_acm_certificate_validation.this[0].certificate_arn : null
  )
}

# ── ACM certificate (us-east-1 required for CloudFront) ────────────────────────

resource "aws_acm_certificate" "this" {
  count    = local.create_cert ? 1 : 0
  provider = aws.us_east_1

  domain_name       = var.domain_name
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "cert_validation" {
  count = local.use_r53_validation ? 1 : 0

  zone_id = var.route53_zone_id
  name    = tolist(aws_acm_certificate.this[0].domain_validation_options)[0].resource_record_name
  type    = tolist(aws_acm_certificate.this[0].domain_validation_options)[0].resource_record_type
  records = [tolist(aws_acm_certificate.this[0].domain_validation_options)[0].resource_record_value]
  ttl     = 60
}

resource "aws_acm_certificate_validation" "this" {
  count    = local.create_cert ? 1 : 0
  provider = aws.us_east_1

  certificate_arn = aws_acm_certificate.this[0].arn
  validation_record_fqdns = local.use_r53_validation ? [
    aws_route53_record.cert_validation[0].fqdn
  ] : []
}

# ── CloudFront VPC Origin (internal ALB) ───────────────────────────────────────

resource "aws_cloudfront_vpc_origin" "api" {
  vpc_origin_endpoint_config {
    name                   = "${var.name}-api"
    arn                    = var.alb_arn
    http_port              = 80
    https_port             = 443
    origin_protocol_policy = "http-only"
  }
}

# ── CloudFront distribution ────────────────────────────────────────────────────

resource "aws_cloudfront_distribution" "this" {
  enabled             = true
  is_ipv6_enabled     = true
  comment             = var.name
  default_root_object = "index.html"
  price_class         = var.price_class

  aliases = local.use_custom_domain ? [var.domain_name] : []

  # S3 origin for SPA assets
  origin {
    origin_id                = "spa"
    domain_name              = var.spa_bucket_regional_domain_name
    origin_access_control_id = var.spa_oac_id
  }

  # Internal ALB via VPC Origin
  origin {
    origin_id   = "api"
    domain_name = var.alb_dns_name

    vpc_origin_config {
      vpc_origin_id            = aws_cloudfront_vpc_origin.api.id
      origin_keepalive_timeout = 60
      origin_read_timeout      = 60
    }
  }

  # Default behavior → SPA (S3)
  default_cache_behavior {
    target_origin_id       = "spa"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true

    forwarded_values {
      query_string = false
      cookies { forward = "none" }
    }

    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.spa_rewrite.arn
    }
  }

  # API + WebSocket — no cache, all methods, all headers/cookies forwarded
  ordered_cache_behavior {
    path_pattern           = "/api/*"
    target_origin_id       = "api"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"]
    cached_methods         = ["GET", "HEAD"]
    compress               = false

    forwarded_values {
      query_string = true
      headers      = ["*"]
      cookies { forward = "all" }
    }

    min_ttl     = 0
    default_ttl = 0
    max_ttl     = 0
  }

  ordered_cache_behavior {
    path_pattern           = "/worker/*"
    target_origin_id       = "api"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"]
    cached_methods         = ["GET", "HEAD"]
    compress               = false

    forwarded_values {
      query_string = true
      headers      = ["*"]
      cookies { forward = "all" }
    }

    min_ttl     = 0
    default_ttl = 0
    max_ttl     = 0
  }

  # OIDC discovery docs — short cache (5 min)
  ordered_cache_behavior {
    path_pattern           = "/.well-known/*"
    target_origin_id       = "api"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true

    forwarded_values {
      query_string = false
      cookies { forward = "none" }
    }

    min_ttl     = 0
    default_ttl = 300
    max_ttl     = 300
  }

  ordered_cache_behavior {
    path_pattern           = "/healthz"
    target_origin_id       = "api"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    compress               = false

    forwarded_values {
      query_string = false
      cookies { forward = "none" }
    }

    min_ttl     = 0
    default_ttl = 0
    max_ttl     = 0
  }

  viewer_certificate {
    acm_certificate_arn            = local.resolved_cert_arn
    cloudfront_default_certificate = local.resolved_cert_arn == null
    ssl_support_method             = local.resolved_cert_arn != null ? "sni-only" : null
    minimum_protocol_version       = local.resolved_cert_arn != null ? "TLSv1.2_2021" : "TLSv1"
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }
}

# CloudFront function: rewrite SPA routes without a dot to /index.html
resource "aws_cloudfront_function" "spa_rewrite" {
  name    = "${var.name}-spa-rewrite"
  runtime = "cloudfront-js-2.0"
  publish = true

  code = <<-JS
    function handler(event) {
      var req = event.request;
      if (!req.uri.includes('.') && req.uri !== '/') {
        req.uri = '/index.html';
      }
      return req;
    }
  JS
}

# ── Route 53 alias for custom domain ──────────────────────────────────────────

resource "aws_route53_record" "cloudfront" {
  count = local.use_custom_domain && var.route53_zone_id != null ? 1 : 0

  zone_id = var.route53_zone_id
  name    = var.domain_name
  type    = "A"

  alias {
    name                   = aws_cloudfront_distribution.this.domain_name
    zone_id                = aws_cloudfront_distribution.this.hosted_zone_id
    evaluate_target_health = false
  }
}
