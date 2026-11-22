"""Optional evidence envelope. Preserve reader facts; never certify business truth."""
from copy import deepcopy
import os

from .errors import DriveError

SCHEMA = "df-evidence-envelope/1"


def _reported_bool(value):
    return value if type(value) is bool else None


def _all_reported(values):
    values = list(values)
    if False in values:
        return False
    return True if values and all(value is True for value in values) else None


def envelope(result, method, config, params=None, selected=None):
    """Wrap a CLI result using the same connection's config snapshot.

    A completed selection is not an exhaustive business population. Unknown
    checks remain null; the original response, errors and warnings stay intact.
    No source, index, credential or network is opened by this function.
    """
    params = params or {}
    if selected and (not config or selected.get("config_fingerprint") != config.get("config_fingerprint") or
                     os.path.normcase(os.path.abspath(selected.get("config_path", ""))) !=
                     os.path.normcase(os.path.abspath(str(config.get("path", ""))))):
        raise DriveError("CONFIG_CHANGED", "The selected catalog entry does not match this reader connection.", False,
                         "Select the intended source again; do not attach one profile to another profile's evidence.")
    effective = config["config"] if config else {}
    provider = effective.get("provider", "local") if config else None
    if provider == "microsoft_graph":
        graph = effective["graph"]
        root = {"drive_id": graph["drive_id"], "folder_id": graph["folder_id"]}
    else:
        root = effective.get("root")
    pages = result.get("pages")
    responses = pages if isinstance(pages, list) and pages else [result]
    pagination = _reported_bool(result.get("pagination_complete"))
    if pagination is None and type(result.get("has_more")) is bool:
        pagination = not result["has_more"]
    extraction = _all_reported(_reported_bool(item.get("extraction_complete")) for item in responses)
    inventory = _all_reported(_reported_bool(item.get("coverage", {}).get("inventory_complete"))
                              for item in responses)
    source_ids = {item.get("source_id") for item in responses if isinstance(item.get("source_id"), str)}
    source = {"profile_id": selected.get("profile_id") if selected else None,
              "source_id": next(iter(source_ids)) if len(source_ids) == 1 else None,
              "config_id": config["config_id"] if config else None, "provider": provider,
              "scope": {"source": root, "prefix": params.get("prefix", "")}}
    if selected:
        source["catalog_source_id"] = selected["catalog_source_id"]
        source["config_fingerprint"] = selected["config_fingerprint"]
    request = {key: deepcopy(params[key]) for key in
               ("file_id", "expected_version", "selector", "start", "limit") if key in params}
    return {"ok": result.get("ok") is True, "schema_version": SCHEMA, "method": method,
            "source": source, "request": request,
            "technical": {"request_succeeded": result.get("ok") is True,
                          "pagination_complete": pagination, "extraction_complete": extraction,
                          "inventory_complete": inventory},
            "business_completeness": "not_established", "result": deepcopy(result)}
