"""Conservative source-engine residency check before a new native allocation.

This is a read-only observation, not an atomic reservation across two engines.
Osaurus remains responsible for its own loading, eviction and active requests.
"""
from urllib.parse import quote

from .idle_policy import normalize_endpoint
from .osaurus_sync import read_json


POLICY = "osaurus_residency_policy"
UNAVAILABLE = "osaurus_residency_unavailable"


def _model_id(value):
    return isinstance(value, str) and 0 < len(value) <= 1024 and all(ord(c) >= 32 and ord(c) != 127 for c in value)


def native_admission(profile, config, *, reader=read_json):
    """Return a fixed proxy reason, or None if every source model is cold.

    Only source IDs belonging to this snapshot and endpoint are queried. No
    unload request is issued, even when a source model currently appears idle.
    """
    if not profile.get("synced") or profile.get("settings_source") == "fixed" or profile.get("mode") == "osaurus":
        return None
    try:
        source = profile.get("_osaurus")
        if not isinstance(source, dict) or not _model_id(source.get("model_id")):
            return UNAVAILABLE
        endpoint = normalize_endpoint(source.get("endpoint"))
        identifiers = {source["model_id"]}
        models = config.get("models", [])
        if not isinstance(models, list):
            return UNAVAILABLE
        for model in models:
            other = model.get("_osaurus") if isinstance(model, dict) else None
            if not isinstance(other, dict):
                continue
            try:
                other_endpoint = normalize_endpoint(other.get("endpoint"))
            except Exception:
                continue
            if other_endpoint == endpoint:
                if not _model_id(other.get("model_id")):
                    return UNAVAILABLE
                identifiers.add(other["model_id"])
        for identifier in sorted(identifiers):
            value = reader(endpoint, "/admin/models/residency?model=" + quote(identifier, safe=""), timeout=1.0)
            if not isinstance(value, dict):
                return UNAVAILABLE
            state, held = value.get("state"), value.get("heldBytes")
            if not isinstance(state, str) or type(held) is not int or held < 0:
                return UNAVAILABLE
            flags = [value.get("busy"), value.get("keepLoaded"), value.get("generating", False)]
            if any(type(flag) is not bool for flag in flags):
                return UNAVAILABLE
            state = state.casefold()
            if state not in ("cold", "unloaded", "loaded", "loading", "unloading", "busy"):
                return UNAVAILABLE
            if state in ("loaded", "loading", "unloading", "busy") or held or any(flags):
                return POLICY
        return None
    except Exception:
        # Source HTTP exceptions and malformed values never carry private data
        # into diagnostics. Delegating lets Osaurus apply its own resident limit.
        return UNAVAILABLE
