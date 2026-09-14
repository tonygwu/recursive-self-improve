"""Detector-version scan observations, signal occurrences, and physical-line exposure.

``scan.scan_all`` is the producer. For each changed transcript it calls
:func:`record_scan` inside the scanner's own per-file transaction, and
:func:`record_scan_failure` in a separate transaction after a rollback. Neither
writer begins, commits, or rolls back a transaction.

The readers :func:`scan_history` and :func:`exposure_window` use only the
supplied Store. They never read transcripts, invoke a worker, migrate, or write.

The primary rate is ``100000 * signal_occurrences / eligible_physical_lines``
for one canonical project, one half-open UTC interval, and one compatibility
key. Different compatibility keys are never pooled.

Contract: docs/dashboard-parity/PARALLEL_WORK.md, version 1. Concrete record
fields, limits, and design choices: docs/dashboard-parity/SCAN_HANDOFF.md.
"""

from __future__ import annotations

import base64
import builtins
import dataclasses
import hashlib
import importlib
import importlib.metadata
import json
import os
import re
import sqlite3
import statistics
import types
from datetime import datetime, timezone
from pathlib import Path, PurePath

from .filter_incidents import (
    SIGNAL_CORRECTION,
    SIGNAL_FRICTION_LOOP,
    SIGNAL_FRUSTRATION,
    SIGNAL_INSTRUCTION_EDIT,
    SIGNAL_REPEATED_ERROR,
    SIGNAL_SELF_OBSERVATION,
    SIGNAL_STANDING_INSTRUCTION,
)
from .sources.base import (
    LINE_BLANK,
    LINE_CATEGORIES,
    LINE_ENCRYPTED,
    LINE_MALFORMED,
    LINE_UNKNOWN_TYPE,
)
from .store import utc_now_iso

MIGRATION = "0022_scan_observations"
LINK_MIGRATION = "0026_scan_incident_links"
CONTRACT_VERSION = 1
#: Bump when measurement semantics change in a way no manifested module shows.
SEMANTICS_VERSION = 1
MANIFEST_SCHEMA = "scan-manifest/1"
UNKNOWN_PREFIX = "unknown:"

SIGNALS = (
    SIGNAL_CORRECTION,
    SIGNAL_STANDING_INSTRUCTION,
    SIGNAL_FRUSTRATION,
    SIGNAL_REPEATED_ERROR,
    SIGNAL_FRICTION_LOOP,
    SIGNAL_INSTRUCTION_EDIT,
    SIGNAL_SELF_OBSERVATION,
)
KIND_SIGNAL = "signal"
KIND_ERROR_FINGERPRINT = "error_fingerprint"
OCCURRENCE_KINDS = (KIND_SIGNAL, KIND_ERROR_FINGERPRINT)

#: Line exclusion causes in precedence order. A line records the first cause
#: that applies; an empty exclusion means the line is eligible exposure.
EXCLUSION_CAUSES = (
    "denied",
    "blank",
    "malformed",
    "unknown_project",
    "invalid_time",
    "unknown_time",
)
TIME_STATUSES = ("known", "missing", "invalid")
DETECTOR_COVERAGES = ("full", "partial", "none")
LINK_KINDS = ("produced", "corroborated")
PROJECTIONS = ("complete", "unavailable")

SCAN_TABLES = (
    "scan_manifests",
    "scan_working_copies",
    "scan_observations",
    "scan_lines",
    "scan_occurrences",
    "scan_observation_occurrences",
    "scan_incident_links",
)

#: Config fields that change detection, eligibility, attribution, or caps.
#: Source paths and model settings are deliberately absent: they are
#: machine-specific or do not affect deterministic detection.
DETECTION_CONFIG_FIELDS = (
    "denylist_substrings",
    "max_incidents_per_signal_per_session",
    "correction_max_len",
    "repeated_error_min_in_session",
    "repeated_error_min_sessions",
    "friction_loop_min_cycles",
    "friction_loop_window_events",
    "instruction_edit_filenames",
    "project_identity_use_gh",
)

#: Detector evidence that distinguishes same-line detections. Counts that grow
#: with appended data (count, cycles) are excluded so identity stays stable.
IDENTITY_EVIDENCE_KEYS = ("fingerprint", "file", "patterns")

_DETECTOR_MODULES = (
    "self_improve.filter_incidents",
    "self_improve.redact",
    "self_improve.sources.base",
)
_MEASUREMENT_MODULES = (
    "self_improve.scan",
    "self_improve.scan_observations",
    "self_improve.project_identity",
    "self_improve.sources.base",
)
_PARSERS = frozenset(
    {
        ("self_improve.sources.claude_code", "ClaudeCodeSource"),
        ("self_improve.sources.codex", "CodexSource"),
    }
)


class ScanObservationError(Exception):
    """Invalid measurement input, corrupt stored evidence, or a missing schema."""


# ---------------------------------------------------------------------------
# Canonical identities.
# ---------------------------------------------------------------------------


def canonical_json(value) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def content_id(value) -> str:
    """SHA-256 of the strict canonical JSON encoding of ``value``."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def normalize_timestamp(value: object) -> str | None:
    """ISO-8601 UTC with microseconds, or None for a value that is not a zoned time.

    A naive time has no known offset and is not guessed to be UTC.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def transcript_identity(source: str, file_path: str) -> dict:
    """Transcript identity from the source and the canonical transcript path.

    Symlink aliases resolve to one path. A path that cannot be resolved uses a
    named lexical fallback. A moved file without retained identity evidence is
    a new transcript, not an inferred alias.
    """
    lexical = os.path.normpath(os.path.abspath(file_path))
    if os.path.exists(file_path):
        canonical, method = os.path.realpath(file_path), "realpath"
    else:
        canonical, method = lexical, "lexical_missing"
    return {
        "transcript_id": content_id([source, canonical]),
        "canonical_path": canonical,
        "path_method": method,
    }


def working_copy_identity(project_key: str, cwd: str) -> dict:
    """Working copy identity: canonical project plus normalized absolute cwd.

    A missing directory keeps its lexical path under a named method. Unrelated
    missing paths are never merged by basename.
    """
    lexical = os.path.normpath(os.path.abspath(cwd))
    if os.path.isdir(cwd):
        normalized, method = os.path.realpath(cwd), "realpath"
    else:
        normalized, method = lexical, "lexical_missing"
    return {
        "id": content_id([project_key, normalized]),
        "normalized_path": normalized,
        "normalization": method,
    }


def logical_session_key(source: str, session_id: str) -> str:
    """Logical session identity, or "" when the transcript recorded no session ID."""
    return content_id([source, session_id]) if session_id else ""


def line_key(transcript_id: str, line_no: int, line_sha256: str) -> str:
    return content_id([transcript_id, line_no, line_sha256])


def identity_evidence(detail: object) -> dict:
    """The stable, content-free subset of detector evidence used for identity."""
    if not isinstance(detail, dict):
        raise ScanObservationError(
            f"detector detail must be an object, got {type(detail).__name__}"
        )
    evidence = {key: detail[key] for key in IDENTITY_EVIDENCE_KEYS if key in detail}
    canonical_json(evidence)  # raises on values strict JSON cannot carry
    return evidence


def occurrence_discriminator(event_ordinal: int, evidence: dict) -> str:
    """Distinguish detections on one line by their position in that line and evidence.

    ``event_ordinal`` counts earlier events from the SAME physical line, so it
    is a function of the line bytes and parser version. It is never a
    session-wide event-list index, run ID, clock value, or score rank.
    """
    return content_id({"event_ordinal": event_ordinal, "evidence": evidence})


def occurrence_identity(
    *,
    transcript_id: str,
    compatibility_key: str,
    kind: str,
    signal_type: str,
    supporting_line_keys: list[str],
    trigger_line_key: str,
    discriminator: str,
) -> str:
    return content_id(
        [
            "scan-occurrence/1",
            transcript_id,
            compatibility_key,
            kind,
            signal_type,
            list(supporting_line_keys),
            trigger_line_key,
            discriminator,
        ]
    )


def classify_line(*, category: str, denied: bool, project_key: str, time_status: str) -> str:
    """The exclusion cause for one line, in EXCLUSION_CAUSES precedence; "" is eligible."""
    if denied:
        return "denied"
    if category == LINE_BLANK:
        return "blank"
    if category == LINE_MALFORMED:
        return "malformed"
    if not project_key:
        return "unknown_project"
    if time_status == "invalid":
        return "invalid_time"
    if time_status == "missing":
        return "unknown_time"
    return ""


def detector_coverage(category: str, exclusion: str) -> str:
    """How completely the detectors could read a line's content."""
    if exclusion in ("denied", "blank", "malformed"):
        return "none"
    if category in (LINE_UNKNOWN_TYPE, LINE_ENCRYPTED):
        return "partial"
    return "full"


# ---------------------------------------------------------------------------
# Versions.
# ---------------------------------------------------------------------------


