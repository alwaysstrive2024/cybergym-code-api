from cybergym.agents.tool_results import (
    extract_structured_evidence,
    process_read_file,
    process_run_command,
    process_tool_result,
)


def numbered(*lines: str) -> str:
    return "".join(f"{number:>6}\t{line}\n" for number, line in enumerate(lines, 1))


def test_license_header_is_omitted_without_renumbering() -> None:
    raw = numbered(
        "/*",
        " * Copyright (C) 2019 Example",
        " * This library is free software; GNU Lesser General Public License.",
        " */",
        "#include <stdint.h>",
    )
    processed = process_read_file(raw)
    assert processed.text == "[license header omitted: lines 1-4]\n     5\t#include <stdint.h>\n"
    assert processed.metadata["license_lines_omitted"] == 4


def test_non_license_and_function_semantic_comments_are_preserved() -> None:
    raw = numbered(
        "/* parser for the wire format */",
        "int parse(size_t length) {",
        "    /* caller guarantees length includes the terminating byte */",
        "    return data[length];",
        "}",
    )
    assert process_read_file(raw).text == raw
    assert process_read_file(raw).metadata["function_definitions"][0]["line"] == 2


def test_repeated_blank_lines_are_compressed_but_line_numbers_stay_original() -> None:
    raw = numbered("int a;", "", "", "", "int b;")
    processed = process_read_file(raw)
    assert processed.text == "     1\tint a;\n     2\t\n     5\tint b;\n"
    assert processed.metadata["blank_lines_omitted"] == 2


def test_read_file_truncation_marker_is_preserved() -> None:
    raw = numbered("int a;", "") + "[truncated; request a narrower line range]\n"
    assert process_read_file(raw).text.endswith("[truncated; request a narrower line range]\n")


def test_large_static_data_is_folded_cautiously() -> None:
    data = ["static const unsigned table[30] = {"] + [f"    {i}," for i in range(30)] + ["};", "int f(void);"]
    processed = process_read_file(numbered(*data))
    assert "[static numeric data omitted: lines 5-28, 24 of 30 value lines" in processed.text
    assert "     2\t    0," in processed.text
    assert "    31\t    29," in processed.text
    assert "    32\t};" in processed.text
    assert "    33\tint f(void);" in processed.text


def test_static_data_with_security_comment_is_not_folded() -> None:
    values = [f"    {i}," for i in range(30)]
    values[12] = "    12, /* length must stay within buffer capacity */"
    raw = numbered("static const unsigned table[30] = {", *values, "};")
    assert process_read_file(raw).text == raw


def test_static_table_with_expressions_or_designators_is_not_folded() -> None:
    expressions = [f"    VALUE_{i}," for i in range(30)]
    raw = numbered("static const unsigned table[30] = {", *expressions, "};")
    assert process_read_file(raw).text == raw

    designated = [f"    [{i}] = {i}," for i in range(30)]
    raw = numbered("static const unsigned table[30] = {", *designated, "};")
    assert process_read_file(raw).text == raw


def test_run_command_deduplicates_only_exact_warnings_and_keeps_sanitizer_stack() -> None:
    warning = "a.c:4: warning: unused value\n"
    raw = (
        warning
        + warning
        + "b.c:5: warning: unused value\n"
        + "ERROR: AddressSanitizer: heap-buffer-overflow\n#0 0x1234 in parse a.c:9\n"
    )
    processed = process_run_command(raw)
    assert processed.text.count("a.c:4: warning") == 1
    assert "b.c:5: warning" in processed.text
    assert "AddressSanitizer: heap-buffer-overflow" in processed.text
    assert "#0 0x1234 in parse a.c:9" in processed.text
    assert processed.metadata["duplicate_warnings_omitted"] == 1
    assert processed.metadata["contains_sanitizer_evidence"] is True
    assert "[exact duplicate warnings omitted: 1]" in processed.text
    assert processed.text.startswith("[priority diagnostics extracted from full output]")


def test_mid_log_sanitizer_evidence_is_moved_before_noise() -> None:
    raw = "exit_code=1\n" + ("ordinary build output\n" * 1000)
    raw += "ERROR: AddressSanitizer: stack-buffer-overflow\n#0 0x1234 in decode parser.c:77\n"
    raw += "tail noise\n" * 1000
    processed = process_run_command(raw)

    assert processed.text.index("AddressSanitizer") < 500
    assert "#0 0x1234 in decode parser.c:77" in processed.text[:1000]


