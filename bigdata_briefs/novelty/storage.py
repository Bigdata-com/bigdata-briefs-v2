from abc import ABC, abstractmethod
from datetime import datetime

from sqlalchemy import and_, or_, func
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from bigdata_briefs.novelty.models import BulletPointEmbedding, ChunkTextHash, GeneratedBulletPoint
from bigdata_briefs.novelty.sql_models import (
    SQLBulletPointEmbedding,
    SQLChunkTextHash,
    SQLGeneratedBulletPoint,
)


def _embedding_is_present(embedding: list[float] | None) -> bool:
    return embedding is not None and len(embedding) > 0


def _historical_retrieve_condition():
    """Rows with a vector and not explicitly marked status_embedding=False (legacy rows use NULL)."""
    return and_(
        SQLBulletPointEmbedding.embedding.isnot(None),
        or_(
            SQLBulletPointEmbedding.status_embedding.is_(True),
            SQLBulletPointEmbedding.status_embedding.is_(None),
        ),
    )


class EmbeddingStorage(ABC):
    @abstractmethod
    def retrieve(
        self, entity_id: str, *, start_date: datetime, end_date: datetime
    ) -> list[BulletPointEmbedding]: ...

    @abstractmethod
    def store(self, data: list[BulletPointEmbedding]): ...


def _row_to_bullet_embedding(r: SQLBulletPointEmbedding, entity_id: str) -> BulletPointEmbedding:
    return BulletPointEmbedding(
        date=r.date,
        entity_id=entity_id,
        embedding=r.embedding or [],
        original_text=r.original_text,
        pre_rewrite_text=getattr(r, "pre_rewrite_text", None),
        status=r.status,
        novelty=getattr(r, "novelty", None),
        evaluator_details=r.evaluator_details,
        earnings_call_date=getattr(r, "earnings_call_date", None),
        added_past_evidence_from=getattr(r, "added_past_evidence_from", None),
        status_novelty_check_bigdata=getattr(r, "status_novelty_check_bigdata", None),
        status_embedding=getattr(r, "status_embedding", None),
        report_window_start=getattr(r, "report_window_start", None),
        report_window_end=getattr(r, "report_window_end", None),
    )


class SQLiteEmbeddingStorage(EmbeddingStorage):
    def __init__(self, engine: Engine):
        self.engine = engine

    def retrieve(
        self, entity_id: str, start_date: datetime, end_date: datetime
    ) -> list[BulletPointEmbedding]:
        historical = _historical_retrieve_condition()
        with Session(self.engine) as session:
            results = session.exec(
                select(SQLBulletPointEmbedding).where(
                    SQLBulletPointEmbedding.entity_id == entity_id,
                    SQLBulletPointEmbedding.date >= start_date,
                    SQLBulletPointEmbedding.date <= end_date,
                    historical,
                )
            ).all()
            return [_row_to_bullet_embedding(r, entity_id) for r in results]

    def get_min_date(self, entity_id: str) -> datetime | None:
        """Return the earliest stored bullet date for this entity, or None if none."""
        historical = _historical_retrieve_condition()
        with Session(self.engine) as session:
            stmt = select(func.min(SQLBulletPointEmbedding.date)).where(
                SQLBulletPointEmbedding.entity_id == entity_id,
                historical,
            )
            return session.scalar(stmt)

    def store(self, data: list[BulletPointEmbedding]):
        with Session(self.engine) as session:
            for bp_embedding in data:
                emb = bp_embedding.embedding
                status_emb = getattr(bp_embedding, "status_embedding", None)
                if status_emb is None:
                    status_emb = _embedding_is_present(emb)
                sql_embedding = SQLBulletPointEmbedding(
                    entity_id=bp_embedding.entity_id,
                    date=bp_embedding.date,
                    embedding=emb,
                    original_text=bp_embedding.original_text,
                    pre_rewrite_text=getattr(bp_embedding, "pre_rewrite_text", None),
                    status=getattr(bp_embedding, "status", None),
                    novelty=getattr(bp_embedding, "novelty", None),
                    evaluator_details=getattr(bp_embedding, "evaluator_details", None),
                    earnings_call_date=getattr(bp_embedding, "earnings_call_date", None),
                    added_past_evidence_from=getattr(
                        bp_embedding, "added_past_evidence_from", None
                    ),
                    status_novelty_check_bigdata=getattr(
                        bp_embedding, "status_novelty_check_bigdata", None
                    ),
                    status_embedding=status_emb,
                    report_window_start=getattr(bp_embedding, "report_window_start", None),
                    report_window_end=getattr(bp_embedding, "report_window_end", None),
                )
                session.add(sql_embedding)
            session.commit()

    def store_all_bullets(
        self,
        entity_id: str,
        date: datetime,
        bullets: list[tuple[str, list[float] | None, str, list[dict] | None, str | None]],
        earnings_call_date: str | None = None,
        *,
        report_window_start: datetime | None = None,
        report_window_end: datetime | None = None,
    ) -> None:
        """Persist bullets with status, novelty (derived), optional evaluator_details, pre_rewrite_text,
        and earnings_call_date (debug mode).

        Each tuple: (original_text, embedding, status, evaluator_details, pre_rewrite_text).
        pre_rewrite_text is the attempted rewrite text for rewrite-path discards, None otherwise.
        """
        with Session(self.engine) as session:
            for row in bullets:
                original_text, embedding, status, evaluator_details = row[0], row[1], row[2], row[3]
                pre_rewrite_text: str | None = row[4] if len(row) > 4 else None  # type: ignore[misc]
                novelty = status in ("keep", "rewrite")
                sql_embedding = SQLBulletPointEmbedding(
                    entity_id=entity_id,
                    date=date,
                    embedding=embedding,
                    original_text=original_text,
                    pre_rewrite_text=pre_rewrite_text,
                    status=status,
                    novelty=novelty,
                    evaluator_details=evaluator_details,
                    earnings_call_date=earnings_call_date,
                    status_embedding=_embedding_is_present(embedding),
                    report_window_start=report_window_start,
                    report_window_end=report_window_end,
                )
                session.add(sql_embedding)
            session.commit()


