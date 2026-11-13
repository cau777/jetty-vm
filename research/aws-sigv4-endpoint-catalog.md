# AWS SigV4 endpoint catalog

Researched 2026-11-13 for Jetty's `@aws` selector.

## Authoritative lists

- Human-readable service endpoint index: [AWS service endpoints](https://docs.aws.amazon.com/general/latest/gr/rande.html), linking to each service's endpoint table.
- Machine-readable endpoint catalog: AWS SDK's [botocore/data/endpoints.json](https://github.com/boto/botocore/blob/develop/botocore/data/endpoints.json). This is endpoint metadata, not a blanket assertion that every request needs signing.
- Current service-specific endpoint construction: `botocore/data/<service>/<api-version>/endpoint-rule-set-1.json`, together with `service-2.json` authentication metadata. For example, [ELBv2 endpoint rules](https://raw.githubusercontent.com/boto/botocore/develop/botocore/data/elbv2/2015-12-01/endpoint-rule-set-1.json) and its [service model](https://raw.githubusercontent.com/boto/botocore/develop/botocore/data/elbv2/2015-12-01/service-2.json).

## Authentication is separate from hostname ownership

Use modeled service/operation authentication to determine whether signing applies, then endpoint properties for signing overrides. The [Smithy endpoint rules specification](https://smithy.io/2.0/additional-specs/rules-engine/specification.html#endpoint-authschemes-list-property) explicitly says endpoint `authSchemes` modifies the already resolved modeled scheme; it is not the primary scheme selector. Consequently, filtering only endpoints with an explicit `authSchemes: sigv4` would miss valid APIs: ELBv2 rules have empty endpoint properties, while its service model declares `aws.auth#sigv4` and `signatureVersion: v4`.

The endpoint `sigv4` scheme can specify `signingName` and `signingRegion`; `sigv4a` uses a region set and is a distinct signing algorithm. See [AWS rules engine authentication schemes](https://smithy.io/2.0/aws/rules-engine/auth-schemes.html). Services and operations may also have unsigned or alternative authentication; a hostname catalog alone cannot establish that every request requires SigV4.

## The reported ALB

The ELB management API is `elasticloadbalancing.us-east-1.amazonaws.com`, with signing service `elasticloadbalancing`. AWS lists it in the [ELB endpoint table](https://docs.aws.amazon.com/general/latest/gr/elb.html), and its SDK service model declares SigV4.

`app-alb-ed2ac38-1850627321.us-east-1.elb.amazonaws.com` is a load balancer's application-facing hostname, not that management API. An [Application Load Balancer routes client requests to application targets](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/application-load-balancers.html). Inference: its AWS DNS suffix does not justify injecting AWS management credentials or forcing TLS interception.

## Resource-specific hostnames

A finite list of exact regional API hosts is useful but incomplete. S3 supports [`<bucket>.s3.<region>.amazonaws.com`](https://docs.aws.amazon.com/AmazonS3/latest/userguide/VirtualHosting.html) and [access-point hostnames](https://docs.aws.amazon.com/AmazonS3/latest/userguide/access-points-naming.html). These need explicitly modeled service-specific patterns rather than arbitrary subdomains of every AWS endpoint.

API Gateway illustrates why “supports signing” differs from “requires signing”: [HTTP API routes require SigV4 when IAM authorization is enabled](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-access-control-iam.html). Hostname alone cannot reveal the route's configured authorization.

## Recommendation for Jetty

Replace the `@aws` DNS suffix test with a shared endpoint recognizer backed by the pinned SDK catalog and signing metadata. Match known exact API hosts, plus explicitly supported resource-host families; unrecognized AWS-hosted applications should pass through under ordinary policy. Keep SigV4a and unsigned/alternative-auth operations distinct. Custom endpoints should require explicit rules rather than inference from their suffix.

Local code inspection: `aws_signing.resolve_injection` already performs a narrower SDK-backed lookup and rejects the reported ALB, while `rule_engine._matches_hostname('@aws', ...)` admits every subdomain of `amazonaws.com`. The mismatch occurs before request signing, at the TLS interception decision. Sharing endpoint recognition across these stages would prevent this particular mismatch. The existing resolver uses legacy endpoint metadata and is a starting point, not proof of complete modern endpoint coverage.
