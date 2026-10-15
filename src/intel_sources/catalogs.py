"""Read-only catalog discovery; listings never prove inference or free entitlement."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import quote, urlsplit

import httpx

OPENROUTER_MODELS = "https://openrouter.ai/api/v1/models"
OPENROUTER_PROVIDERS = "https://openrouter.ai/api/v1/providers"
UNOROUTER_CATALOG = "https://api.unorouter.com/api/pricing/catalog"
UNVERIFIED = "Listed model; inference and free entitlement unverified."


def _pick(row, fields):
    return {key: row[key] for key in fields if key in row}


def _project_types(row):
    architecture = row.get("architecture") or {}
    inputs = architecture.get("input_modalities") or []
    outputs = architecture.get("output_modalities") or []
    kinds = set(inputs + outputs)
    types = kinds.intersection({"text", "audio", "video", "embeddings", "speech", "transcription", "rerank"})
    if "image" in inputs:
        types.add("vision")
    if "image" in outputs:
        types.add("image-generation")
    if len(kinds) > 1:
        types.add("multimodal")
    if "tools" in (row.get("supported_parameters") or []):
        types.add("tools")
    if {"reasoning", "reasoning_effort", "include_reasoning"}.intersection(row.get("supported_parameters") or []):
        types.add("reasoning")
    return sorted(types)


def _fingerprint(value, field=None):
    if isinstance(value, dict):
        return {k: _fingerprint(v, k) for k, v in value.items()
                if k not in {"created", "created_at", "updated", "updated_at", "checked_at", "release_ts", "timestamp", "uptime_24h", "success_rate", "avg_latency_ms"}}
    if isinstance(value, list):
        normalized = [_fingerprint(v) for v in value]
        if field in {"input_modalities", "output_modalities", "supported_parameters", "supported_endpoint_types", "tags", "supported_voices", "supported_efforts"}:
            return sorted(normalized)
        return normalized
    return value


def _item(namespace, identifier, headline, source, url, properties, types, **evidence):
    return {
        "id": f"{namespace}:{identifier}", "headline": headline, "source": source,
        "summary": properties.get("description") or (
            "Listed hosting provider; inference and free entitlement unverified."
            if evidence.get("kind") == "provider" else UNVERIFIED),
        "url": url, "project_types": types,
        "evidence": {**properties, **evidence, "inference_verified": False,
                     "free_entitlement_verified": False},
        "fingerprint_data": _fingerprint(properties),
    }


def _openrouter_model(row):
    properties = _pick(row, ("id", "name", "description", "pricing", "context_length",
                             "architecture", "supported_parameters", "top_provider", "canonical_slug", "hugging_face_id",
                             "per_request_limits", "default_parameters", "reasoning", "supported_voices", "created",
                             "knowledge_cutoff", "expiration_date", "alias_target", "links"))
    return _item("openrouter:model", row["id"], row.get("name") or row["id"],
                 "openrouter-models", "https://openrouter.ai/" + quote(row["id"], safe="/:"),
                 properties, _project_types(row), catalog_url=OPENROUTER_MODELS)


def _openrouter_provider(row):
    properties = _pick(row, ("slug", "name", "headquarters", "datacenters",
                             "privacy_policy_url", "terms_of_service_url", "status_page_url"))
    return _item("openrouter:provider", row["slug"], row.get("name") or row["slug"],
                 "openrouter-providers", "https://openrouter.ai/provider/" + quote(row["slug"], safe=""),
                 properties, [], kind="provider", catalog_url=OPENROUTER_PROVIDERS)


def _unorouter_model(row):
    tags = row.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]
    capabilities = {t.lower() for t in tags}
    types = capabilities.intersection({"text", "tools", "reasoning", "audio", "video", "embeddings"})
    if row.get("type") in {"text", "audio", "video", "embeddings"}:
        types.add(row["type"])
    if row.get("type") == "embedding" or "embedding" in capabilities:
        types.add("embeddings")
    if "vision" in capabilities:
        types.update({"vision", "multimodal"})
    if row.get("type") == "image":
        types.add("image-generation")
    pricing = _pick(row, ("input_price", "output_price", "fixed_price", "is_fixed_price",
                          "original_input_price", "original_output_price", "original_fixed_price"))
    properties = _pick(row, ("model_name", "vendor", "vendor_id", "type", "is_free", "online",
                             "supported_endpoint_types", "chat", "release_ts"))
    properties.update({"tags": sorted(tags), "pricing": pricing})
    return _item("unorouter:model", row["model_name"], row["model_name"], "unorouter-models",
                 "https://unorouter.com/en/models/" + quote(row["model_name"], safe=":"),
                 properties, sorted(types), catalog_url=UNOROUTER_CATALOG,
                 pricing_units="USD per million tokens (input/output); fixed price per request")


def _unorouter_accessible_model(row):
    url = "https://api.unorouter.com/v1/models"
    properties = _pick(row, ("id", "name", "owned_by", "context_length", "max_output_tokens",
                             "architecture", "supported_parameters", "pricing"))
    return _item("unorouter:accessible-model", row["id"], row.get("name") or row["id"],
                 "unorouter-accessible-models", url + "#" + quote(row["id"], safe=""),
                 properties, _project_types(row), catalog_url=url, catalog_scope="key-accessible",
                 provenance={"kind": "key_scoped_catalog", "independent_of_upstream": False,
                             "upstream_source": "unorouter"})


def _local_model(row, catalog_url):
    properties = _pick(row, ("id", "name", "description", "owned_by", "source", "provider",
                             "upstream_model_id", "pricing", "context_length", "architecture",
                             "supported_parameters"))
    provenance = {"kind": "local_aggregated_catalog", "independent_of_upstream": False,
                  "upstream_source": row.get("source")}
    return _item("freellmapi:model", row["id"], row.get("name") or row["id"],
                 "freellmapi-models", catalog_url + "#" + quote(row["id"], safe=""),
                 properties, _project_types(row), catalog_url=catalog_url, provenance=provenance)


def _source(name, url, status="error", count=0, error=None):
    return {"name": name, "status": status,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "item_count": count, "error": error, "url": url}


def _sanitize(value, secrets):
    if isinstance(value, str):
        for secret in secrets:
            for variant in {secret, quote(secret, safe="")}:
                value = value.replace(variant, "[REDACTED]")
        return value
    if isinstance(value, dict):
        sensitive = {"apikey", "authorization", "password", "secret", "token", "accesstoken", "cookie", "credentials"}
        return {_sanitize(k, secrets): _sanitize(v, secrets) for k, v in value.items()
                if k.lower().replace("_", "").replace("-", "") not in sensitive}
    if isinstance(value, list):
        return [_sanitize(v, secrets) for v in value]
    return value


def _validate_row(row, identifier_field):
    if not isinstance(row, dict):
        raise ValueError("Invalid row")
    identifier = row.get(identifier_field)
    if not isinstance(identifier, str) or not identifier or identifier != identifier.strip() or any(ord(c) < 32 for c in identifier):
        raise ValueError("Invalid identifier")
    for key in ("name", "description", "owned_by", "source", "provider", "type"):
        if row.get(key) is not None and not isinstance(row[key], str):
            raise ValueError("Invalid text field")
    for key in ("pricing", "architecture", "top_provider"):
        if row.get(key) is not None and not isinstance(row[key], dict):
            raise ValueError("Invalid object field")
    for limits in (row, row.get("top_provider") or {}):
        for key in ("context_length", "max_output_tokens", "max_completion_tokens"):
            value = limits.get(key)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("Invalid context")
    for key in ("is_free", "online", "chat", "is_fixed_price"):
        if row.get(key) is not None and type(row[key]) is not bool:
            raise ValueError("Invalid catalog flag")
    tags = row.get("tags")
    if tags is not None and not (isinstance(tags, str) or (
            isinstance(tags, list) and all(isinstance(t, str) for t in tags))):
        raise ValueError("Invalid catalog tags")
    pricing = row.get("pricing") or {}
    price_fields = ("prompt", "completion", "request", "image", "web_search", "input_cache_read",
                    "input_cache_write", "image_output", "image_token", "audio", "audio_output",
                    "input_cache_write_1h", "internal_reasoning", "input_audio_cache")
    overrides = pricing.get("overrides")
    if overrides is not None and (not isinstance(overrides, list) or
                                  not all(isinstance(tier, dict) for tier in overrides)):
        raise ValueError("Invalid pricing tiers")
    for tier in overrides or []:
        threshold = tier.get("min_prompt_tokens")
        if threshold is not None and (type(threshold) is not int or threshold < 0):
            raise ValueError("Invalid pricing threshold")
    price_objects = [(pricing, price_fields)] + [(tier, price_fields) for tier in overrides or []]
    price_objects.append((row, ("input_price", "output_price", "fixed_price", "original_input_price",
                                "original_output_price", "original_fixed_price")))
    for values, fields in price_objects:
        for key in fields:
            value = values.get(key)
            if value is None:
                continue
            try:
                if isinstance(value, bool) or not Decimal(str(value)).is_finite():
                    raise ValueError("Invalid price")
            except InvalidOperation:
                raise ValueError("Invalid price") from None
    architecture = row.get("architecture") or {}
    for key in ("input_modalities", "output_modalities"):
        value = architecture.get(key)
        if value is not None and (not isinstance(value, list) or not all(isinstance(v, str) for v in value)):
            raise ValueError("Invalid modalities")
    for key in ("supported_parameters", "supported_endpoint_types"):
        value = row.get(key)
        if value is not None and (not isinstance(value, list) or not all(isinstance(v, str) for v in value)):
            raise ValueError("Invalid capabilities")


async def _fetch_source(client, name, url, container, parser, secrets=(), **request_kwargs):
    try:
        explicit_headers = request_kwargs.pop("headers", {})
        request = client.build_request("GET", url, timeout=20.0, **request_kwargs)
        request.url = httpx.URL(url).copy_merge_params(request_kwargs.get("params", {}))
        # Shared clients can have unrelated defaults. Only explicit endpoint keys travel.
        for header in list(request.headers):
            if header not in {"host", "accept", "accept-encoding", "connection", "user-agent"}:
                del request.headers[header]
        request.headers.update(explicit_headers)
        response = await client.send(request, auth=None, follow_redirects=False)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("success") is False or payload.get("error"):
            return [], _source(name, url, error="Invalid or error catalog response")
        rows = payload.get(container)
        if not isinstance(rows, list) or not rows:
            return [], _source(name, url, error="Empty or invalid catalog list")
        items, invalid, duplicates, seen = [], 0, 0, set()
        identifier_field = "model_name" if container == "models" else "slug" if name == "openrouter-providers" else "id"
        for row in rows:
            try:
                _validate_row(row, identifier_field)
                if any(secret in row[identifier_field] for secret in secrets):
                    raise ValueError("Credential-bearing identifier")
                item = parser(_sanitize(row, secrets))
                if item["id"] in seen:
                    duplicates += 1
                    continue
                seen.add(item["id"])
                items.append(item)
            except (KeyError, TypeError, AttributeError, ValueError):
                invalid += 1
        links = payload.get("links")
        total = payload.get("total_count")
        if total is None and isinstance(payload.get("counts"), dict):
            total = payload["counts"].get("models")
        has_more = isinstance(links, dict) and bool(links.get("next"))
        incomplete = has_more or (type(total) is int and total > len(rows))
        inconsistent = total is not None and (type(total) is not int or total < len(rows))
        status = "partial" if (invalid or duplicates or incomplete or inconsistent) and items else "ok" if items else "error"
        errors = []
        if invalid:
            errors.append(f"Ignored {invalid} invalid catalog entries")
        if duplicates:
            errors.append(f"Ignored {duplicates} duplicate catalog entries")
        if incomplete:
            errors.append("Incomplete catalog response; pagination not followed")
        if inconsistent:
            errors.append("Inconsistent declared catalog total")
        error = "; ".join(errors) or None
        source = _source(name, url, status, len(items), error)
        source["coverage"] = {
            "scope": "key-accessible" if name == "unorouter-accessible-models" else
                     "local-aggregated" if name == "freellmapi-models" else "public-catalog",
            "returned_count": len(rows), "accepted_count": len(items),
            "invalid_count": invalid, "duplicate_count": duplicates,
            "declared_total": total if type(total) is int and total >= 0 else None,
            "has_more": bool(has_more), "pagination_followed": False,
        }
        if name == "openrouter-models":
            source["coverage"]["output_modalities"] = "all"
        # Collection time is not origin freshness. Persist bounded numeric cache
        # diagnostics, never arbitrary response headers or credential-bearing text.
        age_text = response.headers.get("age", "").strip()
        age = int(age_text) if age_text.isascii() and age_text.isdigit() and len(age_text) <= 10 else None
        max_age = None
        for directive in response.headers.get("cache-control", "").split(","):
            key, sep, value = directive.strip().partition("=")
            value = value.strip().strip('"')
            if key.lower() == "max-age" and sep and value.isascii() and value.isdigit() and len(value) <= 10:
                max_age = int(value)
        source["coverage"].update({
            "http_cache_age_seconds": age, "http_cache_max_age_seconds": max_age,
            "http_cache_freshness": "unknown" if age is None or max_age is None else
                                    "exceeds-max-age" if age >= max_age else "within-max-age",
        })
        return items, source
    except httpx.HTTPStatusError as exc:
        return [], _source(name, url, error=f"HTTP {exc.response.status_code}")
    except httpx.TimeoutException:
        return [], _source(name, url, error="Request timed out")
    except httpx.RequestError:
        return [], _source(name, url, error="Network request failed")
    except ValueError:
        return [], _source(name, url, error="Invalid JSON response")
    except (KeyError, TypeError, AttributeError):
        return [], _source(name, url, error="Invalid catalog entry")


def _local_url(base):
    try:
        parsed = urlsplit(base)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            return None
        return base.rstrip("/") + "/v1/models"
    except (TypeError, ValueError, AttributeError):
        return None


async def fetch_catalogs(client: httpx.AsyncClient,
                         freellmapi_url="http://localhost:3001", api_key=None,
                         unorouter_api_key=None, include_local=True) -> dict:
    """Return catalog DATA, keeping each stream's freshness and failures independent."""
    items, sources = [], []
    secrets = tuple(k for k in (api_key, unorouter_api_key) if isinstance(k, str) and k)
    for args, kwargs in [
        (("openrouter-models", OPENROUTER_MODELS, "data", _openrouter_model),
         {"params": {"output_modalities": "all"}}),
        (("openrouter-providers", OPENROUTER_PROVIDERS, "data", _openrouter_provider), {}),
        (("unorouter-models", UNOROUTER_CATALOG, "models", _unorouter_model), {}),
    ]:
        rows, source = await _fetch_source(client, *args, secrets=secrets, **kwargs)
        items.extend(rows)
        sources.append(source)
    if unorouter_api_key:
        rows, source = await _fetch_source(
            client, "unorouter-accessible-models", "https://api.unorouter.com/v1/models", "data",
            _unorouter_accessible_model, secrets=secrets, headers={"Authorization": "Bearer " + unorouter_api_key})
        items.extend(rows)
        sources.append(source)
    local_url = _local_url(freellmapi_url)
    if not local_url:
        sources.append(_source("freellmapi-models", "[invalid]", error="Invalid FreeLLMAPI base URL"))
    elif include_local and api_key:
        rows, source = await _fetch_source(
            client, "freellmapi-models", local_url, "data", lambda row: _local_model(row, local_url),
            secrets=secrets, headers={"Authorization": "Bearer " + api_key})
        items.extend(rows)
        sources.append(source)
    else:
        reason = "Local catalog disabled" if not include_local else "Explicit FreeLLMAPI API key required"
        sources.append(_source("freellmapi-models", local_url, "skipped", error=reason))
    return _sanitize({"items": items, "sources": sources}, secrets)