class SQLiteGeneratedBulletPointStorage:
    """
    Storage for the final bullet points that made it into the report for a given run.
    No embeddings are stored here — link back to `sqlbulletpointembedding` via `trace_id`.
    """

    def __init__(self, engine: Engine):
        self.engine = engine

    def store(self, data: list[GeneratedBulletPoint]) -> None:
        if not data:
            return
        with Session(self.engine) as session:
            for bp in data:
                session.add(
                    SQLGeneratedBulletPoint(
                        run_id=bp.run_id,
                        entity_id=bp.entity_id,
                        entity_name=bp.entity_name,
                        report_window_start=bp.report_window_start,
                        report_window_end=bp.report_window_end,
                        created_at=bp.created_at,
                        trace_id=bp.trace_id,
                        text=bp.text,
                        citations=[c.model_dump() for c in bp.citations] if bp.citations else None,
                        embedding_decision=bp.embedding_decision,
                        search_action=bp.search_action,
                        is_novel=bp.is_novel,
                    )
                )
            session.commit()

    def get_all_runs_bullets(
        self, entity_id: str
    ) -> dict[str, list[SQLGeneratedBulletPoint]]:
        """
        Return ALL bullet points for the entity, grouped by run_id.

        The returned dict is ordered newest-run-first (by ``created_at`` of the
        first bullet in each run).  Keys are ``run_id`` strings; values are the
        list of bullets for that run (insertion order within the run).

        Returns an empty dict if no data exists for the entity.
        """
        with Session(self.engine) as session:
            rows = session.exec(
                select(SQLGeneratedBulletPoint)
                .where(SQLGeneratedBulletPoint.entity_id == entity_id)
                .order_by(
                    SQLGeneratedBulletPoint.created_at.desc(),
                    SQLGeneratedBulletPoint.run_id,
                )
            ).all()

            if not rows:
                return {}

            session.expunge_all()

        # Group by run_id preserving the order already established by the query
        grouped: dict[str, list[SQLGeneratedBulletPoint]] = {}
        for row in rows:
            grouped.setdefault(row.run_id, []).append(row)
        return grouped

    def get_latest_run_bullets(
        self, entity_id: str
    ) -> list[SQLGeneratedBulletPoint]:
        """
        Return all bullet points from the latest run for the given entity.

        "Latest run" is determined by the maximum ``created_at`` among all rows
        for that entity; all rows sharing that ``run_id`` are returned.
        Returns an empty list if no data exists for the entity.
        """
        with Session(self.engine) as session:
            latest_created_at = session.exec(
                select(func.max(SQLGeneratedBulletPoint.created_at)).where(
                    SQLGeneratedBulletPoint.entity_id == entity_id
                )
            ).one()

            if latest_created_at is None:
                return []

            # Resolve the run_id for that timestamp (handles edge-case ties)
            latest_run_id = session.exec(
                select(SQLGeneratedBulletPoint.run_id)
                .where(SQLGeneratedBulletPoint.entity_id == entity_id)
                .where(SQLGeneratedBulletPoint.created_at == latest_created_at)
                .limit(1)
            ).one()

            rows = session.exec(
                select(SQLGeneratedBulletPoint)
                .where(SQLGeneratedBulletPoint.run_id == latest_run_id)
                .order_by(SQLGeneratedBulletPoint.created_at)
            ).all()

            # Detach from session so the list is usable after session closes
            session.expunge_all()
            return list(rows)


