#!/usr/bin/env python3
"""Reject selected breaking changes between an OpenAPI baseline and current schema."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, MutableSet, Optional, Sequence, Tuple

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}


def _load_json(path: Path) -> Dict[str, Any]:
    """Load a JSON object from disk with a concise validation error.

    Args:
        path: JSON file path.

    Returns:
        dict[str, Any]: Parsed JSON object.

    Raises:
        ValueError: File root is not an object.
    """
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"OpenAPI document must be a JSON object: {path}")
    return value


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    """Write a mapping as canonical UTF-8 JSON.

    Args:
        path: Destination path.
        value: JSON mapping to write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _decode_json_pointer_token(token: str) -> str:
    """Decode one RFC 6901 JSON pointer token.

    Args:
        token: Encoded token.

    Returns:
        str: Decoded object key.
    """
    return token.replace("~1", "/").replace("~0", "~")


def _resolve_ref(document: Mapping[str, Any], value: Any) -> Any:
    """Resolve a local ``$ref`` object and return non-ref values unchanged.

    Args:
        document: Complete OpenAPI document.
        value: Candidate mapping containing a local ``$ref``.

    Returns:
        Any: Resolved target or original value when no local reference exists.
    """
    if not isinstance(value, Mapping) or "$ref" not in value:
        return value
    ref = value["$ref"]
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return value
    current: Any = document
    for token in ref[2:].split("/"):
        if not isinstance(current, Mapping):
            return value
        current = current.get(_decode_json_pointer_token(token))
        if current is None:
            return value
    return current


def _parameter_map(
    document: Mapping[str, Any],
    path_item: Mapping[str, Any],
    operation: Mapping[str, Any],
) -> Dict[Tuple[str, str], Mapping[str, Any]]:
    """Merge path-level and operation-level parameters keyed by location/name.

    Args:
        document: Complete OpenAPI document.
        path_item: Path item object.
        operation: HTTP operation object.

    Returns:
        dict[tuple[str, str], Mapping[str, Any]]: Effective parameters.
    """
    parameters: Dict[Tuple[str, str], Mapping[str, Any]] = {}
    for raw in list(path_item.get("parameters", [])) + list(operation.get("parameters", [])):
        parameter = _resolve_ref(document, raw)
        if not isinstance(parameter, Mapping):
            continue
        name = parameter.get("name")
        location = parameter.get("in")
        if isinstance(name, str) and isinstance(location, str):
            parameters[(location, name)] = parameter
    return parameters