def module_identity(module: types.ModuleType) -> dict:
    """Content identity of a loaded module, verified against the code in memory.

    The source file is compiled and executed in a fresh namespace, then compared
    with the loaded module's functions, classes, and constants. A disk edit
    after import therefore yields a named unknown identity instead of
    relabeling the code that is actually running. The digest covers source
    bytes only, never the path, so clones and wheel installs agree.
    """
    name = module.__name__
    path = getattr(module, "__file__", None)
    if not path:
        return {"module": name, "verified": False, "reason": "no_source_file"}
    try:
        source = Path(path).read_bytes()
    except OSError as exc:
        return {"module": name, "verified": False, "reason": f"source_unreadable:{type(exc).__name__}"}
    digest = hashlib.sha256(source).hexdigest()
    try:
        code = compile(source, path, "exec", dont_inherit=True)
        fresh: dict = {
            "__name__": name,
            "__package__": module.__package__,
            "__file__": path,
            "__builtins__": builtins,
        }
        exec(code, fresh)
    except Exception as exc:  # any failure means the source cannot vouch for memory
        return {
            "module": name,
            "sha256": digest,
            "verified": False,
            "reason": f"source_not_reproducible:{type(exc).__name__}",
        }
    mismatch = _namespace_mismatch(name, vars(module), fresh)
    if mismatch:
        return {
            "module": name,
            "sha256": digest,
            "verified": False,
            "reason": f"loaded_code_differs_from_source:{mismatch}",
        }
    return {"module": name, "sha256": digest, "verified": True}


_CLASS_SKIP = frozenset(
    {
        "__dict__",
        "__weakref__",
        "__doc__",
        "__module__",
        "__qualname__",
        "__firstlineno__",
        "__static_attributes__",
        "__annotations__",
        "__annotate__",
        "__annotate_func__",
        "__annotations_cache__",
        "__parameters__",
        "__orig_bases__",
        "__protocol_attrs__",
        "__non_callable_proto_members__",
        "__subclasshook__",
        "__abstractmethods__",
        "_abc_impl",
    }
)


def _namespace_mismatch(module_name: str, loaded: dict, fresh: dict) -> str:
    for name in sorted(set(loaded) | set(fresh)):
        if name.startswith("__") and name.endswith("__"):
            continue
        if name not in fresh and isinstance(loaded[name], types.ModuleType):
            continue  # a submodule attribute set by a later import
        if name not in loaded or name not in fresh:
            return name
        if not _same_value(module_name, loaded[name], fresh[name]):
            return name
    return ""


def _same_value(module_name: str, a, b) -> bool:
    if a is b:
        return True
    if isinstance(a, types.FunctionType) and isinstance(b, types.FunctionType):
        if a.__module__ != module_name or b.__module__ != module_name:
            return False
        module_file = getattr(importlib.import_module(module_name), "__file__", None)
        if a.__code__.co_filename != module_file and b.__code__.co_filename != module_file:
            # Generated by @dataclass from the fields and parameters, which
            # _same_class compares; its closure cells reference the class itself.
            return a.__code__ == b.__code__
        return (
            a.__code__ == b.__code__
            and _same_closure(a, b)
            and _same_value(module_name, a.__defaults__, b.__defaults__)
            and _same_value(module_name, a.__kwdefaults__, b.__kwdefaults__)
        )
    if isinstance(a, type) and isinstance(b, type):
        if a.__module__ != module_name or b.__module__ != module_name:
            return False
        return _same_class(module_name, a, b)
    if isinstance(a, (staticmethod, classmethod)) and type(a) is type(b):
        return _same_value(module_name, a.__func__, b.__func__)
    if isinstance(a, property) and isinstance(b, property):
        return all(
            _same_value(module_name, getattr(a, part), getattr(b, part))
            for part in ("fget", "fset", "fdel")
        )
    if isinstance(a, re.Pattern) and isinstance(b, re.Pattern):
        return (a.pattern, a.flags) == (b.pattern, b.flags)
    if type(a) is not type(b):
        return False
    if isinstance(a, (str, bytes, int, float, complex, bool, type(None), PurePath)):
        return a == b
    if isinstance(a, (frozenset, set)):
        return a == b
    if isinstance(a, (tuple, list)):
        return len(a) == len(b) and all(_same_value(module_name, x, y) for x, y in zip(a, b))
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_same_value(module_name, a[k], b[k]) for k in a)
    if isinstance(a, dataclasses.Field) and isinstance(b, dataclasses.Field):
        return (
            a.name == b.name
            and a.type == b.type
            and _same_value(module_name, a.default, b.default)
            and _same_value(module_name, a.default_factory, b.default_factory)
        )
    return False  # no comparison rule: the source cannot vouch for this value


def _same_closure(a: types.FunctionType, b: types.FunctionType) -> bool:
    """No closure, or only the ``__class__`` cell that zero-argument super() creates."""
    if a.__closure__ is None and b.__closure__ is None:
        return True
    if a.__code__.co_freevars != ("__class__",) or b.__code__.co_freevars != ("__class__",):
        return False
    owner_a, owner_b = a.__closure__[0].cell_contents, b.__closure__[0].cell_contents
    return isinstance(owner_a, type) and isinstance(owner_b, type) and (
        owner_a.__qualname__ == owner_b.__qualname__
    )


def _same_class(module_name: str, a: type, b: type) -> bool:
    if a.__qualname__ != b.__qualname__:
        return False
    if [base.__qualname__ for base in a.__bases__] != [base.__qualname__ for base in b.__bases__]:
        return False
    names = (set(vars(a)) | set(vars(b))) - _CLASS_SKIP
    for name in sorted(names):
        if name not in vars(a) or name not in vars(b):
            return False
        va, vb = vars(a)[name], vars(b)[name]
        if isinstance(va, (types.GetSetDescriptorType, types.MemberDescriptorType)):
            if type(va) is not type(vb):
                return False
            continue
        if name == "__dataclass_params__":
            if repr(va) != repr(vb):
                return False
            continue
        if name == "__dataclass_fields__":
            if va.keys() != vb.keys() or not all(
                _same_value(module_name, va[k], vb[k]) for k in va
            ):
                return False
            continue
        if not _same_value(module_name, va, vb):
            return False
    return True


def _callable_label(fn) -> str:
    module = getattr(fn, "__module__", type(fn).__module__)
    qualname = getattr(fn, "__qualname__", type(fn).__qualname__)
    return f"{module}.{qualname}"


def _component(modules: list[dict]) -> dict:
    unverified = [m for m in modules if not m["verified"]]
    if unverified:
        key = UNKNOWN_PREFIX + ",".join(f"{m['module']}:{m['reason']}" for m in unverified)
    else:
        key = content_id([[m["module"], m["sha256"]] for m in modules])
    return {"key": key, "modules": modules}