class ChunkHashStorage:
    """
    Storage for chunk text hashes used to filter already-seen content across runs.
    
    Stores SHA256 hashes of chunk texts, keyed by entity_id and date,
    allowing efficient lookup of previously used chunks.
    """
    
    def __init__(self, engine: Engine):
        self.engine = engine

    def retrieve_hashes(
        self, entity_id: str, start_date: datetime, end_date: datetime
    ) -> set[str]:
        """
        Retrieve all chunk text hashes for an entity within a date range.
        
        Returns:
            Set of SHA256 hashes that were used in previous runs.
        """
        with Session(self.engine) as session:
            results = session.exec(
                select(SQLChunkTextHash).where(
                    SQLChunkTextHash.entity_id == entity_id,
                    SQLChunkTextHash.date >= start_date,
                    SQLChunkTextHash.date <= end_date,
                )
            ).all()
            return {r.text_hash for r in results}

    def get_existing_chunk_keys_for_date(
        self, entity_id: str, date: datetime
    ) -> set[str]:
        """
        Get chunk keys that already exist for a specific entity and date.
        
        Used to avoid storing duplicate chunk_keys when re-running the same day.
        
        Returns:
            Set of chunk_keys already stored for this entity+date.
        """
        with Session(self.engine) as session:
            results = session.exec(
                select(SQLChunkTextHash).where(
                    SQLChunkTextHash.entity_id == entity_id,
                    SQLChunkTextHash.date == date,
                )
            ).all()
            return {r.chunk_key for r in results}

    def store_hashes(self, data: list[ChunkTextHash], skip_existing: bool = True):
        """
        Store new chunk text hashes.
        
        Args:
            data: List of ChunkTextHash objects to store.
            skip_existing: If True, skip storing hashes where chunk_key already
                           exists for the same entity+date (to allow re-runs).
        """
        if not data:
            return
        
        with Session(self.engine) as session:
            if skip_existing:
                # Group by entity_id+date to query existing chunk_keys
                from collections import defaultdict
                by_entity_date: dict[tuple[str, datetime], list[ChunkTextHash]] = defaultdict(list)
                for chunk_hash in data:
                    key = (chunk_hash.entity_id, chunk_hash.date)
                    by_entity_date[key].append(chunk_hash)
                
                stored_count = 0
                for (entity_id, date), hashes in by_entity_date.items():
                    # Get existing chunk_keys for this entity+date
                    existing = self.get_existing_chunk_keys_for_date(entity_id, date)
                    
                    for chunk_hash in hashes:
                        if chunk_hash.chunk_key not in existing:
                            sql_hash = SQLChunkTextHash(
                                entity_id=chunk_hash.entity_id,
                                date=chunk_hash.date,
                                text_hash=chunk_hash.text_hash,
                                chunk_key=chunk_hash.chunk_key,
                            )
                            session.add(sql_hash)
                            stored_count += 1
            else:
                # Store all without checking
                for chunk_hash in data:
                    sql_hash = SQLChunkTextHash(
                        entity_id=chunk_hash.entity_id,
                        date=chunk_hash.date,
                        text_hash=chunk_hash.text_hash,
                        chunk_key=chunk_hash.chunk_key,
                    )
                    session.add(sql_hash)
                    
            session.commit()
