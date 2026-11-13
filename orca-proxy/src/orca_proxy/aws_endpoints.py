"""Shared SDK-backed recognition and signing scopes for supported AWS APIs."""

from functools import lru_cache

from botocore.session import Session


@lru_cache(maxsize=1)
def _endpoint_metadata():
    """Keep endpoint metadata and signing names, without retaining SDK clients."""
    session = Session()
    resolver = session._get_internal_component("endpoint_resolver")
    partitions = session.get_component("data_loader").load_data("endpoints")["partitions"]
    signing_names = {}
    for name in session.get_available_services():
        model = session.get_service_model(name)
        signing_names[model.endpoint_prefix] = model.signing_name
    regions = {region for partition in partitions for region in partition["regions"]}
    globals_ = [
        (prefix, region, partition["partition"])
        for partition in partitions
        for prefix, service in partition["services"].items()
        if prefix in signing_names
        for region, endpoint in service.get("endpoints", {}).items()
        if region not in regions and not endpoint.get("deprecated", False)
    ]
    return resolver, signing_names, regions, globals_


def _add_endpoints(catalog, prefix, region, partition=None):
    from botocore.exceptions import EndpointVariantError

    resolver, signing_names, regions, _ = _endpoint_metadata()
    for dualstack, fips in ((False, False), (True, False), (False, True), (True, True)):
        try:
            endpoint = resolver.construct_endpoint(
                prefix, region, partition_name=partition,
                use_dualstack_endpoint=dualstack, use_fips_endpoint=fips,
            )
        except EndpointVariantError:
            continue
        if not endpoint:
            continue
        scope = endpoint.get("credentialScope", {})
        signing_region = scope.get("region", region)
        if signing_region not in regions:
            continue
        hostname = endpoint["hostname"]
        if "{" not in hostname:
            catalog[hostname] = (scope.get("service", signing_names[prefix]), signing_region)


@lru_cache(maxsize=16)
def _regional_signing_scopes(region):
    catalog = {}
    for prefix in _endpoint_metadata()[1]:
        _add_endpoints(catalog, prefix, region)
    return catalog


@lru_cache(maxsize=1)
def _global_signing_scopes():
    catalog = {}
    for prefix, region, partition in _endpoint_metadata()[3]:
        _add_endpoints(catalog, prefix, region, partition)
    return catalog


@lru_cache(maxsize=4096)
def signing_scope(hostname: str) -> tuple[str, str] | None:
    """Return the supported API's signing scope, or None for other hosts.

    Resource subdomains are supported only for explicit families shared with
    automatic signing; AWS DNS ownership alone is insufficient.
    """
    hostname = hostname.lower().rstrip(".")
    known_regions = _endpoint_metadata()[2]
    region = next((label for label in hostname.split(".") if label in known_regions), None)
    catalog = _regional_signing_scopes(region) if region else _global_signing_scopes()
    scope = catalog.get(hostname)
    if scope is None:
        # Only these known endpoint families accept extra resource labels.
        for endpoint, endpoint_scope in catalog.items():
            if endpoint_scope[0] in {"s3", "execute-api"} and hostname.endswith("." + endpoint):
                scope = endpoint_scope
                break
    return scope