def _config_value(value):
    if isinstance(value, tuple):
        return [_config_value(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ScanObservationError(
        f"detection config value of type {type(value).__name__} has no manifest encoding"
    )


def _compatibility_key(manifest: dict) -> str:
    parts = [
        "scan-compatibility/1",
        manifest["semantics_version"],
        manifest["detector"]["key"],
        manifest["parser"]["key"],
        manifest["config"]["key"],
    ]
    key = content_id(parts)
    return key if manifest["identifiable"] else UNKNOWN_PREFIX + key


def _manifest_id(manifest: dict) -> str:
    return content_id({k: v for k, v in manifest.items() if k != "manifest_id"})


def detector_manifest(cfg, source, detect_fn) -> dict:
    """Content-addressed identity of the detector, parser, and settings a scan executes.

    Deterministic; no database writes, model calls, or network requests. A
    custom detector, custom source, or unverifiable module receives a named
    ``unknown:`` component key, and the whole compatibility key is then
    ``unknown:``-prefixed so readers never compute a comparable rate for it.
    """
    from . import filter_incidents

    if detect_fn is filter_incidents.detect:
        detector = _component([module_identity(importlib.import_module(n)) for n in _DETECTOR_MODULES])
        detector["implementation"] = _callable_label(detect_fn)
    else:
        detector = {
            "key": UNKNOWN_PREFIX + "custom_detect_fn",
            "implementation": _callable_label(detect_fn),
            "modules": [],
        }
    source_type = type(source)
    parser_ref = (source_type.__module__, source_type.__qualname__)
    # A verified class does not identify an overridden bound method or the
    # configuration the instance actually reads. Keep custom instances unknown.
    parser_issue = "custom_source"
    if parser_ref in _PARSERS and getattr(source_type, "supports_line_records", False) is True:
        runtime_fields = {"config", "parse_stats", "discover_stats", "last_discover_stats"}
        overrides = sorted(set(vars(source)) - runtime_fields)
        source_cfg = getattr(source, "config", None)
        if overrides:
            parser_issue = "source_instance_override:" + ",".join(overrides)
        elif source_cfg is None or any(
            getattr(source_cfg, field, object()) != getattr(cfg, field)
            for field in DETECTION_CONFIG_FIELDS
        ):
            parser_issue = "source_config_differs_from_manifest"
        else:
            parser_issue = ""
    if not parser_issue:
        # One parser identity covers BOTH sources, so a project mixing Claude and
        # Codex lines keeps one comparable rate. A change to either parser
        # therefore partitions all history, which is the conservative direction.
        names = list(dict.fromkeys((*sorted(m for m, _ in _PARSERS), *_MEASUREMENT_MODULES)))
        parser = _component([module_identity(importlib.import_module(n)) for n in names])
    else:
        parser = {"key": UNKNOWN_PREFIX + parser_issue, "modules": []}
    parser["implementation"] = ".".join(parser_ref)
    values = {name: _config_value(getattr(cfg, name)) for name in DETECTION_CONFIG_FIELDS}
    config = {"key": content_id(["scan-config/1", values]), "values": values}
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "semantics_version": SEMANTICS_VERSION,
        "detector": detector,
        "parser": parser,
        "config": config,
        "identifiable": not any(
            c["key"].startswith(UNKNOWN_PREFIX) for c in (detector, parser)
        ),
    }
    manifest["compatibility_key"] = _compatibility_key(manifest)
    manifest["manifest_id"] = _manifest_id(manifest)
    return manifest


def verify_manifest(manifest: object, owner: str) -> dict:
    """Recompute every derived key; raise naming ``owner`` on any disagreement."""
    required = {
        "schema", "semantics_version", "detector", "parser", "config",
        "identifiable", "compatibility_key", "manifest_id",
    }
    if not isinstance(manifest, dict) or set(manifest) != required:
        raise ScanObservationError(f"{owner}: manifest has the wrong shape")
    if manifest["schema"] != MANIFEST_SCHEMA:
        raise ScanObservationError(f"{owner}: unsupported manifest schema {manifest['schema']!r}")
    if type(manifest["semantics_version"]) is not int or manifest["semantics_version"] != 1:
        raise ScanObservationError(f"{owner}: unsupported manifest semantics version")
    try:
        canonical_json(manifest)
    except (TypeError, ValueError) as exc:
        raise ScanObservationError(f"{owner}: manifest is not strict JSON") from exc
    for part in ("detector", "parser", "config"):
        if not isinstance(manifest[part], dict) or not isinstance(manifest[part].get("key"), str):
            raise ScanObservationError(f"{owner}: manifest {part} has no key")
    config = manifest["config"]
    if not isinstance(config.get("values"), dict) or config["key"] != content_id(
        ["scan-config/1", config["values"]]
    ):
        raise ScanObservationError(f"{owner}: manifest config key does not match its values")
    for part in ("detector", "parser"):
        modules = manifest[part].get("modules")
        if not isinstance(modules, list):
            raise ScanObservationError(f"{owner}: manifest {part} modules are not a list")
        for module in modules:
            if (
                not isinstance(module, dict)
                or not {"module", "verified"} <= set(module) <= {"module", "verified", "sha256", "reason"}
                or not isinstance(module["module"], str) or not module["module"]
                or type(module["verified"]) is not bool
                or (module["verified"] and (
                    not isinstance(module.get("sha256"), str)
                    or re.fullmatch(r"[0-9a-f]{64}", module["sha256"]) is None
                ))
                or (not module["verified"] and (
                    not isinstance(module.get("reason"), str) or not module["reason"]
                ))
            ):
                raise ScanObservationError(f"{owner}: manifest {part} module has the wrong shape")
        if not modules and not manifest[part]["key"].startswith(UNKNOWN_PREFIX):
            raise ScanObservationError(f"{owner}: manifest {part} has no identified modules")
        if modules and manifest[part]["key"] != _component(modules)["key"]:
            raise ScanObservationError(f"{owner}: manifest {part} key does not match its modules")
    identifiable = not any(
        manifest[p]["key"].startswith(UNKNOWN_PREFIX) for p in ("detector", "parser")
    )
    if manifest["identifiable"] is not identifiable:
        raise ScanObservationError(f"{owner}: manifest identifiable flag is inconsistent")
    if manifest["compatibility_key"] != _compatibility_key(manifest):
        raise ScanObservationError(f"{owner}: manifest compatibility_key does not match its components")
    if manifest["manifest_id"] != _manifest_id(manifest):
        raise ScanObservationError(f"{owner}: manifest content does not match manifest_id")
    return manifest


def package_provenance() -> dict:
    """Installed package version: provenance beside the manifest, never a substitute."""
    try:
        return {"package_version": importlib.metadata.version("self-improve")}
    except importlib.metadata.PackageNotFoundError:
        return {"package_version": "", "package_version_unavailable": "PackageNotFoundError"}


# ---------------------------------------------------------------------------
# Schema.
# ---------------------------------------------------------------------------


def require_schema(store) -> None:
    """Raise unless 0022 is applied and every scan table is readable.

    An applied migration with a missing table is a named data error, never an
    empty history.
    """
    if not store.query_one("SELECT name FROM schema_migrations WHERE name = ?", (MIGRATION,)):
        raise ScanObservationError(
            f"scan observations require migration {MIGRATION}; upgrade the database explicitly"
        )
    for table in SCAN_TABLES:
        try:
            store.query(f"SELECT 1 FROM {table} LIMIT 1")
        except sqlite3.OperationalError as exc:
            raise ScanObservationError(
                f"migration {MIGRATION} is recorded but table {table} is unreadable: {exc}"
            ) from exc


# ---------------------------------------------------------------------------
# Writers.
# ---------------------------------------------------------------------------

_OBSERVATION_FIELDS = {
    "id": str,
    "run_id": str,
    "source": str,
    "session_file": str,
    "transcript_id": str,
    "canonical_path": str,
    "path_method": str,
    "manifest": dict,
    "projection": str,
    "file_size": int,
    "byte_end": int,
    "line_end": int,
    "pending_bytes": int,
    "observed_at": str,
    "counts": dict,
    "caps": dict,
    "coverage": dict,
    "provenance": dict,
}
_FAILURE_FIELDS = {
    key: _OBSERVATION_FIELDS[key]
    for key in (
        "id", "run_id", "source", "session_file", "transcript_id", "canonical_path",
        "path_method", "manifest", "file_size", "observed_at",
    )
}
_WORKING_COPY_FIELDS = {"id": str, "normalized_path": str, "normalization": str}
_LINE_FIELDS = {
    "line_no": int,
    "line_key": str,
    "sha256": str,
    "byte_start": int,
    "byte_end": int,
    "occurred_at": str,
    "time_status": str,
    "source": str,
    "project_key": str,
    "project_key_method": str,
    "working_copy": (dict, type(None)),
    "logical_session_key": str,
    "headless": bool,
    "is_subagent": bool,
    "category": str,
    "cause": str,
    "exclusion": str,
    "detector_coverage": str,
    "events": int,
}
_OCCURRENCE_FIELDS = {
    "occurrence_id": str,
    "compatibility_key": str,
    "kind": str,
    "signal_type": str,
    "trigger_line_key": str,
    "trigger_line_no": int,
    "supporting_line_keys": list,
    "discriminator": str,
    "occurred_at": str,
    "evidence": dict,
    "incident_links": list,
}
_LINK_FIELDS = {"incident_id": str, "link_kind": str}


def _check_fields(record: object, spec: dict, owner: str) -> None:
    if not isinstance(record, dict):
        raise ScanObservationError(f"{owner}: expected an object, got {type(record).__name__}")
    missing = sorted(set(spec) - set(record))
    unknown = sorted(set(record) - set(spec))
    if missing or unknown:
        raise ScanObservationError(f"{owner}: missing fields {missing}, unknown fields {unknown}")
    for key, expected in spec.items():
        value = record[key]
        allowed = expected if isinstance(expected, tuple) else (expected,)
        if (bool not in allowed and isinstance(value, bool)) or not isinstance(value, allowed):
            raise ScanObservationError(
                f"{owner}: field {key!r} must be {'/'.join(t.__name__ for t in allowed)}, "
                f"got {type(value).__name__}"
            )


def _validate_observation(observation: object, spec: dict) -> str:
    identifier = observation.get("id") if isinstance(observation, dict) else None
    owner = f"scan observation {identifier!r}"
    _check_fields(observation, spec, owner)
    if not observation["id"] or not observation["source"] or not observation["session_file"]:
        raise ScanObservationError(f"{owner}: id, source, and session_file must be non-empty")
    verify_manifest(observation["manifest"], owner)
    if observation["transcript_id"] != content_id(
        [observation["source"], observation["canonical_path"]]
    ):
        raise ScanObservationError(
            f"{owner}: transcript_id does not match its source and canonical_path"
        )
    if normalize_timestamp(observation["observed_at"]) != observation["observed_at"]:
        raise ScanObservationError(f"{owner}: observed_at must be ISO-UTC with microseconds")
    for key in ("file_size", "byte_end", "line_end", "pending_bytes"):
        if key in observation and observation[key] < 0:
            raise ScanObservationError(f"{owner}: {key} must not be negative")
    if "projection" in observation and observation["projection"] not in PROJECTIONS:
        raise ScanObservationError(f"{owner}: projection must be one of {PROJECTIONS}")
    return owner


def _validate_lines(observation: dict, lines: object, owner: str) -> dict[str, dict]:
    if not isinstance(lines, list):
        raise ScanObservationError(f"{owner}: lines must be a list")
    if observation["projection"] == "unavailable":
        if lines or observation["line_end"] or observation["byte_end"]:
            raise ScanObservationError(
                f"{owner}: an unavailable projection carries no lines or byte range"
            )
        return {}
    if len(lines) != observation["line_end"]:
        raise ScanObservationError(
            f"{owner}: a complete projection needs lines 1..{observation['line_end']}, "
            f"got {len(lines)}"
        )
    transcript_id = observation["transcript_id"]
    by_key: dict[str, dict] = {}
    expected_start = 0
    for index, line in enumerate(lines, start=1):
        line_owner = f"{owner} line {index}"
        _check_fields(line, _LINE_FIELDS, line_owner)
        if line["line_no"] != index:
            raise ScanObservationError(f"{line_owner}: lines must be the complete ordered file")
        if line["byte_start"] != expected_start or line["byte_end"] <= line["byte_start"]:
            raise ScanObservationError(f"{line_owner}: byte range is not contiguous")
        expected_start = line["byte_end"]
        if line["line_key"] != line_key(transcript_id, index, line["sha256"]):
            raise ScanObservationError(f"{line_owner}: line_key does not bind transcript, position, and digest")
        if line["source"] != observation["source"]:
            raise ScanObservationError(f"{line_owner}: source differs from its observation")
        if line["category"] not in LINE_CATEGORIES:
            raise ScanObservationError(f"{line_owner}: unknown category {line['category']!r}")
        if line["time_status"] not in TIME_STATUSES:
            raise ScanObservationError(f"{line_owner}: unknown time_status {line['time_status']!r}")
        if line["exclusion"] not in ("", *EXCLUSION_CAUSES):
            raise ScanObservationError(f"{line_owner}: unknown exclusion {line['exclusion']!r}")
        denied = line["exclusion"] == "denied"
        if denied:
            if (
                line["project_key"]
                or line["project_key_method"]
                or line["logical_session_key"]
                or line["occurred_at"]
            ):
                raise ScanObservationError(f"{line_owner}: a denied line must retain no attribution")
        elif line["time_status"] == "known":
            if normalize_timestamp(line["occurred_at"]) != line["occurred_at"]:
                raise ScanObservationError(f"{line_owner}: occurred_at is not normalized ISO-UTC")
        elif line["occurred_at"]:
            raise ScanObservationError(f"{line_owner}: occurred_at is set without a known time")
        expected = classify_line(
            category=line["category"],
            denied=denied,
            project_key=line["project_key"],
            time_status=line["time_status"],
        )
        if line["exclusion"] != expected:
            raise ScanObservationError(
                f"{line_owner}: exclusion {line['exclusion']!r} contradicts its fields "
                f"(expected {expected!r})"
            )
        if line["detector_coverage"] != detector_coverage(line["category"], line["exclusion"]):
            raise ScanObservationError(f"{line_owner}: detector_coverage contradicts its category")
        working_copy = line["working_copy"]
        if line["project_key"]:
            _check_fields(working_copy, _WORKING_COPY_FIELDS, f"{line_owner} working_copy")
            if working_copy["id"] != content_id([line["project_key"], working_copy["normalized_path"]]):
                raise ScanObservationError(f"{line_owner}: working_copy id does not match its path")
        elif working_copy is not None:
            raise ScanObservationError(f"{line_owner}: a working copy requires a project")
        if line["events"] < 0:
            raise ScanObservationError(f"{line_owner}: events must not be negative")
        by_key[line["line_key"]] = line
    if expected_start != observation["byte_end"]:
        raise ScanObservationError(f"{owner}: lines end at byte {expected_start}, not byte_end")
    return by_key


def _validate_occurrences(
    store, observation: dict, lines_by_key: dict, occurrences: object, owner: str
) -> None:
    if not isinstance(occurrences, list):
        raise ScanObservationError(f"{owner}: occurrences must be a list")
    if observation["projection"] == "unavailable" and occurrences:
        raise ScanObservationError(f"{owner}: an unavailable projection carries no occurrences")
    compatibility = observation["manifest"]["compatibility_key"]
    seen: set[str] = set()
    linked_pairs: set[tuple[str, str]] = set()
    incident_kinds: dict[str, str] = {}
    for index, occurrence in enumerate(occurrences):
        name = occurrence.get("occurrence_id") if isinstance(occurrence, dict) else index
        occ_owner = f"{owner} occurrence {name!r}"
        _check_fields(occurrence, _OCCURRENCE_FIELDS, occ_owner)
        if occurrence["kind"] not in OCCURRENCE_KINDS:
            raise ScanObservationError(f"{occ_owner}: unknown kind {occurrence['kind']!r}")
        if occurrence["signal_type"] not in SIGNALS:
            raise ScanObservationError(f"{occ_owner}: unknown signal {occurrence['signal_type']!r}")
        if occurrence["compatibility_key"] != compatibility:
            raise ScanObservationError(f"{occ_owner}: compatibility_key differs from its observation")
        trigger = lines_by_key.get(occurrence["trigger_line_key"])
        if trigger is None or trigger["line_no"] != occurrence["trigger_line_no"]:
            raise ScanObservationError(f"{occ_owner}: trigger line is not in this observation")
        support = occurrence["supporting_line_keys"]
        support_lines = [lines_by_key.get(key) if isinstance(key, str) else None for key in support]
        if not support or any(line is None for line in support_lines):
            raise ScanObservationError(f"{occ_owner}: every supporting line must be in this observation")
        numbers = [line["line_no"] for line in support_lines]
        if numbers != sorted(set(numbers)) or occurrence["trigger_line_key"] not in support:
            raise ScanObservationError(
                f"{occ_owner}: supporting lines must be ordered, distinct, and include the trigger"
            )
        if occurrence["occurred_at"] != trigger["occurred_at"]:
            raise ScanObservationError(f"{occ_owner}: occurred_at must be the trigger line's timestamp")
        if identity_evidence(occurrence["evidence"]) != occurrence["evidence"]:
            raise ScanObservationError(f"{occ_owner}: evidence carries non-identity fields")
        expected = occurrence_identity(
            transcript_id=observation["transcript_id"],
            compatibility_key=compatibility,
            kind=occurrence["kind"],
            signal_type=occurrence["signal_type"],
            supporting_line_keys=support,
            trigger_line_key=occurrence["trigger_line_key"],
            discriminator=occurrence["discriminator"],
        )
        if occurrence["occurrence_id"] != expected:
            raise ScanObservationError(f"{occ_owner}: occurrence_id does not match its identity record")
        if expected in seen:
            raise ScanObservationError(f"{occ_owner}: duplicate occurrence in one observation")
        seen.add(expected)
        for link in occurrence["incident_links"]:
            _check_fields(link, _LINK_FIELDS, f"{occ_owner} incident link")
            if occurrence["kind"] != KIND_SIGNAL:
                raise ScanObservationError(f"{occ_owner}: only signal occurrences link to incidents")
            if link["link_kind"] not in LINK_KINDS:
                raise ScanObservationError(f"{occ_owner}: unknown link_kind {link['link_kind']!r}")
            pair = (link["incident_id"], expected)
            if pair in linked_pairs:
                raise ScanObservationError(
                    f"{occ_owner}: incident {link['incident_id']} has a duplicate occurrence link"
                )
            linked_pairs.add(pair)
            if incident_kinds.setdefault(link["incident_id"], link["link_kind"]) != link["link_kind"]:
                raise ScanObservationError(f"{occ_owner}: incident links disagree on production history")
            incident = store.query_one(
                "SELECT session_file, signal_type, run_id FROM incidents WHERE id = ?",
                (link["incident_id"],),
            )
            if incident is None:
                raise ScanObservationError(f"{occ_owner}: linked incident {link['incident_id']} does not exist")
            if (
                incident["session_file"] != observation["session_file"]
                or incident["signal_type"] != occurrence["signal_type"]
            ):
                raise ScanObservationError(
                    f"{occ_owner}: linked incident {link['incident_id']} has a different file or signal"
                )
            if link["link_kind"] == "produced" and incident["run_id"] != observation["run_id"]:
                raise ScanObservationError(
                    f"{occ_owner}: incident {link['incident_id']} was not produced by run "
                    f"{observation['run_id']}"
                )
            if link["link_kind"] == "produced" and store.query_one(
                "SELECT 1 FROM scan_incident_links WHERE incident_id = ? "
                "AND link_kind = 'produced' AND observation_id <> ?",
                (link["incident_id"], observation["id"]),
            ):
                raise ScanObservationError(f"{occ_owner}: incident already has a producing observation")


def _require_write_transaction(store, owner: str) -> None:
    if getattr(store, "read_only", False):
        raise ScanObservationError(f"{owner}: a read-only Store cannot record scan observations")
    if not store.conn.in_transaction:
        raise ScanObservationError(
            f"{owner}: requires the caller's active write transaction; "
            "this writer never begins, commits, or rolls back one"
        )


def _content_hash(observation: dict, lines: list, occurrences: list) -> str:
    digest = hashlib.sha256()
    digest.update(canonical_json(observation).encode("utf-8"))
    for line in lines:
        digest.update(b"\nL")
        digest.update(canonical_json(line).encode("utf-8"))
    for occurrence in occurrences:
        digest.update(b"\nO")
        digest.update(canonical_json(occurrence).encode("utf-8"))
    return digest.hexdigest()


def _already_recorded(store, observation_id: str, content_hash: str) -> bool:
    row = store.query_one(
        "SELECT content_hash, run_id FROM scan_observations WHERE id = ?", (observation_id,)
    )
    if row is None:
        return False
    if row["content_hash"] == content_hash:
        return True
    raise ScanObservationError(
        f"scan observation id {observation_id} is already owned by run {row['run_id']} "
        "with different content"
    )


def _store_manifest(store, manifest: dict, now: str) -> None:
    text = canonical_json(manifest)
    row = store.query_one(
        "SELECT manifest_json FROM scan_manifests WHERE id = ?", (manifest["manifest_id"],)
    )
    if row is not None:
        if row["manifest_json"] != text:
            raise ScanObservationError(
                f"scan manifest {manifest['manifest_id']}: stored content differs from its identity"
            )
        return
    store.insert(
        "scan_manifests",
        {
            "id": manifest["manifest_id"],
            "compatibility_key": manifest["compatibility_key"],
            "detector_key": manifest["detector"]["key"],
            "parser_key": manifest["parser"]["key"],
            "config_key": manifest["config"]["key"],
            "semantics_version": manifest["semantics_version"],
            "identifiable": 1 if manifest["identifiable"] else 0,
            "manifest_json": text,
            "created_at": now,
        },
    )


def _line_revision(compatibility_key: str, line: dict) -> str:
    return content_id(["scan-line-revision/1", compatibility_key, line])


def _occurrence_attributes(trigger: dict) -> dict:
    working_copy = trigger["working_copy"]
    return {
        "source": trigger["source"],
        "project_key": trigger["project_key"],
        "working_copy_id": working_copy["id"] if working_copy else "",
        "logical_session_key": trigger["logical_session_key"],
        "headless": 1 if trigger["headless"] else 0,
        "is_subagent": 1 if trigger["is_subagent"] else 0,
        "exclusion": trigger["exclusion"],
    }


def _plan(active_rows: dict, new_items: list[tuple[object, str, dict]]) -> tuple[list, list, dict]:
    """Diff new revisions against active rows keyed identically.

    ``new_items`` holds (key, revision_hash, payload). Returns rows to insert,
    active row ids to supersede, and counts. Keys absent from the new set are
    superseded as removed.
    """
    inserts, supersede = [], []
    unchanged = 0
    for key, revision, payload in new_items:
        current = active_rows.pop(key, None)
        if current is not None and current["revision_hash"] == revision:
            unchanged += 1
            continue
        if current is not None:
            supersede.append(current["id"])
        inserts.append((revision, payload))
    removed = [row["id"] for row in active_rows.values()]
    counts = {
        "inserted": len(inserts),
        "superseded": len(supersede) + len(removed),
        "unchanged": unchanged,
        "removed": len(removed),
    }
    return inserts, supersede + removed, counts


def _summary(lines: list, occurrences: list) -> dict:
    by_category: dict[str, int] = {}
    by_exclusion: dict[str, int] = {}
    by_signal: dict[str, int] = {}
    partial = eligible = diagnostics = 0
    for line in lines:
        by_category[line["category"]] = by_category.get(line["category"], 0) + 1
        if line["exclusion"]:
            by_exclusion[line["exclusion"]] = by_exclusion.get(line["exclusion"], 0) + 1
        else:
            eligible += 1
            partial += line["detector_coverage"] == "partial"
    for occurrence in occurrences:
        if occurrence["kind"] == KIND_SIGNAL:
            by_signal[occurrence["signal_type"]] = by_signal.get(occurrence["signal_type"], 0) + 1
        else:
            diagnostics += 1
    return {
        "lines_observed": len(lines),
        "eligible_lines": eligible,
        "lines_by_category": by_category,
        "excluded_lines_by_cause": by_exclusion,
        "partial_detector_coverage_lines": partial,
        "signal_occurrences_by_signal": by_signal,
        "error_fingerprint_occurrences": diagnostics,
    }


def record_scan(store, *, observation: dict, lines: list, occurrences: list) -> str:
    """Validate and publish one complete scan observation and its active projections.

    Runs inside the caller's per-file write transaction and never starts,
    commits, or rolls back. Replaying an identical observation is a no-op;
    reusing its ID with changed content raises naming the owning run.
    """
    owner = _validate_observation(observation, _OBSERVATION_FIELDS)
    _require_write_transaction(store, owner)
    require_schema(store)
    if not store.query_one("SELECT name FROM schema_migrations WHERE name = ?", (LINK_MIGRATION,)):
        raise ScanObservationError(f"{owner}: scan writes require migration {LINK_MIGRATION}; upgrade explicitly")
    lines_by_key = _validate_lines(observation, lines, owner)
    _validate_occurrences(store, observation, lines_by_key, occurrences, owner)
    content_hash = _content_hash(observation, lines, occurrences)
    if _already_recorded(store, observation["id"], content_hash):
        return observation["id"]

    now = utc_now_iso()
    manifest = observation["manifest"]
    compatibility = manifest["compatibility_key"]
    transcript_id = observation["transcript_id"]
    observation_id = observation["id"]
    complete = observation["projection"] == "complete"
    reconciliation: dict = {}

    line_inserts = line_supersede = occ_inserts = occ_supersede = []
    if complete:
        active_lines = {
            row["line_no"]: row
            for row in store.query(
                "SELECT id, line_no, revision_hash FROM scan_lines "
                "WHERE transcript_id = ? AND compatibility_key = ? AND active = 1",
                (transcript_id, compatibility),
            )
        }
        line_inserts, line_supersede, reconciliation["lines"] = _plan(
            active_lines,
            [(line["line_no"], _line_revision(compatibility, line), line) for line in lines],
        )
        active_occurrences = {
            row["occurrence_id"]: row
            for row in store.query(
                "SELECT id, occurrence_id, revision_hash FROM scan_occurrences "
                "WHERE transcript_id = ? AND compatibility_key = ? AND active = 1",
                (transcript_id, compatibility),
            )
        }
        planned = []
        for occurrence in occurrences:
            attributes = _occurrence_attributes(lines_by_key[occurrence["trigger_line_key"]])
            identity = {k: v for k, v in occurrence.items() if k != "incident_links"}
            revision = content_id(["scan-occurrence-revision/1", identity, attributes])
            planned.append((occurrence["occurrence_id"], revision, (occurrence, attributes)))
        occ_inserts, occ_supersede, reconciliation["occurrences"] = _plan(active_occurrences, planned)

    known_times = [line["occurred_at"] for line in lines if line["occurred_at"]]
    record = {
        "summary": _summary(lines, occurrences),
        "reconciliation": reconciliation,
        "incident_links": sum(len(o["incident_links"]) for o in occurrences),
        "counts": observation["counts"],
        "caps": observation["caps"],
        "coverage": observation["coverage"],
        "provenance": observation["provenance"],
    }
    _store_manifest(store, manifest, now)
    store.insert(
        "scan_observations",
        {
            "id": observation_id,
            "content_hash": content_hash,
            "run_id": observation["run_id"],
            "source": observation["source"],
            "session_file": observation["session_file"],
            "transcript_id": transcript_id,
            "canonical_path": observation["canonical_path"],
            "path_method": observation["path_method"],
            "manifest_id": manifest["manifest_id"],
            "compatibility_key": compatibility,
            "outcome": "succeeded",
            "failure_cause": "",
            "projection": observation["projection"],
            "file_size": observation["file_size"],
            "byte_end": observation["byte_end"],
            "line_end": observation["line_end"],
            "pending_bytes": observation["pending_bytes"],
            "first_occurred_at": min(known_times, default=""),
            "last_occurred_at": max(known_times, default=""),
            "record_json": canonical_json(record),
            "observed_at": observation["observed_at"],
        },
    )
    if not complete:
        return observation_id

    conn = store.conn
    if line_supersede:
        conn.executemany(
            "UPDATE scan_lines SET active = 0, superseded_by = ? WHERE id = ?",
            [(observation_id, row_id) for row_id in line_supersede],
        )
    working_copies = {}
    for _, line in line_inserts:
        if line["working_copy"]:
            working_copies[line["working_copy"]["id"]] = (line["project_key"], line["working_copy"])
    conn.executemany(
        "INSERT INTO scan_working_copies (id, project_key, normalized_path, normalization, created_at) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT (id) DO NOTHING",
        [
            (wc_id, project_key, wc["normalized_path"], wc["normalization"], now)
            for wc_id, (project_key, wc) in working_copies.items()
        ],
    )
    conn.executemany(
        "INSERT INTO scan_lines (id, revision_hash, transcript_id, compatibility_key, line_no, "
        " line_key, line_sha256, byte_start, byte_end, occurred_at, time_status, source, "
        " project_key, project_key_method, working_copy_id, logical_session_key, headless, "
        " is_subagent, category, cause, exclusion, detector_coverage, events, observation_id, "
        " superseded_by, active) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', 1)",
        [
            (
                content_id([revision, observation_id]),
                revision,
                transcript_id,
                compatibility,
                line["line_no"],
                line["line_key"],
                line["sha256"],
                line["byte_start"],
                line["byte_end"],
                line["occurred_at"],
                line["time_status"],
                line["source"],
                line["project_key"],
                line["project_key_method"],
                line["working_copy"]["id"] if line["working_copy"] else "",
                line["logical_session_key"],
                1 if line["headless"] else 0,
                1 if line["is_subagent"] else 0,
                line["category"],
                line["cause"],
                line["exclusion"],
                line["detector_coverage"],
                line["events"],
                observation_id,
            )
            for revision, line in line_inserts
        ],
    )
    if occ_supersede:
        conn.executemany(
            "UPDATE scan_occurrences SET active = 0, superseded_by = ? WHERE id = ?",
            [(observation_id, row_id) for row_id in occ_supersede],
        )
    conn.executemany(
        "INSERT INTO scan_occurrences (id, revision_hash, occurrence_id, transcript_id, "
        " compatibility_key, kind, signal_type, trigger_line_key, trigger_line_no, "
        " supporting_lines_json, discriminator, occurred_at, source, project_key, "
        " working_copy_id, logical_session_key, headless, is_subagent, exclusion, "
        " evidence_json, observation_id, superseded_by, active) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', 1)",
        [
            (
                content_id([revision, observation_id]),
                revision,
                occurrence["occurrence_id"],
                transcript_id,
                compatibility,
                occurrence["kind"],
                occurrence["signal_type"],
                occurrence["trigger_line_key"],
                occurrence["trigger_line_no"],
                canonical_json(occurrence["supporting_line_keys"]),
                occurrence["discriminator"],
                occurrence["occurred_at"],
                attributes["source"],
                attributes["project_key"],
                attributes["working_copy_id"],
                attributes["logical_session_key"],
                attributes["headless"],
                attributes["is_subagent"],
                attributes["exclusion"],
                canonical_json(occurrence["evidence"]),
                observation_id,
            )
            for revision, (occurrence, attributes) in occ_inserts
        ],
    )
    conn.executemany(
        "INSERT INTO scan_observation_occurrences (observation_id, occurrence_id) VALUES (?, ?)",
        [(observation_id, occurrence["occurrence_id"]) for occurrence in occurrences],
    )
    conn.executemany(
        "INSERT INTO scan_incident_links (id, occurrence_id, incident_id, observation_id, "
        " link_kind, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (
                content_id([occurrence["occurrence_id"], link["incident_id"], observation_id]),
                occurrence["occurrence_id"],
                link["incident_id"],
                observation_id,
                link["link_kind"],
                now,
            )
            for occurrence in occurrences
            for link in occurrence["incident_links"]
        ],
    )
    return observation_id