def test_sanitizer_priority_keeps_following_input_and_object_context() -> None:
    raw = (
        "exit_code=1\n"
        + "noise\n" * 1_000
        + "ERROR: AddressSanitizer: heap-buffer-overflow\n"
        + "READ of size 4 at 0x1234\n"
        + "input artifact: candidate.bin\n"
        + "object capacity: 16 bytes\n"
        + "tail\n" * 1_000
    )
    processed = process_run_command(raw)
    priority = processed.text.split("[end priority diagnostics]", 1)[0]
    assert "input artifact: candidate.bin" in priority
    assert "object capacity: 16 bytes" in priority


def test_generated_header_is_omitted_but_non_header_notice_is_preserved() -> None:
    header = numbered("/* auto-generated; do not edit manually */", "int generated_value;")
    assert process_read_file(header).text.startswith("[license header omitted: lines 1-1]")

    body = numbered("int f(void) {", "  /* auto-generated value is validated here */", "  return 1;", "}")
    assert process_read_file(body).text == body


def test_long_nonsemantic_doxygen_file_prose_is_omitted_conservatively() -> None:
    prose = ["/**", " * @file decoder.c"] + [f" * General decoder overview section {i}." for i in range(10)] + [" */"]
    processed = process_read_file(numbered(*prose, "int decode(void);"))
    assert "[non-semantic file documentation omitted: lines 1-13]" in processed.text
    assert "    14\tint decode(void);" in processed.text

    contract = prose[:-2] + [" * @param length input buffer length", " */"]
    raw = numbered(*contract, "int decode(void);")
    assert process_read_file(raw).text == raw


def test_license_block_with_security_note_is_kept_whole() -> None:
    raw = numbered(
        "// Copyright 2020 Example",
        "// Licensed under the Apache License",
        "// FIXME: length must remain within buffer capacity",
        "int parse(void);",
    )
    assert process_read_file(raw).text == raw


def test_partial_license_header_at_read_boundary_is_still_omitted() -> None:
    raw = numbered(
        "/*",
        " * Copyright (C) 2019 Example",
        " * This library is free software; GNU Lesser General Public License.",
        " * This library is distributed without any warranty.",
    )
    processed = process_read_file(raw)
    assert processed.text == "[license header omitted: lines 1-4]\n"


def test_hash_prefixed_license_after_shebang_is_omitted() -> None:
    raw = numbered(
        "#!/usr/bin/env python3",
        "# Copyright 2020 Example",
        "# Licensed under the Apache License, Version 2.0",
        "",
        "def main(): pass",
    )
    processed = process_read_file(raw)
    assert "Copyright" not in processed.text
    assert "     1\t#!/usr/bin/env python3" in processed.text
    assert "[license header omitted: lines 2-3]" in processed.text


def test_run_command_compacts_long_hex_with_audit_metadata() -> None:
    blob = "ab" * 100
    processed = process_run_command(f"payload={blob}\n")
    assert blob not in processed.text
    assert "[hex omitted: 100 bytes, sha256=" in processed.text
    assert processed.metadata["hex_blobs_compacted"] == 1


def test_long_command_output_copies_critical_error_to_the_front() -> None:
    raw = "noise\n" * 3_000 + "ERROR: AddressSanitizer: use-after-free\n#0 0xbeef in vulnerable a.c:9\n"
    processed = process_run_command(raw)
    assert processed.text.startswith("[priority diagnostics extracted from full output]\nERROR: AddressSanitizer")
    assert processed.metadata["priority_diagnostics_prepended"] >= 1


def test_unknown_tool_passes_through() -> None:
    processed = process_tool_result("submit_poc", "unchanged")
    assert processed.text == "unchanged"
    assert processed.metadata == {"processor": "passthrough", "changed": False}


def test_successful_build_log_is_compacted_but_warnings_and_tail_remain() -> None:
    raw = "exit_code=0\n" + ("compiling object\n" * 400) + "a.c:4: warning: unused value\nlinked target\n"
    processed = process_tool_result("run_command", raw, {"command": "make -j2"})

    assert "[successful build output omitted:" in processed.text
    assert "a.c:4: warning: unused value" in processed.text
    assert "linked target" in processed.text
    assert processed.metadata["successful_build_compacted"] is True


def test_large_rg_result_gets_a_file_index() -> None:
    raw = "exit_code=0\n" + "".join(f"src/file{i % 3}.c:{i}:match\n" for i in range(60))
    processed = process_tool_result("run_command", raw, {"command": "rg -n match src"})

    assert processed.text.startswith("[rg evidence index: 60 matches across 3 files]")
    assert processed.metadata["search_match_lines"] == 60

    summary = extract_structured_evidence("run_command", raw, {"command": "rg -n match src"})
    assert summary is not None
    assert summary["artifact_type"] == "symbol_search"
    assert summary["match_count"] == 60
