# CloudFront in front of everything:
#   default            → the SPA bucket (Origin Access Control)
#   /api/*             → the API (REST, WebSocket /api/v1/ws, Google OAuth callback)
#   /.well-known/*     → the API (OIDC discovery of the workload issuer)
#   /oidc/*            → the API (JWKS: the discovery's jwks_uri, fetched by AWS STS)
#   /healthz           → the API
# The worker protocol (/worker/*) and the state backend (/state/v1/*) are NOT public: workers run
# in the VPC and reach the internal ALB directly (STACKD_INTERNAL_URL).
#
# The API origin is the internal ALB through a CloudFront VPC origin, over HTTPS: CloudFront
# connects to internal_domain_name and verifies the ALB's ACM certificate for it.

locals {
  api_paths = ["/api/*", "/.well-known/*", "/oidc/*", "/healthz"]
}

data "aws_cloudfront_cache_policy" "disabled" {
  name = "Managed-CachingDisabled"
}

data "aws_cloudfront_cache_policy" "optimized" {
  name = "Managed-CachingOptimized"
}

# Every viewer header, cookie and query string, except Host: the origin sees internal_domain_name.
data "aws_cloudfront_origin_request_policy" "all_viewer_except_host" {
  name = "Managed-AllViewerExceptHostHeader"
}

data "aws_cloudfront_response_headers_policy" "security_headers" {
  name = "Managed-SecurityHeadersPolicy"
}

resource "aws_cloudfront_vpc_origin" "api" {
  vpc_origin_endpoint_config {
    name                   = "${var.name}-api"
    arn                    = var.alb_arn
    http_port              = 80
    https_port             = 443
    origin_protocol_policy = "https-only"

    origin_ssl_protocols {
      items    = ["TLSv1.2"]
      quantity = 1
    }
  }
}

resource "aws_cloudfront_distribution" "this" {
  enabled             = true
  is_ipv6_enabled     = true
  http_version        = "http2and3"
  comment             = var.name
  default_root_object = "index.html"
  price_class         = var.price_class
  aliases             = [var.domain_name]

  origin {
    origin_id                = "spa"
    domain_name              = var.spa_bucket_regional_domain_name
    origin_access_control_id = var.spa_oac_id
  }

  origin {
    origin_id   = "api"
    domain_name = var.internal_domain_name

    vpc_origin_config {
      vpc_origin_id            = aws_cloudfront_vpc_origin.api.id
      origin_keepalive_timeout = 60
      # Above the claim long-poll; CloudFront's default quota is 60 s.
      origin_read_timeout = 60
    }
  }

  default_cache_behavior {
    target_origin_id           = "spa"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD", "OPTIONS"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = true
    cache_policy_id            = data.aws_cloudfront_cache_policy.optimized.id
    response_headers_policy_id = data.aws_cloudfront_response_headers_policy.security_headers.id

    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.spa_rewrite.arn
    }
  }

  dynamic "ordered_cache_behavior" {
    for_each = local.api_paths
    content {
      path_pattern             = ordered_cache_behavior.value
      target_origin_id         = "api"
      viewer_protocol_policy   = "https-only"
      allowed_methods          = ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"]
      cached_methods           = ["GET", "HEAD"]
      compress                 = true
      cache_policy_id          = data.aws_cloudfront_cache_policy.disabled.id
      origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer_except_host.id
    }
  }

  viewer_certificate {
    acm_certificate_arn      = var.certificate_arn
    ssl_support_method       = "sni-only"
    minimum_protocol_version = "TLSv1.2_2021"
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }
}

# SPA routes (no file extension) → /index.html.
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

resource "aws_route53_record" "public" {
  for_each = var.manage_records ? toset(["A", "AAAA"]) : toset([])

  zone_id = var.route53_zone_id
  name    = var.domain_name
  type    = each.value

  alias {
    name                   = aws_cloudfront_distribution.this.domain_name
    zone_id                = aws_cloudfront_distribution.this.hosted_zone_id
    evaluate_target_health = false
  }
}