def record_scan_failure(store, *, observation: dict, cause: str) -> str:
    """Record a failed scan attempt without touching any active projection.

    The previous projection stays readable; readers report its coverage as
    stale while this failure is the transcript's latest observation. Same
    transaction rule as :func:`record_scan`: the caller owns it.
    """
    owner = _validate_observation(observation, _FAILURE_FIELDS)
    if not isinstance(cause, str) or not cause.strip():
        raise ScanObservationError(f"{owner}: a failure needs a non-empty cause")
    _require_write_transaction(store, owner)
    require_schema(store)
    content_hash = content_id(["scan-failure/1", observation, cause])
    if _already_recorded(store, observation["id"], content_hash):
        return observation["id"]
    now = utc_now_iso()
    manifest = observation["manifest"]
    _store_manifest(store, manifest, now)
    store.insert(
        "scan_observations",
        {
            "id": observation["id"],
            "content_hash": content_hash,
            "run_id": observation["run_id"],
            "source": observation["source"],
            "session_file": observation["session_file"],
            "transcript_id": observation["transcript_id"],
            "canonical_path": observation["canonical_path"],
            "path_method": observation["path_method"],
            "manifest_id": manifest["manifest_id"],
            "compatibility_key": manifest["compatibility_key"],
            "outcome": "failed",
            "failure_cause": cause,
            "projection": "",
            "file_size": observation["file_size"],
            "record_json": canonical_json(
                {"coverage": {"stale_projection": True, "failure_cause": cause}}
            ),
            "observed_at": observation["observed_at"],
        },
    )
    return observation["id"]


