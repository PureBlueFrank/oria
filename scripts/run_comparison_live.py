"""Run the frozen single/multi holdout against one explicitly selected Live target."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import os
import secrets
import subprocess
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal

from oria.config import resolve_runtime_config
from oria.core.runtime import build_runtime
from oria.core.types import ValueModel
from oria.data import initialize_data
from oria.eval.compare import (
    Architecture,
    ArchitectureRunResult,
    ComparisonError,
    ComparisonExecutionSlot,
    ComparisonFrozenBinding,
    ComparisonReport,
    JudgeBasis,
    load_comparison_live_config,
    load_comparison_pricing_snapshot,
    preflight_comparison_live,
    reblind_comparison_runs,
    rebuild_comparison_live_report,
    run_comparison_live,
    select_comparison_live_target,
    shuffled_blind_review_packets,
    validate_comparison_live_resume_report,
)

_CODEX_SUBSCRIPTION_TARGET = "codex-subscription-gpt56-sol-high"
_KNOWN_TARGETS = frozenset({"deepseek", "deepseek-pro-structured", _CODEX_SUBSCRIPTION_TARGET})
_RUNNER_JUDGE_BASIS: JudgeBasis = "structural_proxy"


class ComparisonInFlight(ValueModel):
    """Durable reservation written before a potentially paid architecture slot."""

    position: int
    architecture: Architecture
    case_id: str
    repetition: int
    reserved_at: datetime
    request_count: Literal["unknown"] = "unknown"


class ComparisonRunState(ValueModel):
    """Restricted local state; never publish this file as verification evidence."""

    schema_version: Literal[1] = 1
    target_id: str
    blind_secret_hex: str
    frozen_binding: ComparisonFrozenBinding
    judge_basis: JudgeBasis
    created_at: datetime
    in_flight: ComparisonInFlight | None = None
    pending_results: tuple[ArchitectureRunResult, ...] = ()


class InFlightReservationError(ComparisonError):
    """An earlier paid request may have completed but cannot be proven either way."""

    def __init__(self, reservation: ComparisonInFlight) -> None:
        super().__init__(
            "comparison Live resume blocked by an unresolved in-flight reservation; "
            "request consumption is unknown and the slot will not be retried"
        )
        self.evidence = {
            "request_count": "unknown",
            "in_flight": reservation.model_dump(mode="json"),
        }


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--config", type=Path, default=Path("eval/config/comparison-live-v1.yaml"))
    parser.add_argument("--pricing-dir", type=Path, default=Path("eval/config/pricing"))
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("eval/datasets/scenario_b/v2.manifest.json"),
    )
    parser.add_argument(
        "--rubric",
        type=Path,
        default=Path("eval/config/attribution-rubric-v2.yaml"),
    )
    parser.add_argument("--data-dir", type=Path, default=Path(".artifacts/eval/comparison-live-v1"))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".artifacts/eval/comparison-live-v1/run.json"),
    )
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--run-live", action="store_true", help="explicitly enable model calls")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="resume completed architecture runs from the existing output report",
    )
    parser.add_argument(
        "--max-new-case-runs",
        type=int,
        help="pause cleanly after this many newly completed architecture runs",
    )
    return parser.parse_args()


def _json_bytes(value: object) -> bytes:
    if isinstance(value, ValueModel):
        payload = value.model_dump_json(indent=2)
    else:
        payload = json.dumps(value, ensure_ascii=False, indent=2)
    return (payload + "\n").encode()


def _stage_bytes(path: Path, payload: bytes, *, mode: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _atomic_write_bytes(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    temporary = _stage_bytes(path, payload, mode=mode)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_json(path: Path, value: object, *, mode: int = 0o600) -> None:
    _atomic_write_bytes(path, _json_bytes(value), mode=mode)


def _blind_review_path(output: Path) -> Path:
    return output.with_name(f"{output.stem}.blind-review.json")


def _state_path(output: Path) -> Path:
    return output.with_name(f".{output.stem}.state.json")


def _lock_path(output: Path) -> Path:
    return output.with_name(f".{output.stem}.lock")


@contextmanager
def _exclusive_run_lock(output: Path) -> Iterator[None]:
    lock_path = _lock_path(output)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ComparisonError("another comparison Live process holds the run lock") from exc
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _publish_report_bundle(
    output: Path,
    report: ComparisonReport,
    *,
    blind_secret: bytes,
) -> ComparisonReport:
    packets = shuffled_blind_review_packets(report, blind_secret=blind_secret)
    sidecar_payload = _json_bytes([packet.model_dump(mode="json") for packet in packets])
    published = report.model_copy(
        update={"blind_review_sha256": hashlib.sha256(sidecar_payload).hexdigest()}
    )
    report_payload = _json_bytes(published)
    validated = ComparisonReport.model_validate_json(report_payload)
    if validated != published:
        raise ComparisonError("comparison report bundle failed round-trip validation")

    sidecar = _blind_review_path(output)
    report_temporary: Path | None = None
    sidecar_temporary: Path | None = None
    old_sidecar = sidecar.read_bytes() if sidecar.exists() else None
    try:
        report_temporary = _stage_bytes(output, report_payload, mode=0o600)
        sidecar_temporary = _stage_bytes(sidecar, sidecar_payload, mode=0o600)
        sidecar_temporary.replace(sidecar)
        sidecar_temporary = None
        try:
            report_temporary.replace(output)
            report_temporary = None
        except Exception:
            if old_sidecar is None:
                sidecar.unlink(missing_ok=True)
            else:
                _atomic_write_bytes(sidecar, old_sidecar)
            raise
    finally:
        if report_temporary is not None:
            report_temporary.unlink(missing_ok=True)
        if sidecar_temporary is not None:
            sidecar_temporary.unlink(missing_ok=True)
    return published


def _write_run_state(path: Path, state: ComparisonRunState) -> None:
    _write_json(path, state, mode=0o600)


def _load_run_state(path: Path) -> ComparisonRunState:
    try:
        return ComparisonRunState.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ComparisonError(
            "comparison Live restricted run state is unavailable or invalid"
        ) from exc


def _assert_no_in_flight(reservation: ComparisonInFlight | None) -> None:
    if reservation is not None:
        raise InFlightReservationError(reservation)


def _reserve_run_state(
    path: Path,
    state: ComparisonRunState,
    slot: ComparisonExecutionSlot,
) -> ComparisonRunState:
    _assert_no_in_flight(state.in_flight)
    reservation = ComparisonInFlight(
        position=slot.position,
        architecture=slot.architecture,
        case_id=slot.case_id,
        repetition=slot.repetition,
        reserved_at=datetime.now().astimezone(),
    )
    reserved = state.model_copy(update={"in_flight": reservation})
    _write_run_state(path, reserved)
    return reserved


def _complete_run_state(
    path: Path,
    state: ComparisonRunState,
    slot: ComparisonExecutionSlot,
    result: ArchitectureRunResult,
) -> ComparisonRunState:
    reservation = state.in_flight
    if reservation is None or (
        reservation.position,
        reservation.architecture,
        reservation.case_id,
        reservation.repetition,
    ) != (slot.position, slot.architecture, slot.case_id, slot.repetition):
        raise ComparisonError("comparison Live in-flight reservation does not match result")
    completed = state.model_copy(
        update={
            "in_flight": None,
            "pending_results": (*state.pending_results, result),
        }
    )
    _write_run_state(path, completed)
    return completed


def _record_failure(output: Path, value: object) -> None:
    failure_path = output.with_name(f"{output.stem}.failure-{uuid.uuid4().hex}.json")
    _write_json(failure_path, value)


def _codex_chatgpt_auth_ready() -> bool:
    try:
        completed = subprocess.run(
            ["codex", "login", "status"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    output = completed.stdout + completed.stderr
    return completed.returncode == 0 and "Logged in using ChatGPT" in output


def _read_report(path: Path) -> ComparisonReport:
    try:
        return ComparisonReport.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ComparisonError("existing comparison Live report cannot be resumed") from exc


def _decode_blind_secret(state: ComparisonRunState) -> bytes:
    try:
        secret = bytes.fromhex(state.blind_secret_hex)
    except ValueError as exc:
        raise ComparisonError("comparison Live blind secret is invalid") from exc
    if len(secret) < 32:
        raise ComparisonError("comparison Live blind secret is invalid")
    return secret


def _new_run_state(
    *, target_id: str, frozen_binding: ComparisonFrozenBinding, now: datetime
) -> ComparisonRunState:
    return ComparisonRunState(
        target_id=target_id,
        blind_secret_hex=secrets.token_hex(32),
        frozen_binding=frozen_binding,
        judge_basis=_RUNNER_JUDGE_BASIS,
        created_at=now,
    )


def _merge_pending_results(
    report_runs: tuple[ArchitectureRunResult, ...],
    pending_results: tuple[ArchitectureRunResult, ...],
) -> tuple[ArchitectureRunResult, ...]:
    if not pending_results:
        return report_runs
    if (
        len(report_runs) >= len(pending_results)
        and report_runs[-len(pending_results) :] == pending_results
    ):
        return report_runs
    return report_runs + pending_results


def _print_report_summary(report: ComparisonReport) -> None:
    print(
        json.dumps(
            {
                "status": report.run_status,
                "target_id": report.target_id,
                "completed_architecture_runs": len(report.runs),
                "conclusion": report.conclusion,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


async def _run_locked(args: argparse.Namespace) -> int:
    started_at = datetime.now().astimezone()
    if args.output.exists() and not args.resume:
        raise ComparisonError("output already exists; use --resume or a new output path")
    if args.resume and not args.output.exists():
        raise ComparisonError("resume requires an existing report")
    if args.resume and args.preflight_only:
        raise ComparisonError("preflight requires a separate output path")

    resume_report: ComparisonReport | None = None
    run_state: ComparisonRunState | None = None
    blind_secret: bytes | None = None
    target = None
    snapshot = None
    state_path = _state_path(args.output)

    if args.resume:
        source_report = _read_report(args.output)
        if source_report.target_id != args.target:
            raise ComparisonError("existing comparison report target does not match --target")
        config = load_comparison_live_config(args.config)
        target = select_comparison_live_target(config, args.target)
        snapshot = load_comparison_pricing_snapshot(
            args.pricing_dir / f"{target.pricing_snapshot_id}.yaml"
        )
        canonical = validate_comparison_live_resume_report(
            source_report,
            manifest_path=args.manifest,
            rubric_path=args.rubric,
            target=target,
            pricing_snapshot=snapshot,
            judge_basis=_RUNNER_JUDGE_BASIS,
        )
        if state_path.exists():
            run_state = _load_run_state(state_path)
            if (
                run_state.target_id != target.target_id
                or run_state.judge_basis != _RUNNER_JUDGE_BASIS
                or run_state.frozen_binding != canonical.frozen_binding
            ):
                raise ComparisonError(
                    "comparison Live restricted state does not match the frozen target"
                )
        else:
            if canonical.frozen_binding is None:
                raise ComparisonError("comparison Live report has no frozen binding")
            run_state = _new_run_state(
                target_id=target.target_id,
                frozen_binding=canonical.frozen_binding,
                now=started_at,
            )
            _write_run_state(state_path, run_state)
        _assert_no_in_flight(run_state.in_flight)
        blind_secret = _decode_blind_secret(run_state)
        combined_runs = _merge_pending_results(canonical.runs, run_state.pending_results)
        migrated_runs = reblind_comparison_runs(
            combined_runs,
            order=canonical.execution_order,
            blind_secret=blind_secret,
        )
        canonical = rebuild_comparison_live_report(
            manifest_path=args.manifest,
            rubric_path=args.rubric,
            target=target,
            pricing_snapshot=snapshot,
            runs=migrated_runs,
            judge_basis=_RUNNER_JUDGE_BASIS,
        )
        resume_report = _publish_report_bundle(args.output, canonical, blind_secret=blind_secret)
        if run_state.pending_results:
            run_state = run_state.model_copy(update={"pending_results": ()})
            _write_run_state(state_path, run_state)
        if resume_report.run_status == "completed":
            _print_report_summary(resume_report)
            return 0

    live_environ = dict(os.environ)
    if args.target == _CODEX_SUBSCRIPTION_TARGET and _codex_chatgpt_auth_ready():
        live_environ["ORIA_CODEX_CHATGPT_AUTH_READY"] = "1"
    preflight = preflight_comparison_live(
        config_path=args.config,
        manifest_path=args.manifest,
        rubric_path=args.rubric,
        pricing_dir=args.pricing_dir,
        target_id=args.target,
        environ=live_environ,
        now=started_at,
        known_targets=_KNOWN_TARGETS,
    )
    if preflight.status == "blocked" or args.preflight_only:
        if args.preflight_only:
            _write_json(args.output, preflight)
        else:
            _record_failure(args.output, preflight)
        print(preflight.model_dump_json())
        return 0 if preflight.status == "ready" else 2

    if target is None or snapshot is None:
        config = load_comparison_live_config(args.config)
        target = select_comparison_live_target(config, args.target)
        snapshot = load_comparison_pricing_snapshot(
            args.pricing_dir / f"{target.pricing_snapshot_id}.yaml"
        )
    if run_state is None:
        initial_report = rebuild_comparison_live_report(
            manifest_path=args.manifest,
            rubric_path=args.rubric,
            target=target,
            pricing_snapshot=snapshot,
            runs=(),
            judge_basis=_RUNNER_JUDGE_BASIS,
        )
        if initial_report.frozen_binding is None:
            raise ComparisonError("comparison Live report has no frozen binding")
        run_state = _new_run_state(
            target_id=target.target_id,
            frozen_binding=initial_report.frozen_binding,
            now=started_at,
        )
        blind_secret = _decode_blind_secret(run_state)
        _publish_report_bundle(args.output, initial_report, blind_secret=blind_secret)
        _write_run_state(state_path, run_state)
    if blind_secret is None:
        blind_secret = _decode_blind_secret(run_state)

    state_box = [run_state]

    def reserve_slot(slot: ComparisonExecutionSlot) -> None:
        state_box[0] = _reserve_run_state(state_path, state_box[0], slot)

    def complete_slot(slot: ComparisonExecutionSlot, result: ArchitectureRunResult) -> None:
        state_box[0] = _complete_run_state(
            state_path,
            state_box[0],
            slot,
            result,
        )

    run_data_dir = args.data_dir / "runs" / started_at.strftime("%Y%m%dT%H%M%S%f%z")
    runtime_environ = dict(live_environ)
    runtime_environ.update({"ORIA_ENVIRONMENT": "test", "ORIA_EMBEDDING_PROFILE": "fixture"})
    resolved = resolve_runtime_config(
        runtime_profile=target.runtime_profile,
        llm_profile=target.target_id,
        embedding_profile=target.embedding_profile,
        data_dir=run_data_dir / "runtime",
        environ=runtime_environ,
    )
    await initialize_data(resolved)
    runtime = await build_runtime(resolved)
    async with runtime:
        report = await run_comparison_live(
            args.manifest,
            rubric_path=args.rubric,
            base_runtime=runtime,
            target=target,
            data_dir=run_data_dir,
            pricing_snapshot=snapshot,
            resume_runs=() if resume_report is None else resume_report.runs,
            max_new_case_runs=args.max_new_case_runs,
            judge_basis=_RUNNER_JUDGE_BASIS,
            blind_secret=blind_secret,
            on_slot_reserved=reserve_slot,
            on_slot_completed=complete_slot,
        )
    report = _publish_report_bundle(args.output, report, blind_secret=blind_secret)
    final_state = state_box[0]
    if final_state.in_flight is not None:
        raise ComparisonError("comparison Live returned with an unresolved reservation")
    if final_state.pending_results:
        final_state = final_state.model_copy(update={"pending_results": ()})
        _write_run_state(state_path, final_state)
    _print_report_summary(report)
    return 0


async def _run(args: argparse.Namespace) -> int:
    if not args.preflight_only and not args.run_live:
        raise ComparisonError("Live execution requires --run-live and an approved target/budget")
    with _exclusive_run_lock(args.output):
        return await _run_locked(args)


def main() -> int:
    args = _arguments()
    try:
        return asyncio.run(_run(args))
    except Exception as exc:
        failed = {
            "schema_version": 1,
            "suite": "attribution_comparison",
            "target_id": args.target,
            "status": "failed",
            "request_count": None,
            "reason": str(exc) if isinstance(exc, ComparisonError) else type(exc).__name__,
        }
        if isinstance(exc, InFlightReservationError):
            failed.update(exc.evidence)
        _record_failure(args.output, failed)
        print(json.dumps(failed, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
