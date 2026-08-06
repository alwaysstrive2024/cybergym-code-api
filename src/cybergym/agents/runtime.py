"""Shared, sandboxed tool runtime for CyberGym evaluation agents."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import shlex
import tarfile
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

import httpx

import docker
from cybergym.agents.context import ContextLedger
from cybergym.agents.policy import ExplorationPolicy, PolicyConfig
from cybergym.agents.tool_results import extract_structured_evidence, process_tool_result
from cybergym.task.types import STAGED_ARCHIVES_MANIFEST

LOG = logging.getLogger("cybergym.agent_runtime")
MAX_FILE_BYTES = 1_000_000
MAX_READ_FILE_CHARS = 20_000
DEFAULT_MAX_TOOL_RESULT_CHARS = 12_288


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def json_dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def upload_task_workspace(task_dir: Path, container: Any, workspace: str) -> None:
    """Upload normal task files plus read-only dataset archives from a staging manifest."""
    manifest_path = task_dir / STAGED_ARCHIVES_MANIFEST
    try:
        staged_archives: dict[str, str] = {}
        if manifest_path.is_file():
            value = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or not all(
                isinstance(key, str) and isinstance(item, str) for key, item in value.items()
            ):
                raise RuntimeError("invalid staged archive manifest")
            staged_archives = value
        with tempfile.NamedTemporaryFile(prefix="cybergym-task-", suffix=".tar") as temporary:
            with tarfile.open(temporary.name, mode="w") as archive:
                for entry in sorted(task_dir.rglob("*")):
                    arcname = str(entry.relative_to(task_dir))
                    if entry == manifest_path:
                        continue
                    if entry.is_symlink():
                        continue
                    archive.add(entry, arcname=arcname, recursive=False)
                for arcname, source_name in sorted(staged_archives.items()):
                    archive_path = PurePosixPath(arcname)
                    if archive_path.is_absolute() or ".." in archive_path.parts:
                        raise RuntimeError(f"invalid staged archive destination: {arcname}")
                    source = Path(source_name).resolve(strict=True)
                    if not source.is_file():
                        raise RuntimeError(f"staged archive is not a regular file: {source}")
                    archive.add(source, arcname=arcname, recursive=False)
            temporary.seek(0)
            if not container.put_archive(workspace, temporary):
                raise RuntimeError("failed to upload task workspace to Docker sandbox")
    finally:
        manifest_path.unlink(missing_ok=True)


class TaskSandbox:
    """Network-disabled Docker workspace shared by every supported agent."""

    WORKSPACE = "/workspace"

    def __init__(self, task_dir: Path, image: str, command_timeout: int):
        self.task_dir = task_dir.resolve()
        self.image = image
        self.command_timeout = command_timeout
        self.client = docker.from_env()
        self.name = f"cybergym-agent-{uuid4().hex[:16]}"
        self.container: docker.models.containers.Container | None = None

    def start(self) -> None:
        self.container = self.client.containers.run(
            self.image,
            name=self.name,
            command=["sleep", "infinity"],
            detach=True,
            network_disabled=True,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            pids_limit=512,
            mem_limit="8g",
            nano_cpus=4_000_000_000,
            working_dir=self.WORKSPACE,
        )
        self.container.exec_run(["mkdir", "-p", self.WORKSPACE])
        self._upload_initial_workspace()

    @staticmethod
    def relative_path(requested: str) -> str:
        path = PurePosixPath(requested)
        if path.is_absolute():
            try:
                path = path.relative_to(PurePosixPath(TaskSandbox.WORKSPACE))
            except ValueError as exc:
                raise ValueError("absolute paths must stay below /workspace") from exc
        if ".." in path.parts:
            raise ValueError("path must stay inside the task workspace")
        normalized = str(path)
        return "." if normalized in ("", ".") else normalized

    # Retain the old private spelling while existing callers migrate.
    _relative_path = relative_path

    def _container_path(self, requested: str) -> tuple[str, str]:
        relative = self.relative_path(requested)
        return relative, self.WORKSPACE if relative == "." else f"{self.WORKSPACE}/{relative}"

    def _upload_initial_workspace(self) -> None:
        if not self.container:
            raise RuntimeError("sandbox has not started")
        upload_task_workspace(self.task_dir, self.container, self.WORKSPACE)

    def _exec(self, command: list[str], *, workdir: str | None = None) -> tuple[int, bytes, bytes]:
        if not self.container:
            raise RuntimeError("sandbox has not started")
        result = self.container.exec_run(
            command,
            workdir=workdir or self.WORKSPACE,
            stdout=True,
            stderr=True,
            demux=True,
        )
        stdout, stderr = result.output
        return result.exit_code, stdout or b"", stderr or b""

    def list_files(self, path: str = ".", max_entries: int = 80) -> str:
        relative, container_path = self._container_path(path)
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        limit = min(max_entries, 200)
        quoted_path = shlex.quote(container_path)
        ignored_names = (
            ".git", ".hg", ".svn", ".cache", ".pytest_cache", "__pycache__", "node_modules",
            "build", "dist", "target", ".idea", ".vscode",
        )
        prune = " -o ".join(f"-name {shlex.quote(name)}" for name in ignored_names)
        scan_limit = 2_000
        command = (
            f"if [ -f {quoted_path} ]; then printf 'f\\t.\\n'; "
            f"elif [ -d {quoted_path} ]; then find {quoted_path} -mindepth 1 "
            f"\\( {prune} \\) -prune -o -printf '%y\\t%p\\n' "
            f"| LC_ALL=C sort | head -n {scan_limit + 1}; else exit 3; fi"
        )
        exit_code, stdout, _ = self._exec(["bash", "-lc", command])
        if exit_code == 3:
            return "error: path does not exist"
        if exit_code != 0:
            return f"error: unable to list path (exit_code={exit_code})"
        prefix = "" if relative == "." else relative.rstrip("/") + "/"
        entries: list[tuple[str, str]] = []
        raw_entries = stdout.decode("utf-8", errors="replace").splitlines()
        for line in raw_entries[:scan_limit]:
            kind, separator, value = line.partition("\t")
            if not separator:
                continue
            if value == ".":
                displayed = relative
            elif relative == ".":
                displayed = value.removeprefix(self.WORKSPACE + "/")
            else:
                displayed = prefix + value.removeprefix(container_path.rstrip("/") + "/")
            entries.append((kind, displayed + ("/" if kind == "d" and not displayed.endswith("/") else "")))

        def priority(item: tuple[str, str]) -> tuple[int, int, str]:
            _, displayed = item
            lowered = PurePosixPath(displayed.rstrip("/")).name.lower()
            if lowered.startswith("readme"):
                rank = 0
            elif any(word in lowered for word in ("fuzz", "harness", "crash", "stack", "asan", "ubsan")):
                rank = 1
            elif lowered in {"build.sh", "makefile", "cmakelists.txt"}:
                rank = 2
            else:
                rank = 3
            return rank, displayed.count("/"), displayed

        entries.sort(key=priority)
        displayed_entries = [value for _, value in entries[:limit]]
        if len(entries) > limit or len(raw_entries) > scan_limit:
            displayed_entries.append(f"[truncated; showing {limit} prioritized entries]")
        return "\n".join(displayed_entries) or "(empty)"

    def read_file_bytes(self, path: str, max_bytes: int = MAX_FILE_BYTES) -> bytes:
        _, container_path = self._container_path(path)
        if not self.container:
            raise RuntimeError("sandbox has not started")
        stream, stat = self.container.get_archive(container_path)
        if stat.get("size", 0) > max_bytes:
            raise ValueError(f"file exceeds {max_bytes} byte read limit")
        archive_bytes = b"".join(stream)
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:*") as archive:
            members = [member for member in archive.getmembers() if member.isfile()]
            if len(members) != 1:
                raise ValueError("path is not a regular file")
            extracted = archive.extractfile(members[0])
            if extracted is None:
                raise ValueError("unable to read regular file")
            data = extracted.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError(f"file exceeds {max_bytes} byte read limit")
        return data

    def read_file(
        self,
        path: str,
        start_line: int = 1,
        max_lines: int = 240,
        max_chars: int = MAX_READ_FILE_CHARS,
    ) -> str:
        if start_line < 1 or max_lines < 1 or max_chars < 1:
            return "error: start_line, max_lines, and max_chars must be positive"
        if start_line < 1 or max_lines < 1:
            raise ValueError("start_line and max_lines must be positive")
        text = self.read_file_bytes(path).decode("utf-8", errors="replace")
        limit = min(max_lines, 400)
        char_limit = min(max_chars, MAX_READ_FILE_CHARS)
        numbered: list[str] = []
        used = 0
        for number, line in enumerate(text.splitlines()[start_line - 1 : start_line - 1 + limit], start=start_line):
            rendered = f"{number:>6}\t{line}\n"
            if used + len(rendered) > char_limit:
                numbered.append("[truncated; request a narrower line range]\n")
                break
            numbered.append(rendered)
            used += len(rendered)
        return "".join(numbered)

    def write_file(self, path: str, content: str) -> int:
        relative, _ = self._container_path(path)
        if relative == ".":
            raise ValueError("path must name a regular file")
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_FILE_BYTES:
            raise ValueError(f"content exceeds {MAX_FILE_BYTES} byte write limit")
        parent = str(PurePosixPath(relative).parent)
        if parent not in ("", "."):
            exit_code, _, _ = self._exec(["mkdir", "-p", f"{self.WORKSPACE}/{parent}"])
            if exit_code != 0:
                raise RuntimeError("unable to create parent directory")
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode="w") as archive:
            info = tarfile.TarInfo(name=relative)
            info.size = len(encoded)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(encoded))
        if not self.container or not self.container.put_archive(self.WORKSPACE, payload.getvalue()):
            raise RuntimeError("unable to write file to sandbox")
        return len(encoded)

    def run(self, command: str, timeout: int | None = None) -> str:
        effective_timeout = min(timeout or self.command_timeout, self.command_timeout)
        exit_code, stdout, stderr = self._exec(
            ["timeout", "--preserve-status", "-k", "5", str(effective_timeout), "bash", "-lc", command]
        )
        body = "".join(
            part
            for part in (
                stdout.decode("utf-8", errors="replace") if stdout else "",
                stderr.decode("utf-8", errors="replace") if stderr else "",
            )
        )
        return f"exit_code={exit_code}\n{body}"

    def stop(self) -> None:
        if not self.container:
            return
        try:
            self.container.remove(force=True)
        except docker.errors.NotFound:
            pass
        finally:
            self.container = None


class ToolExecutor:
    """Execute and audit the model-visible CyberGym tool surface."""

    def __init__(
        self,
        task_dir: Path,
        sandbox: TaskSandbox,
        trajectory_path: Path,
        *,
        agent_facing_task_id: str,
        agent_id: str,
        checksum: str,
        server: str,
        differential_submit: bool = False,
        max_tool_result_chars: int = DEFAULT_MAX_TOOL_RESULT_CHARS,
        summary_provider: Callable[[str, str, dict[str, Any]], dict[str, Any] | None] | None = None,
        policy_config: PolicyConfig | None = None,
    ):
        self.task_dir = task_dir.resolve()
        self.sandbox = sandbox
        self.trajectory_path = trajectory_path
        self.agent_facing_task_id = agent_facing_task_id
        self.agent_id = agent_id
        self.checksum = checksum
        self.server = server.rstrip("/")
        self.differential_submit = differential_submit
        self.max_tool_result_chars = max_tool_result_chars
        self.summary_provider = summary_provider
        self.policy = ExplorationPolicy(policy_config)
        self.phase_allowed_names: set[str] | None = None
        self.has_valid_differential_submission = False
        self.infrastructure_failure: dict[str, Any] | None = None
        self.submissions: list[dict[str, Any]] = []
        self.tool_result_index = 0
        self.tool_results_dir = self.trajectory_path.parent / "tool-results"
        self.raw_tool_results_dir = self.tool_results_dir / "raw"
        self.processed_tool_results_dir = self.tool_results_dir / "processed"
        self.tool_summaries_dir = self.tool_results_dir / "summaries"
        self.context_ledger = ContextLedger()
        self.working_memory_path = self.trajectory_path.parent / "working-memory.md"
        self.investigation_state_path = self.trajectory_path.parent / "investigation-state.json"

    def record(self, event: dict[str, Any]) -> None:
        with self.trajectory_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")

    # Retain the old private spelling while existing callers migrate.
    _record = record

    def list_files(self, path: str = ".", max_entries: int = 80) -> str:
        return self.sandbox.list_files(path, max_entries)

    def read_file(
        self,
        path: str,
        start_line: int = 1,
        max_lines: int = 240,
        max_chars: int = MAX_READ_FILE_CHARS,
        hypothesis: str = "",
        expected_evidence: str = "",
        reopen: bool = False,
    ) -> str:
        if not hypothesis and not reopen:
            notice = self.context_ledger.repeated_read_notice(path, start_line, min(max_lines, 400))
            if notice:
                return notice
        try:
            return self.sandbox.read_file(path, start_line, max_lines, max_chars)
        except ValueError as exc:
            return f"error: {exc}; use run_command for targeted inspection"

    def write_file(self, path: str, content: str) -> str:
        try:
            size = self.sandbox.write_file(path, content)
        except ValueError as exc:
            return f"error: {exc}"
        return f"wrote {size} bytes to {path}"

    def save_checkpoint(self, summary: str, next_hypothesis: str = "") -> str:
        """Persist concise evidence across compaction and Claude SDK session resets."""
        try:
            self.context_ledger.add_checkpoint(summary, next_hypothesis)
            if next_hypothesis:
                self.context_ledger.update_investigation_state(
                    {"next_hypothesis": {"primary": next_hypothesis}}
                )
        except ValueError as exc:
            return f"error: {exc}"
        return "checkpoint saved to durable working memory"

    def update_investigation_state(self, **update: Any) -> str:
        """Persist bounded facts, file decisions, call edges, and hypotheses."""
        try:
            self.context_ledger.update_investigation_state(update)
        except ValueError as exc:
            return f"error: {exc}"
        return "investigation state updated"

    def working_memory(self) -> str:
        return self.context_ledger.render()

    def set_execution_phase(self, phase: str, allowed_names: set[str] | None = None) -> None:
        self.policy.set_phase(phase)
        self.phase_allowed_names = allowed_names

    def metrics(self) -> dict[str, Any]:
        return self.policy.state.snapshot(self.policy.config)

    def run_command(self, command: str, timeout_seconds: int = 120) -> str:
        if not command.strip():
            return "error: command is empty"
        if timeout_seconds < 1:
            return "error: timeout_seconds must be positive"
        return self.sandbox.run(command, timeout_seconds)

    def submit_poc(self, path: str) -> str:
        try:
            data = self.sandbox.read_file_bytes(path)
            filename = PurePosixPath(TaskSandbox.relative_path(path)).name
        except (ValueError, docker.errors.NotFound) as exc:
            return f"error: PoC path is not a regular file: {exc}"
        metadata = {
            "task_id": self.agent_facing_task_id,
            "agent_id": self.agent_id,
            "checksum": self.checksum,
            "require_flag": False,
        }
        try:
            endpoint = "submit-diff" if self.differential_submit else "submit-vul"
            response = httpx.post(
                f"{self.server}/{endpoint}",
                data={"metadata": json.dumps(metadata)},
                files={"file": (filename, data, "application/octet-stream")},
                timeout=360 if self.differential_submit else 180,
            )
            response_body: Any = response.text
            if self.differential_submit:
                try:
                    response_body = response.json()
                except ValueError:
                    pass
            record = {
                "timestamp": utc_now(),
                "path": TaskSandbox.relative_path(path),
                "status_code": response.status_code,
                "response": response_body,
            }
            if response.status_code >= 500:
                self.infrastructure_failure = {
                    "operation": "submit_poc",
                    "status_code": response.status_code,
                    "response": response_body,
                }
            if (
                self.differential_submit
                and response.is_success
                and isinstance(response_body, dict)
                and response_body.get("is_valid_exploit") is True
            ):
                self.has_valid_differential_submission = True
        except httpx.HTTPError as exc:
            record = {
                "timestamp": utc_now(),
                "path": TaskSandbox.relative_path(path),
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
            self.infrastructure_failure = {
                "operation": "submit_poc",
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        self.submissions.append(record)
        return json.dumps(record, ensure_ascii=False)

    def _bounded_result(
        self, name: str, raw_result: str, arguments: dict[str, Any] | None = None
    ) -> tuple[str, dict[str, Any]]:
        """Persist raw/processed artifacts and bound only the processed model view."""
        self.tool_result_index += 1
        artifact_name = f"{self.tool_result_index:05d}-{name}.txt"
        self.raw_tool_results_dir.mkdir(parents=True, exist_ok=True)
        self.processed_tool_results_dir.mkdir(parents=True, exist_ok=True)
        raw_path = self.raw_tool_results_dir / artifact_name
        processed_path = self.processed_tool_results_dir / artifact_name
        with raw_path.open("x", encoding="utf-8") as handle:
            handle.write(raw_result)

        processed = process_tool_result(name, raw_result, arguments)
        result = processed.text
        with processed_path.open("x", encoding="utf-8") as handle:
            handle.write(result)
        metadata: dict[str, Any] = {
            "result_view": "processed",
            "raw_result_chars": len(raw_result),
            "processed_result_chars": len(result),
            "raw_result_path": str(raw_path.relative_to(self.trajectory_path.parent)),
            "processed_result_path": str(processed_path.relative_to(self.trajectory_path.parent)),
            "raw_result_sha256": hashlib.sha256(raw_result.encode("utf-8")).hexdigest(),
            "processed_result_sha256": hashlib.sha256(result.encode("utf-8")).hexdigest(),
            "processing": processed.metadata,
        }
        if len(result) <= self.max_tool_result_chars:
            return result, metadata
        summary = extract_structured_evidence(name, raw_result, arguments)
        summary_kind = "deterministic_evidence"
        summary_error: str | None = None
        if summary is None and self.summary_provider is not None and name in {"read_file", "run_command"}:
            try:
                candidate = self.summary_provider(name, result, arguments or {})
                summary = self._validated_agent_summary(candidate)
                summary_kind = "independent_agent"
            except Exception as exc:
                LOG.warning("tool result summarizer failed; using processed fallback: %s", exc)
                summary_error = type(exc).__name__
        if summary is not None:
            self.tool_summaries_dir.mkdir(parents=True, exist_ok=True)
            summary_path = self.tool_summaries_dir / artifact_name.replace(".txt", ".json")
            summary_text = json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
            with summary_path.open("x", encoding="utf-8") as handle:
                handle.write(summary_text)
            if len(summary_text) <= self.max_tool_result_chars:
                metadata.update(
                    result_view="summary",
                    summary_result_path=str(summary_path.relative_to(self.trajectory_path.parent)),
                    summary_result_chars=len(summary_text),
                    summary_result_sha256=hashlib.sha256(summary_text.encode("utf-8")).hexdigest(),
                    summary_kind=summary_kind,
                )
                return summary_text, metadata
        if summary_error:
            metadata["summary_fallback"] = summary_error
        head_chars = self.max_tool_result_chars * 3 // 4
        tail_chars = self.max_tool_result_chars - head_chars
        omitted = len(result) - self.max_tool_result_chars
        bounded = (
            result[:head_chars]
            + f"\n\n[... {omitted} characters omitted; full output is in host run artifacts. Rerun a narrower query for omitted evidence ...]\n\n"
            + result[-tail_chars:]
        )
        metadata.update(result_view="processed_truncated", result_truncated=True)
        return bounded, metadata

    @staticmethod
    def _validated_agent_summary(candidate: dict[str, Any] | None) -> dict[str, Any] | None:
        """Reject free-form or ungrounded model summaries before exposing them."""
        if candidate is None:
            return None
        if not isinstance(candidate, dict) or not isinstance(candidate.get("artifact_type"), str):
            raise ValueError("summary must be an object with artifact_type")
        facts = candidate.get("proven_facts", [])
        if not isinstance(facts, list) or len(facts) > 40:
            raise ValueError("proven_facts must be a list")
        for fact in facts:
            if (
                not isinstance(fact, dict)
                or not str(fact.get("location", "")).strip()
                or not str(fact.get("fact", "")).strip()
                or not re.search(r":(?:L)?\d+(?:-L?\d+)?\Z", str(fact.get("location", "")).strip())
            ):
                raise ValueError("each proven fact requires a file:line location and fact")
        uncertainties = candidate.get("uncertainties", [])
        if not isinstance(uncertainties, list):
            raise ValueError("uncertainties must be a list")
        if not facts and not uncertainties:
            raise ValueError("summary must contain grounded facts or explicit uncertainties")
        encoded = json.dumps(candidate, ensure_ascii=False)
        if len(encoded) > 8_000:
            raise ValueError("summary exceeds 8000 characters")
        return candidate

    def invoke(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        allowed_names: set[str] | None = None,
    ) -> str:
        methods = {
            "list_files": self.list_files,
            "read_file": self.read_file,
            "write_file": self.write_file,
            "save_checkpoint": self.save_checkpoint,
            "update_investigation_state": self.update_investigation_state,
            "run_command": self.run_command,
            "submit_poc": self.submit_poc,
        }
        if allowed_names is None:
            effective_allowed = self.phase_allowed_names
        elif self.phase_allowed_names is None:
            effective_allowed = allowed_names
        else:
            effective_allowed = allowed_names & self.phase_allowed_names
        decision = self.policy.before_tool(name, arguments)
        self.policy.observe_before_result(name, arguments, self.context_ledger.revision)
        valid_before = self.has_valid_differential_submission
        try:
            if effective_allowed is not None and name not in effective_allowed:
                result = f"error: tool {name} is not available in this phase"
            elif decision and decision.blocked:
                self.policy.state.policy_block_count += 1
                result = f"policy_blocked: {decision.reason}. {decision.message}"
            elif name not in methods:
                result = f"error: unknown tool {name}"
            else:
                result = methods[name](**arguments)
        except Exception as exc:  # The model needs a bounded, observable tool error.
            LOG.exception("tool %s failed", name)
            result = f"error: {type(exc).__name__}: {exc}"
        automatic_evidence = extract_structured_evidence(name, result, arguments)
        if automatic_evidence and automatic_evidence.get("artifact_type") == "sanitizer_or_runtime_log":
            self.context_ledger.update_investigation_state(
                {"crash_evidence": automatic_evidence.get("evidence_lines", [])[:6]}
            )
        self.context_ledger.observe_tool(name, arguments, result)
        self.policy.sync_hypothesis_revision(self.context_ledger.revision)
        if name == "read_file" and "exact source range was already inspected" in result:
            self.policy.repeat_blocked()
        self.working_memory_path.write_text(self.context_ledger.render() + "\n", encoding="utf-8")
        json_dump(self.investigation_state_path, self.context_ledger.snapshot())
        bounded_result, result_metadata = self._bounded_result(name, result, arguments)
        if decision and not decision.blocked:
            self.policy.state.policy_guidance_count += 1
            bounded_result += f"\n\n[policy guidance: {decision.reason}] {decision.message}"
            self.record(
                {
                    "timestamp": utc_now(),
                    "event": "policy_guidance",
                    "reason": decision.reason,
                    "tool": name,
                    "recommended_action": decision.message,
                }
            )
            result_metadata["result_view"] = f"{result_metadata.get('result_view', 'processed')}_guided"
        elif decision and decision.blocked:
            self.record(
                {
                    "timestamp": utc_now(),
                    "event": "policy_blocked",
                    "reason": decision.reason,
                    "tool": name,
                    "allowed_next_actions": ["update_investigation_state", "write_file", "submit_poc", "narrow_read"],
                }
            )
        self.policy.observe_visible_result(
            name,
            bounded_result,
            raw_chars=int(result_metadata.get("raw_result_chars", len(result))),
            processed_chars=int(result_metadata.get("processed_result_chars", len(result))),
        )
        result_metadata["model_result_chars"] = len(bounded_result)
        result_metadata["model_result_sha256"] = hashlib.sha256(bounded_result.encode("utf-8")).hexdigest()
        if name == "submit_poc" and not result.startswith("error:"):
            valid_submission = self.has_valid_differential_submission and not valid_before
            if self.differential_submit:
                self.policy.observe_submission(valid_submission)
            receipt = self.submissions[-1] if self.submissions else {}
            response = receipt.get("response", {}) if isinstance(receipt, dict) else {}
            vul = response.get("vul", {}) if isinstance(response, dict) else {}
            fixed = response.get("fixed", {}) if isinstance(response, dict) else {}
            self.record(
                {
                    "timestamp": utc_now(),
                    "event": "submission_outcome",
                    "submission_index": len(self.submissions),
                    "path": receipt.get("path") if isinstance(receipt, dict) else None,
                    "status_code": receipt.get("status_code") if isinstance(receipt, dict) else None,
                    "is_valid_exploit": (
                        response.get("is_valid_exploit", valid_submission)
                        if self.differential_submit and isinstance(response, dict)
                        else None
                    ),
                    "vul_exit_code": vul.get("exit_code") if isinstance(vul, dict) else None,
                    "fixed_exit_code": fixed.get("exit_code") if isinstance(fixed, dict) else None,
                }
            )
        self.record(
            {
                "timestamp": utc_now(),
                "event": "tool",
                "name": name,
                "arguments": arguments,
                "result": bounded_result,
                **result_metadata,
            }
        )
        self.record({"timestamp": utc_now(), "event": "policy_state", **self.metrics()})
        return bounded_result
