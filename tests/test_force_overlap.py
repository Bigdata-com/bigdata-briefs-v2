"""Tests for the force_overlap parameter in run_entity_incremental."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlmodel import Session, SQLModel, create_engine

from bigdata_briefs.models import ReportDates
from bigdata_briefs.orchestration.entity_runner import (
    OrchestratorWindowOverlapError,
    _assert_no_overlapping_run,
)
from bigdata_briefs.orchestration.models import SQLEntityPipelineRunLog


@pytest.fixture
def mem_engine():
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    return engine


def _add_completed_run(
    session: Session,
    *,
    entity_id: str,
    window_start: datetime,
    window_end: datetime,
) -> None:
    import uuid
    session.add(
        SQLEntityPipelineRunLog(
            run_id=uuid.uuid4(),
            entity_id=entity_id,
            status="succeeded",
            report_window_start=window_start,
            report_window_end=window_end,
            process_started_at_utc=window_start,
            process_completed_at_utc=window_end,
        )
    )
    session.commit()


T = lambda s: datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


class TestAssertNoOverlappingRun:
    """Unit tests for _assert_no_overlapping_run."""

    def test_raises_when_window_overlaps_existing_run(self, mem_engine):
        with Session(mem_engine) as session:
            _add_completed_run(
                session,
                entity_id="ENT1",
                window_start=T("2025-01-22T10:00:00"),
                window_end=T("2025-01-22T18:00:00"),
            )

        report_dates = ReportDates(
            start=T("2025-01-22T10:00:00"),
            end=T("2025-01-22T18:00:00"),
        )
        with Session(mem_engine) as session:
            with pytest.raises(OrchestratorWindowOverlapError):
                _assert_no_overlapping_run(session, entity_id="ENT1", report_dates=report_dates)

    def test_raises_on_partial_overlap(self, mem_engine):
        with Session(mem_engine) as session:
            _add_completed_run(
                session,
                entity_id="ENT1",
                window_start=T("2025-01-22T10:00:00"),
                window_end=T("2025-01-22T18:00:00"),
            )

        # New window partially overlaps
        report_dates = ReportDates(
            start=T("2025-01-22T14:00:00"),
            end=T("2025-01-22T20:00:00"),
        )
        with Session(mem_engine) as session:
            with pytest.raises(OrchestratorWindowOverlapError):
                _assert_no_overlapping_run(session, entity_id="ENT1", report_dates=report_dates)

    def test_no_raise_when_window_is_adjacent(self, mem_engine):
        with Session(mem_engine) as session:
            _add_completed_run(
                session,
                entity_id="ENT1",
                window_start=T("2025-01-22T10:00:00"),
                window_end=T("2025-01-22T18:00:00"),
            )

        # New window starts exactly where the previous ended — no overlap
        report_dates = ReportDates(
            start=T("2025-01-22T18:00:00"),
            end=T("2025-01-22T23:00:00"),
        )
        with Session(mem_engine) as session:
            _assert_no_overlapping_run(session, entity_id="ENT1", report_dates=report_dates)

    def test_no_raise_for_different_entity(self, mem_engine):
        with Session(mem_engine) as session:
            _add_completed_run(
                session,
                entity_id="ENT1",
                window_start=T("2025-01-22T10:00:00"),
                window_end=T("2025-01-22T18:00:00"),
            )

        # Same window but different entity — should not raise
        report_dates = ReportDates(
            start=T("2025-01-22T10:00:00"),
            end=T("2025-01-22T18:00:00"),
        )
        with Session(mem_engine) as session:
            _assert_no_overlapping_run(session, entity_id="ENT2", report_dates=report_dates)


class TestForceOverlapSchema:
    """force_overlap is False by default and accepted by BatchRunRequest."""

    def test_default_is_false(self):
        from bigdata_briefs.api.schemas import BatchRunRequest
        req = BatchRunRequest()
        assert req.force_overlap is False

    def test_can_be_set_to_true(self):
        from bigdata_briefs.api.schemas import BatchRunRequest
        req = BatchRunRequest(force_overlap=True)
        assert req.force_overlap is True


class TestForceOverlapBypassesCheck:
    """When force_overlap=True, the overlap check is skipped in run_entity_incremental."""

    def test_overlap_check_skipped_when_force_overlap(self, mem_engine):
        from bigdata_briefs.orchestration import entity_runner

        called = []

        original = entity_runner._assert_no_overlapping_run

        def mock_assert(*args, **kwargs):
            called.append(True)
            return original(*args, **kwargs)

        with patch.object(entity_runner, "_assert_no_overlapping_run", side_effect=mock_assert):
            from bigdata_briefs.orchestration.entity_runner import run_entity_incremental
            from bigdata_briefs.orchestration.windows import WindowMode

            # Seed a completed run so there would be an overlap
            with Session(mem_engine) as session:
                _add_completed_run(
                    session,
                    entity_id="ENT1",
                    window_start=T("2025-01-22T10:00:00"),
                    window_end=T("2025-01-22T18:00:00"),
                )

            # With force_overlap=True the check should not be called
            with patch("bigdata_briefs.orchestration.entity_runner.build_runtime_dependencies"), \
                 patch("bigdata_briefs.orchestration.entity_runner._get_or_create_orch_row") as mock_orch, \
                 patch("bigdata_briefs.orchestration.entity_runner._finalize_stale_running"), \
                 patch("bigdata_briefs.orchestration.entity_runner._assert_no_active_run"), \
                 patch("bigdata_briefs.orchestration.entity_runner.resolve_entity_for_run") as mock_resolve:

                mock_orch.return_value = MagicMock(last_window_end=T("2025-01-22T18:00:00"))
                mock_resolve.side_effect = Exception("stop after overlap check")

                with pytest.raises(Exception, match="stop after overlap check"):
                    run_entity_incremental(
                        entity_id="ENT1",
                        pipeline_config={},
                        state_dir=MagicMock(),
                        force_window_start=T("2025-01-22T10:00:00"),
                        force_window_end=T("2025-01-22T18:00:00"),
                        force_overlap=True,
                        engine=mem_engine,
                    )

        # The overlap check should NOT have been called
        assert len(called) == 0

    def test_overlap_check_called_when_force_overlap_false(self, mem_engine):
        from bigdata_briefs.orchestration import entity_runner

        called = []

        def mock_assert(*args, **kwargs):
            called.append(True)
            raise OrchestratorWindowOverlapError("overlap")

        with patch.object(entity_runner, "_assert_no_overlapping_run", side_effect=mock_assert):
            from bigdata_briefs.orchestration.entity_runner import run_entity_incremental

            with patch("bigdata_briefs.orchestration.entity_runner.build_runtime_dependencies"), \
                 patch("bigdata_briefs.orchestration.entity_runner._get_or_create_orch_row") as mock_orch, \
                 patch("bigdata_briefs.orchestration.entity_runner._finalize_stale_running"), \
                 patch("bigdata_briefs.orchestration.entity_runner._assert_no_active_run"):

                mock_orch.return_value = MagicMock(last_window_end=T("2025-01-22T18:00:00"))

                result = run_entity_incremental(
                    entity_id="ENT1",
                    pipeline_config={},
                    state_dir=MagicMock(),
                    force_window_start=T("2025-01-22T10:00:00"),
                    force_window_end=T("2025-01-22T18:00:00"),
                    force_overlap=False,
                    engine=mem_engine,
                )

        # The overlap check SHOULD have been called and caused a failed result
        assert len(called) == 1
        assert result.success is False