# ---------------------------------------------------------------------------
# Readers.
# ---------------------------------------------------------------------------


def _load_json_object(raw: object, owner: str) -> dict:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ScanObservationError(f"{owner} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ScanObservationError(f"{owner} is not a JSON object")
    try:
        canonical_json(value)
    except (TypeError, ValueError) as exc:
        raise ScanObservationError(f"{owner} is not strict JSON") from exc
    return value


def _load_manifest(store, manifest_id: str, owner: str, cache: dict) -> dict:
    if manifest_id in cache:
        return cache[manifest_id]
    row = store.query_one(
        "SELECT * FROM scan_manifests WHERE id = ?",
        (manifest_id,),
    )
    if row is None:
        raise ScanObservationError(f"{owner}: manifest {manifest_id} is missing")
    manifest_owner = f"scan manifest {manifest_id}"
    manifest = verify_manifest(_load_json_object(row["manifest_json"], manifest_owner), manifest_owner)
    if any(row[column] != expected for column, expected in {
        "id": manifest["manifest_id"], "compatibility_key": manifest["compatibility_key"],
        "detector_key": manifest["detector"]["key"], "parser_key": manifest["parser"]["key"],
        "config_key": manifest["config"]["key"], "semantics_version": manifest["semantics_version"],
        "identifiable": int(manifest["identifiable"]),
    }.items()):
        raise ScanObservationError(f"{manifest_owner}: stored columns differ from its content")
    cache[manifest_id] = manifest
    return manifest


_HISTORY_COLUMNS = (
    "id", "run_id", "source", "session_file", "transcript_id", "canonical_path", "path_method",
    "compatibility_key", "outcome", "failure_cause", "projection", "file_size", "byte_end",
    "line_end", "pending_bytes", "first_occurred_at", "last_occurred_at", "observed_at",
)


def _history_record(store, row: dict, cache: dict) -> dict:
    owner = f"scan observation {row['id']}"
    manifest = _load_manifest(store, row["manifest_id"], owner, cache)
    if manifest["compatibility_key"] != row["compatibility_key"]:
        raise ScanObservationError(f"{owner}: compatibility_key differs from its manifest")
    record = {column: row[column] for column in _HISTORY_COLUMNS}
    record["manifest"] = manifest
    record["record"] = _observation_record(row)
    return record


def _observation_record(row: dict) -> dict:
    owner = f"scan observation {row['id']}"
    record = _load_json_object(row["record_json"], f"{owner} record_json")
    coverage = record.get("coverage")
    if not isinstance(coverage, dict):
        raise ScanObservationError(f"{owner}: coverage has the wrong shape")
    if row["outcome"] == "succeeded":
        causes = coverage.get("incomplete_causes")
        if not isinstance(causes, list) or not all(isinstance(c, str) for c in causes):
            raise ScanObservationError(f"{owner}: coverage.incomplete_causes has the wrong shape")
    return record


def _encode_cursor(selector_digest: str, row: dict) -> str:
    raw = canonical_json(
        {"v": 1, "selector": selector_digest, "observed_at": row["observed_at"], "id": row["id"]}
    )
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: object, selector_digest: str) -> tuple[str, str]:
    if not isinstance(cursor, str) or not cursor:
        raise ValueError("scan_history cursor must be a non-empty string")
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise ValueError("scan_history cursor is not a valid cursor") from exc
    if (
        not isinstance(data, dict)
        or set(data) != {"v", "selector", "observed_at", "id"}
        or data["v"] != 1
        or not all(isinstance(data[k], str) for k in ("selector", "observed_at", "id"))
    ):
        raise ValueError("scan_history cursor has the wrong shape")
    if data["selector"] != selector_digest:
        raise ValueError("scan_history cursor belongs to a different selector")
    return data["observed_at"], data["id"]


