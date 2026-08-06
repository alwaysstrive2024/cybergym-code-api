"""Deterministic, loss-aware cleanup for agent-facing tool results.

The functions in this module never mutate the raw result.  Callers are expected
to persist that result separately and may persist ``ProcessedToolResult.text``
as the processed artifact.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any  # noqa: UP035

_NUMBERED_LINE = re.compile(r"^(?P<prefix>\s*(?P<number>\d+)\t)(?P<body>.*)$")
_LICENSE_WORDS = re.compile(
    r"copyright|spdx-license-identifier|gnu (?:lesser |affero )?general public license|"
    r"redistribution and use|permission is hereby granted|licensed under the apache license|"
    r"mozilla public license|this (?:library|program|software) is free software|"
    r"all rights reserved",
    re.IGNORECASE,
)
_SEMANTIC_COMMENT_WORDS = re.compile(
    r"todo|fixme|hack|bound|size|length|overflow|underflow|alloc|free|ownership|caller|"
    r"must|guarantee|valid|invalid|malform|terminat|security|unsafe|index|capacity",
    re.IGNORECASE,
)
_STATIC_DECLARATION = re.compile(r"\bstatic\s+(?:const\s+)?(?:[\w:*]+\s+)+\w+\s*\[[^]]*]\s*=\s*\{")
_PLAIN_STATIC_VALUE = re.compile(r"^[\s{}(),.+\-0-9a-fA-FxXuUlLfF]+$")
_WARNING = re.compile(r"(?:^|:\s*)warning:", re.IGNORECASE)
_SANITIZER = re.compile(
    r"addresssanitizer|undefinedbehaviorsanitizer|threadsanitizer|memorysanitizer|"
    r"leaksanitizer|runtime error:|summary: .*sanitizer|^\s*#\d+\s+0x[0-9a-f]+",
    re.IGNORECASE,
)
_KEY_ERROR = re.compile(
    r"(?:^|:\s*)(?:fatal )?error:|segmentation fault|core dumped|assertion .* failed", re.IGNORECASE
)
_LONG_HEX = re.compile(r"(?<![0-9A-Fa-f])(?:[0-9A-Fa-f]{2}){64,}(?![0-9A-Fa-f])")
_SANITIZER_DETAIL = re.compile(
    r"\b(?:read|write) of size\b|is located\b|allocated by thread|freed by thread|shadow bytes|"
    r"^\s*0x[0-9a-f]+ is located",
    re.IGNORECASE,
)
_GENERATED_HEADER = re.compile(
    r"generated (?:automatically|by)|auto-generated|do not edit (?:this file )?manually",
    re.IGNORECASE,
)
_FUNCTION_DEFINITION = re.compile(
    r"^(?:[\w:*<>]+\s+)+(?:[\w:]+)\s*\([^;]*\)\s*(?:\{|$)"
)
_HIGH_VALUE_COMMENT = re.compile(
    r"todo|fixme|hack|bounds?|size|length|overflow|underflow|ownership|caller|guarantee|"
    r"malform|security|unsafe|index|capacity|@param|@return|@pre|@post",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ProcessedToolResult:
    """Processed text plus JSON-serializable cleanup diagnostics."""

    text: str
    metadata: dict[str, Any]


def _split_numbered(text: str) -> tuple[list[tuple[int, str, str]], list[str]] | None:
    parsed: list[tuple[int, str, str]] = []
    suffix: list[str] = []
    for line in text.splitlines():
        match = _NUMBERED_LINE.match(line)
        if not match:
            # A read may end with the sandbox truncation marker.
            if line.startswith("[truncated;"):
                suffix.append(line)
                continue
            return None
        parsed.append((int(match.group("number")), match.group("prefix"), match.group("body")))
    return parsed, suffix


def _license_span(lines: list[tuple[int, str, str]]) -> tuple[int, int] | None:
    """Return indexes of a confidently identified, file-leading license block."""
    if not lines:
        return None
    start = 0
    while start < len(lines) and (
        not lines[start][2].strip()
        or lines[start][2].startswith("#!")
        or re.match(r"^#.*coding[:=]", lines[start][2])
    ):
        start += 1
    if start >= len(lines) or lines[start][0] > 12:
        return None

    body = lines[start][2].lstrip()
    line_prefix = next((prefix for prefix in ("//", "#", ";", "--") if body.startswith(prefix)), None)
    if line_prefix:
        end = start
        while end + 1 < len(lines) and lines[end + 1][2].lstrip().startswith(line_prefix):
            end += 1
    elif body.startswith("/*"):
        end = start
        while end < len(lines) and "*/" not in lines[end][2]:
            end += 1
        if end == len(lines):
            partial = "\n".join(item[2] for item in lines[start:])
            if _LICENSE_WORDS.search(partial) and all(
                not item[2].strip() or item[2].lstrip().startswith(("/*", "*")) for item in lines[start:]
            ):
                end = len(lines) - 1
            else:
                return None
    else:
        return None

    comment = "\n".join(item[2] for item in lines[start : end + 1])
    # Avoid removing ordinary file documentation or security-relevant comments.
    if not (_LICENSE_WORDS.search(comment) or _GENERATED_HEADER.search(comment)):
        return None
    if _HIGH_VALUE_COMMENT.search(comment):
        return None
    return start, end


def _file_doc_span(lines: list[tuple[int, str, str]]) -> tuple[int, int] | None:
    """Identify only long, file-leading Doxygen prose with no contract semantics."""
    if not lines:
        return None
    start = next(
        (
            index
            for index, item in enumerate(lines)
            if item[2].strip() and not item[2].startswith("[license header omitted:")
        ),
        len(lines),
    )
    if start >= len(lines) or not lines[start][2].lstrip().startswith("/**"):
        return None
    if any(item[2].strip() and not item[2].startswith("[license header omitted:") for item in lines[:start]):
        return None
    end = start
    while end < len(lines) and "*/" not in lines[end][2]:
        end += 1
    if end >= len(lines) or end - start < 10:
        return None
    comment = "\n".join(item[2] for item in lines[start : end + 1])
    if not re.search(r"[@\\]file\b", comment, re.IGNORECASE) or _HIGH_VALUE_COMMENT.search(comment):
        return None
    return start, end


def _collapse_blank_numbered(lines: list[tuple[int, str, str]]) -> tuple[list[tuple[int, str, str]], int]:
    result: list[tuple[int, str, str]] = []
    blanks = 0
    for item in lines:
        if not item[2].strip() and result and not result[-1][2].strip():
            blanks += 1
            continue
        result.append(item)
    return result, blanks


def _fold_static_data(lines: list[tuple[int, str, str]]) -> tuple[list[tuple[int, str, str]], int]:
    """Fold only long, file-scope-looking static initializers without semantic comments."""
    result: list[tuple[int, str, str]] = []
    folded = 0
    index = 0
    while index < len(lines):
        if not _STATIC_DECLARATION.search(lines[index][2]):
            result.append(lines[index])
            index += 1
            continue
        end = index + 1
        while end < len(lines) and not re.search(r"}\s*;", lines[end][2]):
            end += 1
        interior = lines[index + 1 : end]
        if (
            end >= len(lines)
            or len(interior) < 24
            or any(not _PLAIN_STATIC_VALUE.fullmatch(body) for _, _, body in interior)
            or any("//" in body or "/*" in body or "#" in body for _, _, body in interior)
            or any(_SEMANTIC_COMMENT_WORDS.search(body) for _, _, body in interior if "//" in body or "/*" in body)
        ):
            result.append(lines[index])
            index += 1
            continue
        result.append(lines[index])
        leading = interior[:3]
        trailing = interior[-3:]
        omitted_first = interior[3][0]
        omitted_last = interior[-4][0]
        result.extend(leading)
        marker = (
            f"[static numeric data omitted: lines {omitted_first}-{omitted_last}, "
            f"{len(interior) - 6} of {len(interior)} value lines; first/last samples retained]"
        )
        result.append((omitted_first, "", marker))
        result.extend(trailing)
        result.append(lines[end])
        folded += 1
        index = end + 1
    return result, folded


def process_read_file(result: str) -> ProcessedToolResult:
    """Clean a numbered ``read_file`` result without changing source line numbers."""
    split = _split_numbered(result)
    if split is None:
        return ProcessedToolResult(result, {"processor": "read_file", "changed": False, "reason": "not_numbered"})
    parsed, suffix = split

    metadata: dict[str, Any] = {"processor": "read_file", "raw_chars": len(result)}
    span = _license_span(parsed)
    if span:
        start, end = span
        first, last = parsed[start][0], parsed[end][0]
        parsed[start : end + 1] = [(first, "", f"[license header omitted: lines {first}-{last}]")]
        metadata["license_lines_omitted"] = last - first + 1

    doc_span = _file_doc_span(parsed)
    if doc_span:
        start, end = doc_span
        first, last = parsed[start][0], parsed[end][0]
        parsed[start : end + 1] = [(first, "", f"[non-semantic file documentation omitted: lines {first}-{last}]")]
        metadata["file_documentation_lines_omitted"] = last - first + 1

    parsed, blanks = _collapse_blank_numbered(parsed)
    parsed, tables = _fold_static_data(parsed)
    if blanks:
        metadata["blank_lines_omitted"] = blanks
    if tables:
        metadata["static_data_blocks_folded"] = tables
    symbols = []
    for line_number, _, body in parsed:
        if _FUNCTION_DEFINITION.match(body.strip()):
            symbols.append({"line": line_number, "signature": body.strip()[:300]})
        if len(symbols) >= 30:
            break
    if symbols:
        metadata["function_definitions"] = symbols

    text = "".join(f"{prefix}{body}\n" if prefix else f"{body}\n" for _, prefix, body in parsed)
    if suffix:
        text += "".join(f"{line}\n" for line in suffix)
    if result and not result.endswith("\n"):
        text = text.rstrip("\n")
    metadata.update(processed_chars=len(text), changed=text != result)
    return ProcessedToolResult(text, metadata)


def _replace_long_hex(match: re.Match[str]) -> str:
    value = match.group(0)
    digest = hashlib.sha256(value.encode("ascii")).hexdigest()[:16]
    return f"{value[:32]}...[hex omitted: {len(value) // 2} bytes, sha256={digest}]...{value[-32:]}"


def process_run_command(result: str, command: str = "") -> ProcessedToolResult:
    """Deduplicate warnings and compact huge hex blobs while retaining diagnostics."""
    seen_warnings: dict[str, int] = {}
    output: list[str] = []
    duplicate_warnings = 0
    sanitizer_lines = 0
    error_lines = 0
    for line in result.splitlines(keepends=True):
        plain = line.rstrip("\r\n")
        if _SANITIZER.search(plain):
            sanitizer_lines += 1
        if _KEY_ERROR.search(plain):
            error_lines += 1
        if _WARNING.search(plain):
            # Exact warning lines are safe to coalesce; similar warnings at
            # different source locations remain distinct evidence.
            if plain in seen_warnings:
                seen_warnings[plain] += 1
                duplicate_warnings += 1
                continue
            seen_warnings[plain] = 1
        output.append(_LONG_HEX.sub(_replace_long_hex, line))

    text = "".join(output)
    if duplicate_warnings:
        text += f"[exact duplicate warnings omitted: {duplicate_warnings}]\n"
    # Put sanitizer evidence and its immediate context first so a later size
    # bound cannot discard a crash stack that appeared in the middle of a log.
    priority_diagnostics: list[str] = []
    if sanitizer_lines:
        plain_lines = text.splitlines()
        selected: set[int] = set()
        for index, line in enumerate(plain_lines):
            if _SANITIZER.search(line) or _SANITIZER_DETAIL.search(line):
                selected.update(range(index, min(len(plain_lines), index + 4)))
        priority_diagnostics = [
            plain_lines[index] for index in sorted(selected) if not _WARNING.search(plain_lines[index])
        ][:80]
        if priority_diagnostics:
            prioritized_indexes = {
                index for index in sorted(selected)[:80] if not _WARNING.search(plain_lines[index])
            }
            remainder = "\n".join(line for index, line in enumerate(plain_lines) if index not in prioritized_indexes)
            text = (
                "[priority diagnostics extracted from full output]\n"
                + "\n".join(priority_diagnostics)
                + "\n[end priority diagnostics]\n"
                + remainder
                + ("\n" if text.endswith("\n") else "")
            )
    metadata: dict[str, Any] = {
        "processor": "run_command",
        "raw_chars": len(result),
        "processed_chars": len(text),
        "changed": text != result,
        "duplicate_warnings_omitted": duplicate_warnings,
        "contains_sanitizer_evidence": sanitizer_lines > 0,
        "sanitizer_evidence_lines": sanitizer_lines,
        "key_error_lines": error_lines,
        "sanitizer_diagnostics_prioritized": bool(priority_diagnostics),
    }
    if priority_diagnostics:
        metadata["priority_diagnostics_prepended"] = len(priority_diagnostics)
    normalized_command = " ".join(command.split())
    build_command = bool(
        re.search(r"(?:^|[;&|]\s*)(?:make|cmake|ninja|meson|cargo build|gcc|g\+\+|clang|clang\+\+)\b", normalized_command)
    )
    if build_command and result.startswith("exit_code=0") and not sanitizer_lines and not error_lines and len(text) > 4_000:
        warnings = [line for line in text.splitlines() if _WARNING.search(line)]
        tail = [line for line in text.splitlines()[-8:] if line and not _WARNING.search(line)]
        text = (
            "exit_code=0\n"
            f"[successful build output omitted: {len(result)} raw characters; {len(warnings)} unique warnings]\n"
            + "\n".join([*warnings, *tail])
            + "\n"
        )
        metadata["successful_build_compacted"] = True
    if re.search(r"(?:^|[;&|]\s*)rg\b", normalized_command):
        match_lines = [line for line in result.splitlines() if re.match(r"[^:\n]+:\d+:", line)]
        matched_files = sorted({line.split(":", 1)[0] for line in match_lines})
        metadata["search_match_lines"] = len(match_lines)
        metadata["search_matched_files"] = len(matched_files)
        if len(match_lines) > 40:
            text = (
                f"[rg evidence index: {len(match_lines)} matches across {len(matched_files)} files]\n"
                + "\n".join(f"- {path}" for path in matched_files[:40])
                + "\n[full processed matches follow]\n"
                + text
            )
    hex_matches = len(_LONG_HEX.findall(result))
    if hex_matches:
        metadata["hex_blobs_compacted"] = hex_matches
    metadata["processed_chars"] = len(text)
    metadata["changed"] = text != result
    return ProcessedToolResult(text, metadata)


def process_tool_result(
    tool_name: str, result: str, arguments: dict[str, Any] | None = None
) -> ProcessedToolResult:
    """Dispatch deterministic cleanup by tool name; unknown tools pass through."""
    if tool_name == "read_file":
        return process_read_file(result)
    if tool_name == "run_command":
        return process_run_command(result, str((arguments or {}).get("command", "")))
    return ProcessedToolResult(result, {"processor": "passthrough", "changed": False})


def extract_structured_evidence(
    tool_name: str, result: str, arguments: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Extract only deterministic, line-backed evidence from oversized results."""
    if tool_name != "run_command":
        return None
    command = " ".join(str((arguments or {}).get("command", "")).split())
    lines = result.splitlines()
    exit_line = next((line for line in lines if line.startswith("exit_code=")), "exit_code=unknown")
    sanitizer = [
        line
        for line in lines
        if _SANITIZER.search(line) or _SANITIZER_DETAIL.search(line) or _KEY_ERROR.search(line)
    ]
    if sanitizer:
        return {
            "artifact_type": "sanitizer_or_runtime_log",
            "command": command[:500],
            "exit_status": exit_line,
            "evidence_lines": sanitizer[:120],
            "evidence_lines_omitted": max(0, len(sanitizer) - 120),
            "uncertainties": ["Non-matching log context remains in the processed artifact."],
        }
    if re.search(r"(?:^|[;&|]\s*)rg\b", command):
        matches = [line for line in lines if re.match(r"[^:\n]+:\d+:", line)]
        if not matches:
            return None
        by_file: dict[str, list[str]] = {}
        for line in matches:
            by_file.setdefault(line.split(":", 1)[0], []).append(line)
        return {
            "artifact_type": "symbol_search",
            "command": command[:500],
            "exit_status": exit_line,
            "match_count": len(matches),
            "matched_file_count": len(by_file),
            "matches_by_file": {path: values[:4] for path, values in list(by_file.items())[:40]},
            "uncertainties": ["Only the first four matches per listed file are included."],
        }
    return None