def _request_body(document: Mapping[str, Any], operation: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    """Return a resolved request body mapping when present.

    Args:
        document: Complete OpenAPI document.
        operation: HTTP operation object.

    Returns:
        Optional[Mapping[str, Any]]: Resolved body or ``None``.
    """
    body = _resolve_ref(document, operation.get("requestBody"))
    return body if isinstance(body, Mapping) else None


def _compare_request_schema(
    baseline_document: Mapping[str, Any],
    current_document: Mapping[str, Any],
    baseline_schema: Any,
    current_schema: Any,
    location: str,
    changes: List[str],
    seen: MutableSet[Tuple[int, int]],
) -> None:
    """Recursively detect newly required fields in request schemas.

    Args:
        baseline_document: Baseline OpenAPI document.
        current_document: Current OpenAPI document.
        baseline_schema: Baseline schema node.
        current_schema: Current schema node.
        location: Human-readable operation/schema location.
        changes: Mutable list receiving breaking-change messages.
        seen: Resolved schema identity pairs already compared.
    """
    baseline = _resolve_ref(baseline_document, baseline_schema)
    current = _resolve_ref(current_document, current_schema)
    if not isinstance(baseline, Mapping) or not isinstance(current, Mapping):
        return
    identity = (id(baseline), id(current))
    if identity in seen:
        return
    seen.add(identity)

    baseline_required = {item for item in baseline.get("required", []) if isinstance(item, str)}
    current_required = {item for item in current.get("required", []) if isinstance(item, str)}
    for field in sorted(current_required - baseline_required):
        changes.append(f"required request field added: {location}.{field}")

    baseline_properties = baseline.get("properties", {})
    current_properties = current.get("properties", {})
    if isinstance(baseline_properties, Mapping) and isinstance(current_properties, Mapping):
        for name in sorted(set(baseline_properties) & set(current_properties)):
            _compare_request_schema(
                baseline_document,
                current_document,
                baseline_properties[name],
                current_properties[name],
                f"{location}.{name}",
                changes,
                seen,
            )

    if "items" in baseline and "items" in current:
        _compare_request_schema(
            baseline_document,
            current_document,
            baseline["items"],
            current["items"],
            f"{location}[]",
            changes,
            seen,
        )

    for composition in ("allOf", "anyOf", "oneOf"):
        baseline_items = baseline.get(composition, [])
        current_items = current.get(composition, [])
        if not isinstance(baseline_items, list) or not isinstance(current_items, list):
            continue
        for index, (baseline_item, current_item) in enumerate(zip(baseline_items, current_items)):
            _compare_request_schema(
                baseline_document,
                current_document,
                baseline_item,
                current_item,
                f"{location}.{composition}[{index}]",
                changes,
                seen,
            )


def _compare_request_bodies(
    baseline_document: Mapping[str, Any],
    current_document: Mapping[str, Any],
    path: str,
    method: str,
    baseline_operation: Mapping[str, Any],
    current_operation: Mapping[str, Any],
    changes: List[str],
) -> None:
    """Compare request body requiredness, media types, and required fields.

    Args:
        baseline_document: Baseline OpenAPI document.
        current_document: Current OpenAPI document.
        path: API path.
        method: Lowercase HTTP method.
        baseline_operation: Baseline operation object.
        current_operation: Current operation object.
        changes: Mutable list receiving breaking-change messages.
    """
    baseline_body = _request_body(baseline_document, baseline_operation)
    current_body = _request_body(current_document, current_operation)
    operation_label = f"{method.upper()} {path}"
    baseline_required = bool(baseline_body and baseline_body.get("required"))
    current_required = bool(current_body and current_body.get("required"))
    if current_required and not baseline_required:
        changes.append(f"request body became required: {operation_label}")
    if baseline_body is None or current_body is None:
        return
    baseline_content = baseline_body.get("content", {})
    current_content = current_body.get("content", {})
    if not isinstance(baseline_content, Mapping) or not isinstance(current_content, Mapping):
        return
    for media_type in sorted(set(baseline_content) - set(current_content)):
        changes.append(f"request media type removed: {operation_label} [{media_type}]")
    for media_type in sorted(set(baseline_content) & set(current_content)):
        baseline_media = baseline_content[media_type]
        current_media = current_content[media_type]
        if not isinstance(baseline_media, Mapping) or not isinstance(current_media, Mapping):
            continue
        _compare_request_schema(
            baseline_document,
            current_document,
            baseline_media.get("schema"),
            current_media.get("schema"),
            f"{operation_label} request[{media_type}]",
            changes,
            set(),
        )


def find_breaking_changes(
    baseline_document: Mapping[str, Any],
    current_document: Mapping[str, Any],
) -> List[str]:
    """Return selected backward-incompatible OpenAPI changes.

    The enforced contract covers removed paths/methods, new or newly-required
    query/header/path/cookie parameters, request bodies becoming required,
    removed request media types, and newly required request fields.

    Args:
        baseline_document: Accepted OpenAPI baseline.
        current_document: Newly generated OpenAPI document.

    Returns:
        list[str]: Sorted, de-duplicated breaking-change descriptions.
    """
    changes: List[str] = []
    baseline_paths = baseline_document.get("paths", {})
    current_paths = current_document.get("paths", {})
    if not isinstance(baseline_paths, Mapping) or not isinstance(current_paths, Mapping):
        return ["invalid OpenAPI document: paths must be objects"]
    for path in sorted(set(baseline_paths) - set(current_paths)):
        changes.append(f"removed path: {path}")
    for path in sorted(set(baseline_paths) & set(current_paths)):
        baseline_path_item = baseline_paths[path]
        current_path_item = current_paths[path]
        if not isinstance(baseline_path_item, Mapping) or not isinstance(current_path_item, Mapping):
            continue
        baseline_methods = {key for key in baseline_path_item if key.lower() in HTTP_METHODS}
        current_methods = {key for key in current_path_item if key.lower() in HTTP_METHODS}
        for method in sorted(baseline_methods - current_methods):
            changes.append(f"removed method: {method.upper()} {path}")
        for method in sorted(baseline_methods & current_methods):
            baseline_operation = baseline_path_item[method]
            current_operation = current_path_item[method]
            if not isinstance(baseline_operation, Mapping) or not isinstance(current_operation, Mapping):
                continue
            baseline_parameters = _parameter_map(
                baseline_document, baseline_path_item, baseline_operation
            )
            current_parameters = _parameter_map(current_document, current_path_item, current_operation)
            for key, parameter in sorted(current_parameters.items()):
                if not bool(parameter.get("required")):
                    continue
                baseline_parameter = baseline_parameters.get(key)
                if baseline_parameter is None or not bool(baseline_parameter.get("required")):
                    location, name = key
                    changes.append(
                        f"required parameter added or tightened: {method.upper()} {path} [{location}:{name}]"
                    )
            _compare_request_bodies(
                baseline_document,
                current_document,
                path,
                method,
                baseline_operation,
                current_operation,
                changes,
            )
    return sorted(set(changes))


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Parse command-line options.

    Args:
        argv: Optional explicit argument sequence.

    Returns:
        argparse.Namespace: Parsed options.
    """
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current", type=Path, default=root / "docs" / "generated" / "openapi.json")
    parser.add_argument(
        "--baseline",
        type=Path,
        default=root / "docs" / "generated" / "openapi-baseline.json",
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="explicitly replace the accepted baseline with the current document",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Check the OpenAPI contract or explicitly update its baseline.

    Args:
        argv: Optional explicit argument sequence.

    Returns:
        int: Process exit code.
    """
    args = _parse_args(argv)
    if not args.current.exists():
        print(f"current OpenAPI file is missing: {args.current}", file=sys.stderr)
        return 2
    current = _load_json(args.current)
    if args.update_baseline:
        _write_json(args.baseline, current)
        print(f"OpenAPI baseline updated: {args.baseline}")
        return 0
    if not args.baseline.exists():
        print(
            f"OpenAPI baseline is missing: {args.baseline}; review current schema and run --update-baseline",
            file=sys.stderr,
        )
        return 2
    baseline = _load_json(args.baseline)
    changes = find_breaking_changes(baseline, current)
    if changes:
        print("OpenAPI breaking changes detected:", file=sys.stderr)
        for change in changes:
            print(f"- {change}", file=sys.stderr)
        return 1
    print("OpenAPI contract check: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