def scan_history(
    store,
    *,
    session_file: str | None = None,
    incident_id: str | None = None,
    run_id: str | None = None,
    limit: int = 20,
    cursor: str | None = None,
) -> dict:
    """Scan observations for one transcript file, incident, or exact run, newest first.

    Pages by ``(observed_at, id)`` descending. Incident history labels each
    record ``produced`` (the observation whose run created the incident) or
    ``corroborated`` (a later rescan). An incident with no links has legacy
    unknown provenance and is reported as such, not as an empty success.
    """
    selectors = {"session_file": session_file, "incident_id": incident_id, "run_id": run_id}
    if sum(value is not None for value in selectors.values()) != 1:
        raise ValueError("scan_history requires exactly one selector: session_file, incident_id or run_id")
    for name, value in selectors.items():
        if value is not None and (not isinstance(value, str) or not value):
            raise ValueError(f"scan_history {name} must be a non-empty string")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("scan_history limit must be an integer from 1 through 100")
    selector = {name: value for name, value in selectors.items() if value is not None}
    digest = content_id(["scan-history/1", selector])
    position = _decode_cursor(cursor, digest) if cursor is not None else None
    require_schema(store)

    page_sql, page_params = "", ()
    if position is not None:
        page_sql = " AND (o.observed_at < ? OR (o.observed_at = ? AND o.id < ?))"
        page_params = (position[0], position[0], position[1])
    response: dict = {"selector": selector}
    if session_file is not None or run_id is not None:
        column, value = ("session_file", session_file) if session_file is not None else ("run_id", run_id)
        response["count"] = store.query_one(
            f"SELECT COUNT(*) AS n FROM scan_observations WHERE {column} = ?", (value,)
        )["n"]
        rows = store.query(
            f"SELECT o.* FROM scan_observations o WHERE o.{column} = ?"
            + page_sql
            + " ORDER BY o.observed_at DESC, o.id DESC LIMIT ?",
            (value, *page_params, limit + 1),
        )
        empty_reason = "no_observations"
    else:
        incident = store.query_one(
            "SELECT id, session_file, signal_type, run_id FROM incidents WHERE id = ?", (incident_id,)
        )
        if incident is None:
            return {**response, "records": [], "next_cursor": None, "computable": False,
                    "reason": "incident_not_found", "count": 0}
        producing = store.query(
            "SELECT DISTINCT l.observation_id, o.run_id FROM scan_incident_links l "
            "LEFT JOIN scan_observations o ON o.id = l.observation_id "
            "WHERE l.incident_id = ? AND l.link_kind = 'produced' LIMIT 2",
            (incident_id,),
        )
        if len(producing) > 1:
            raise ScanObservationError(f"incident {incident_id}: multiple producing scan observations")
        produced = producing[0] if producing else None
        if produced and produced["run_id"] != incident["run_id"]:
            raise ScanObservationError(
                f"incident {incident_id}: producing scan observation belongs to a different run or is missing"
            )
        linked = store.query_one(
            "SELECT 1 AS n FROM scan_incident_links WHERE incident_id = ?", (incident_id,)
        )
        response["incident"] = {
            "id": incident["id"],
            "run_id": incident["run_id"],
            "produced_by_observation": produced["observation_id"] if produced else None,
            "provenance": (
                "observed" if produced else "legacy_unknown_corroborated" if linked else "legacy_unknown"
            ),
        }
        response["count"] = store.query_one(
            "SELECT COUNT(*) AS n FROM scan_observations o WHERE EXISTS "
            "(SELECT 1 FROM scan_incident_links l WHERE l.observation_id = o.id AND l.incident_id = ?)",
            (incident_id,),
        )["n"]
        rows = store.query(
            "SELECT o.* FROM scan_observations o WHERE EXISTS "
            "(SELECT 1 FROM scan_incident_links l WHERE l.observation_id = o.id AND l.incident_id = ?)"
            + page_sql
            + " ORDER BY o.observed_at DESC, o.id DESC LIMIT ?",
            (incident_id, *page_params, limit + 1),
        )
        empty_reason = "legacy_unknown_provenance"

    cache: dict = {}
    page_links: dict[str, list] = {}
    if incident_id is not None and rows:
        ids = [row["id"] for row in rows[:limit]]
        for link in store.query(
            "SELECT observation_id, occurrence_id, link_kind FROM scan_incident_links "
            f"WHERE incident_id = ? AND observation_id IN ({','.join('?' for _ in ids)}) "
            "ORDER BY observation_id, occurrence_id",
            (incident_id, *ids),
        ):
            page_links.setdefault(link["observation_id"], []).append(link)
    records = []
    for row in rows[:limit]:
        record = _history_record(store, row, cache)
        if incident_id is not None:
            links = page_links[row["id"]]
            kinds = {link["link_kind"] for link in links}
            if len(kinds) != 1:
                raise ScanObservationError(f"scan observation {row['id']}: incident link kinds disagree")
            record["link_kind"] = next(iter(kinds))
            record["occurrence_ids"] = [link["occurrence_id"] for link in links]
            record["occurrence_id"] = links[0]["occurrence_id"] if len(links) == 1 else None
        records.append(record)
    next_cursor = _encode_cursor(digest, rows[limit - 1]) if len(rows) > limit else None
    if not records and position is None:
        return {**response, "records": [], "next_cursor": None, "computable": False,
                "reason": empty_reason}
    return {**response, "records": records, "next_cursor": next_cursor, "computable": True,
            "reason": ""}


