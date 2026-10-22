"""Public configuration masking and explicit preservation of stored secrets."""
from fastapi import HTTPException

MASKED_SECRET = "********"
_SECRET_NAMES = frozenset({"api_key", "x_api_key", "authorization", "proxy_authorization",
                           "access_token", "token", "secret", "key"})


def is_unchanged_secret(value) -> bool:
    return value is None or (isinstance(value, str) and value.strip() in ("", MASKED_SECRET))


def public_secret(value: str) -> str:
    return MASKED_SECRET if value else ""


def preserve_secret(incoming, existing: str = "") -> str:
    if is_unchanged_secret(incoming):
        if incoming == MASKED_SECRET and not existing:
            raise HTTPException(400, "该配置没有已保存的密钥，请填写 API Key")
        return existing
    return str(incoming)


def _secret_name(name) -> bool:
    return str(name).lower().replace("-", "_") in _SECRET_NAMES


def _public_mapping(value):
    if isinstance(value, dict):
        return {key: public_secret(str(item)) if _secret_name(key) and item
                else _public_mapping(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_public_mapping(item) for item in value]
    return value


def _preserve_mapping(incoming, existing):
    if isinstance(incoming, dict):
        prior = existing if isinstance(existing, dict) else {}
        return {key: preserve_secret(value, prior.get(key, "")) if _secret_name(key)
                else _preserve_mapping(value, prior.get(key)) for key, value in incoming.items()}
    if isinstance(incoming, list):
        prior = existing if isinstance(existing, list) else []
        return [_preserve_mapping(item, prior[index] if index < len(prior) else None)
                for index, item in enumerate(incoming)]
    return incoming


def preserve_secret_mapping(incoming, existing=None):
    """Resolve masked connection fields without returning stored secrets to UI."""
    return _preserve_mapping(incoming, existing)


def public_profile(profile):
    return profile.model_copy(deep=True, update={
        "api_key": public_secret(profile.api_key),
        **{field: _public_mapping(getattr(profile, field))
           for field in ("extra_headers", "extra_query", "extra_body")},
    })


def preserve_profile(profile, existing=None):
    return profile.model_copy(deep=True, update={
        "api_key": preserve_secret(profile.api_key, existing.api_key if existing else ""),
        **{field: _preserve_mapping(getattr(profile, field), getattr(existing, field) if existing else {})
           for field in ("extra_headers", "extra_query", "extra_body")},
    })


def public_panel(panel):
    config = panel.config.model_copy(deep=True, update={
        "profiles": [public_profile(profile) for profile in panel.config.profiles],
    })
    return panel.model_copy(deep=True, update={"config": config})


def public_embedding(data):
    result = dict(data)
    result["api_key"] = public_secret(result.get("api_key") or "")
    result["has_api_key"] = bool(data.get("api_key"))
    return result