def _count_map(rows: list[dict], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        out[str(row[key])] = out.get(str(row[key]), 0) + int(row["n"])
    return out


def exposure_window(
    store,
    *,
    project_key: str,
    start: str,
    end: str,
    compatibility_key: str | None = None,
    working_copy_id: str | None = None,
    signal_types: tuple[str, ...] | None = None,
) -> dict:
    """Signal occurrences per 100,000 eligible physical lines for one project and interval.

    Counts use active projections only. The interval is half-open ``[start, end)``
    in UTC. Rates are never pooled across compatibility keys. See the module
    docstring and SCAN_HANDOFF.md for every returned field.
    """
    if not isinstance(project_key, str) or not project_key:
        raise ValueError("exposure_window project_key must be a non-empty string")
    start_utc, end_utc = normalize_timestamp(start), normalize_timestamp(end)
    if start_utc is None or end_utc is None:
        raise ValueError("exposure_window start and end must be ISO-8601 times with a UTC offset")
    if start_utc >= end_utc:
        raise ValueError("exposure_window requires start < end")
    if compatibility_key is not None and (not isinstance(compatibility_key, str) or not compatibility_key):
        raise ValueError("exposure_window compatibility_key must be a non-empty string or None")
    if working_copy_id is not None and (not isinstance(working_copy_id, str) or not working_copy_id):
        raise ValueError("exposure_window working_copy_id must be a non-empty string or None")
    if signal_types is None:
        signals = SIGNALS
    else:
        if not isinstance(signal_types, tuple) or not signal_types:
            raise ValueError("exposure_window signal_types must be a non-empty tuple or None")
        unknown = [s for s in signal_types if s not in SIGNALS]
        if unknown:
            raise ValueError(f"exposure_window unknown signal types {unknown}; known: {list(SIGNALS)}")
        signals = tuple(dict.fromkeys(signal_types))
    require_schema(store)

    result: dict = {
        "contract_version": CONTRACT_VERSION,
        "requested": {
            "project_key": project_key,
            "start": start_utc,
            "end": end_utc,
            "compatibility_key": compatibility_key,
            "working_copy_id": working_copy_id,
            "signal_types": list(signals),
        },
        "version_groups": [],
        "compatibility_key": None,
        "occurrences": None,
        "occurrences_by_signal": {},
        "diagnostics": {},
        "eligible_lines": None,
        "observed_lines": None,
        "excluded_lines": {},
        "rate_per_100k": None,
        "sessions": None,
        "session_size": None,
        "workload": {},
        "coverage": None,
        "computable": False,
        "reason": "",
    }
    scope_sql = "active = 1 AND project_key = ?"
    scope_params: list = [project_key]
    if working_copy_id is not None:
        scope_sql += " AND working_copy_id = ?"
        scope_params.append(working_copy_id)
    interval_sql = " AND occurred_at >= ? AND occurred_at < ?"
    interval_params = [start_utc, end_utc]
    signal_sql = f" AND signal_type IN ({', '.join('?' for _ in signals)})"

    # Counts cannot establish version compatibility by trusting a hash-shaped
    # column. Verify the manifests of every contributing projection, including
    # occurrence-only rows, against their recorded observation before computing.
    manifest_cache: dict = {}
    provenance = store.query(
        "SELECT p.observation_id, p.compatibility_key AS projection_key, "
        "o.manifest_id, o.compatibility_key FROM ("
        f"SELECT observation_id, compatibility_key FROM scan_lines WHERE {scope_sql} UNION "
        f"SELECT observation_id, compatibility_key FROM scan_occurrences WHERE {scope_sql}"
        ") p LEFT JOIN scan_observations o ON o.id = p.observation_id",
        (*scope_params, *scope_params),
    )
    for row in provenance:
        owner = f"scan observation {row['observation_id']}"
        manifest = _load_manifest(store, row["manifest_id"], owner, manifest_cache)
        if not row["projection_key"] == row["compatibility_key"] == manifest["compatibility_key"]:
            raise ScanObservationError(f"{owner}: projection compatibility differs from its manifest")

    groups = store.query(
        f"SELECT compatibility_key, COUNT(*) AS n FROM scan_lines WHERE {scope_sql} "
        "GROUP BY compatibility_key ORDER BY compatibility_key",
        tuple(scope_params),
    )
    for group in groups:
        key = group["compatibility_key"]
        params = (key, *scope_params, *interval_params)
        eligible = store.query_one(
            f"SELECT COUNT(*) AS n FROM scan_lines WHERE compatibility_key = ? AND {scope_sql}"
            + interval_sql + " AND exclusion = ''",
            params,
        )["n"]
        occurrences = store.query_one(
            f"SELECT COUNT(*) AS n FROM scan_occurrences WHERE compatibility_key = ? AND {scope_sql}"
            + interval_sql + " AND kind = 'signal' AND exclusion = ''" + signal_sql,
            (*params, *signals),
        )["n"]
        identifiable = not key.startswith(UNKNOWN_PREFIX)
        result["version_groups"].append(
            {
                "compatibility_key": key,
                "identifiable": identifiable,
                "active_lines": group["n"],
                # These are the already-verified manifests of contributing
                # observations, not versions reconstructed from current code.
                "manifests": [manifest_cache[mid] for mid in sorted(manifest_cache)
                              if manifest_cache[mid]["compatibility_key"] == key],
                "eligible_lines": eligible,
                "occurrences": occurrences,
                "rate_per_100k": (
                    100000 * occurrences / eligible if identifiable and eligible else None
                ),
            }
        )

    keys = [g["compatibility_key"] for g in result["version_groups"]]
    if not keys:
        result["reason"] = "missing_observations"
        return result
    if compatibility_key is not None:
        if compatibility_key not in keys:
            result["reason"] = "uncovered_version"
            return result
        chosen = compatibility_key
    elif len(keys) > 1:
        result["reason"] = "incompatible_versions"
        return result
    else:
        chosen = keys[0]
    result["compatibility_key"] = chosen

    base_sql = f"compatibility_key = ? AND {scope_sql}"
    base_params = (chosen, *scope_params)
    line_rows = store.query(
        "SELECT exclusion, detector_coverage, source, headless, is_subagent, COUNT(*) AS n "
        f"FROM scan_lines WHERE {base_sql}{interval_sql} "
        "GROUP BY exclusion, detector_coverage, source, headless, is_subagent",
        (*base_params, *interval_params),
    )
    eligible_rows = [r for r in line_rows if r["exclusion"] == ""]
    eligible = sum(r["n"] for r in eligible_rows)
    result["eligible_lines"] = eligible
    result["observed_lines"] = sum(r["n"] for r in line_rows)
    result["excluded_lines"] = _count_map([r for r in line_rows if r["exclusion"]], "exclusion")

    occurrence_rows = store.query(
        "SELECT kind, signal_type, exclusion, source, headless, is_subagent, COUNT(*) AS n "
        f"FROM scan_occurrences WHERE {base_sql}{interval_sql}{signal_sql} "
        "GROUP BY kind, signal_type, exclusion, source, headless, is_subagent",
        (*base_params, *interval_params, *signals),
    )
    primary = [r for r in occurrence_rows if r["kind"] == KIND_SIGNAL and r["exclusion"] == ""]
    result["occurrences"] = sum(r["n"] for r in primary)
    result["occurrences_by_signal"] = {s: 0 for s in signals} | _count_map(primary, "signal_type")
    unknown_time_occurrences = store.query_one(
        f"SELECT COUNT(*) AS n FROM scan_occurrences WHERE {base_sql} AND occurred_at = ''"
        + signal_sql + " AND kind = 'signal'",
        (*base_params, *signals),
    )["n"]
    result["diagnostics"] = {
        "error_fingerprint_occurrences": sum(
            r["n"] for r in occurrence_rows if r["kind"] == KIND_ERROR_FINGERPRINT
        ),
        "excluded_trigger_occurrences": sum(
            r["n"] for r in occurrence_rows if r["kind"] == KIND_SIGNAL and r["exclusion"]
        ),
        "unknown_time_signal_occurrences": unknown_time_occurrences,
    }

    def breakdown(rows: list[dict]) -> dict:
        return {
            "by_source": _count_map(rows, "source"),
            "headless": sum(r["n"] for r in rows if r["headless"]),
            "interactive": sum(r["n"] for r in rows if not r["headless"]),
            "subagent": sum(r["n"] for r in rows if r["is_subagent"]),
            "main": sum(r["n"] for r in rows if not r["is_subagent"]),
        }

    result["workload"] = {"eligible_lines": breakdown(eligible_rows), "occurrences": breakdown(primary)}

    session_rows = store.query(
        "SELECT logical_session_key, COUNT(*) AS n "
        f"FROM scan_lines WHERE {base_sql}{interval_sql} AND exclusion = '' "
        "GROUP BY logical_session_key",
        (*base_params, *interval_params),
    )
    sizes = sorted(r["n"] for r in session_rows if r["logical_session_key"])
    transcripts = store.query_one(
        "SELECT COUNT(DISTINCT transcript_id) AS n "
        f"FROM scan_lines WHERE {base_sql}{interval_sql} AND exclusion = ''",
        (*base_params, *interval_params),
    )["n"]
    result["sessions"] = {
        "logical_sessions": len(sizes),
        "transcripts": transcripts,
        "unknown_session_lines": sum(r["n"] for r in session_rows if not r["logical_session_key"]),
    }
    result["session_size"] = {
        "total": sum(sizes),
        "median": statistics.median(sizes) if sizes else None,
        "max": max(sizes) if sizes else None,
    }

    result["coverage"] = _coverage(
        store, chosen, scope_sql, scope_params, base_sql, base_params, interval_sql,
        interval_params, eligible_rows,
    )

    if chosen.startswith(UNKNOWN_PREFIX):
        result["reason"] = "unknown_version"
        return result
    if eligible == 0:
        result["reason"] = "zero_eligible_exposure"
        return result
    result["rate_per_100k"] = 100000 * result["occurrences"] / eligible
    result["computable"] = True
    return result


def _coverage(
    store, chosen, scope_sql, scope_params, base_sql, base_params, interval_sql,
    interval_params, eligible_rows,
) -> dict:
    """Coverage for the transcripts that contribute any active line to this project."""
    scope_transcripts = (
        f"SELECT DISTINCT transcript_id FROM scan_lines WHERE compatibility_key = ? AND {scope_sql}"
    )
    unattributed = store.query(
        "SELECT exclusion, COUNT(*) AS n FROM scan_lines WHERE active = 1 AND compatibility_key = ? "
        f"AND project_key = '' AND transcript_id IN ({scope_transcripts}) GROUP BY exclusion",
        (chosen, chosen, *scope_params),
    )
    unknown_time_lines = store.query_one(
        f"SELECT COUNT(*) AS n FROM scan_lines WHERE {base_sql} AND occurred_at = ''",
        base_params,
    )["n"]
    observations = store.query(
        "SELECT transcript_id, id, outcome, observed_at, pending_bytes, record_json "
        "FROM scan_observations WHERE compatibility_key = ? "
        f"AND transcript_id IN ({scope_transcripts}) "
        "ORDER BY transcript_id, observed_at DESC, id DESC",
        (chosen, chosen, *scope_params),
    )
    latest: dict[str, dict] = {}
    latest_success: dict[str, dict] = {}
    first_observed = ""
    for row in observations:
        latest.setdefault(row["transcript_id"], row)
        if row["outcome"] == "succeeded":
            latest_success.setdefault(row["transcript_id"], row)
        first_observed = row["observed_at"] if not first_observed else min(first_observed, row["observed_at"])
    stale = sorted(t for t, row in latest.items() if row["outcome"] == "failed")
    pending = {t: row["pending_bytes"] for t, row in latest_success.items() if row["pending_bytes"]}
    incomplete_causes: dict[str, int] = {}
    for transcript, row in latest_success.items():
        record = _observation_record(row)
        causes = record["coverage"]["incomplete_causes"]
        for cause in causes:
            incomplete_causes[cause] = incomplete_causes.get(cause, 0) + 1
    partial_lines = sum(r["n"] for r in eligible_rows if r["detector_coverage"] == "partial")
    counts_by_cause = {
        "stale_failed_reconciliation_transcripts": len(stale),
        "pending_tail_transcripts": len(pending),
        "pending_tail_bytes": sum(pending.values()),
        "partial_detector_coverage_lines": partial_lines,
        # These totals cannot be assigned to this interval. They still prevent
        # the observed subset from claiming complete coverage for that interval.
        "unknown_time_lines": unknown_time_lines,
        **{f"unattributed:{cause}": n for cause, n in _count_map(unattributed, "exclusion").items()},
        **{f"observation:{cause}": n for cause, n in sorted(incomplete_causes.items())},
    }
    earliest = store.query_one(
        f"SELECT MIN(occurred_at) AS t FROM scan_lines WHERE {base_sql} AND occurred_at <> ''",
        base_params,
    )["t"]
    return {
        "coverage_complete": not any(counts_by_cause.values()),
        "counts_by_cause": counts_by_cause,
        "stale_transcripts": stale,
        "time_attribution": {
            "method": "trigger_record_timestamp",
            "interval": "half_open_utc",
            "unknown_time_lines_for_project": unknown_time_lines,
            "unattributed_lines_in_scope_transcripts": _count_map(unattributed, "exclusion"),
            "unallocated": True,
        },
        "retention": {
            "complete_history_known": False,
            "reason": (
                "Only transcripts present when a scan ran were observed. Transcripts "
                "deleted before their first observation are not represented."
            ),
            "earliest_active_line_at": earliest or "",
            "first_observed_at": first_observed,
            "transcripts_in_scope": len(latest),
        },
    }
