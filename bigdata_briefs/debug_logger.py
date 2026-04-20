import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from bigdata_briefs import logger

if TYPE_CHECKING:
    from bigdata_briefs.models import RetrievedSources


def _normalize_citation_lookup_key(ref_id: str) -> str:
    """Align with entity grounding: mapping keys omit the ``CQS:`` prefix."""
    if ref_id.startswith("CQS:"):
        return ref_id[4:]
    return ref_id


def _cited_bullet_source_rank_payload(
    source_mapping: "RetrievedSources",
    citation_lists: list[list[str]],
) -> dict[str, Any]:
    """
    Rank distribution over citation references on raw generated bullets (resolved via ``source_mapping``).
    """
    rank_counts: dict[int, int] = {}
    source_breakdown: dict[str, dict[str, int | str]] = {}
    total_refs = 0
    resolved = 0
    unresolved_samples: list[str] = []
    seen_unique: set[str] = set()

    for cites in citation_lists:
        for ref_id in cites:
            if not isinstance(ref_id, str) or not ref_id.strip():
                continue
            total_refs += 1
            key = _normalize_citation_lookup_key(ref_id.strip())
            if key in source_mapping:
                resolved += 1
                seen_unique.add(key)
                src = source_mapping[key]
                rank = int(src.source_rank or 0)
                rank_counts[rank] = rank_counts.get(rank, 0) + 1
                name = src.source_name or "unknown"
                if name not in source_breakdown:
                    source_breakdown[name] = {"rank": rank, "count": 0}
                source_breakdown[name]["count"] += 1
            elif len(unresolved_samples) < 25:
                unresolved_samples.append(ref_id)

    rank_with_pct: dict[str, dict[str, float | int]] = {}
    for rank, count in sorted(rank_counts.items()):
        pct = (count / resolved * 100.0) if resolved > 0 else 0.0
        rank_with_pct[f"RANK_{rank}"] = {"count": count, "percentage": round(pct, 1)}

    sorted_sources = dict(
        sorted(source_breakdown.items(), key=lambda x: x[1]["count"], reverse=True)
    )

    return {
        "description": (
            "Source ranks for chunk references cited on raw generated bullets "
            "(survivors after per-bullet relevance prefilter in the theme loop; "
            "same citation lists as raw_citations / checkpoints)."
        ),
        "citation_reference_total": total_refs,
        "resolved_to_source_mapping": resolved,
        "unresolved_reference_count": total_refs - resolved,
        "unresolved_samples": unresolved_samples,
        "unique_cited_chunk_keys": len(seen_unique),
        "rank_distribution": rank_with_pct,
        "sources": sorted_sources,
    }


class DebugLogger:
    """Centralized debug logging system for saving API queries and LLM outputs.
    
    Directory structure:
    debug_logs/
    └── {entity_name}_{date}_{request_id[:5]}/
        ├── comparison.json
        ├── step_metrics.json
        ├── source_rank_distribution.json  (pool + cited-by-bullets rank stats)
        ├── llm_concept_extraction.json
        └── {mode}/  (e.g., iterative_sequential_with_thematic_chunks)
            ├── concept_search_summary.json
            └── details/
                ├── 01_query/
                │   └── query_api_call.json
                ├── 02_bullet_generation/
                │   └── llm_entity_update_*.json
                ├── 02_relevance_check/
                │   └── llm_relevance_check_<Theme>_<n>.json (per-theme bullet relevance during generation)
                ├── 03_entity_grounding/
                │   ├── llm_entity_grounding_check_*.json
                │   └── llm_entity_grounding_rewrite.json
                ├── 04_novelty_check/
                │   ├── llm_novelty_embedding_*.json
                │   ├── llm_novelty_search_langgraph_batch.json (LangGraph batch output)
                │   ├── llm_novelty_search_skip.json (when LangGraph search is not run)
                │   └── llm_novelty_search_rewrite_relevance_check_*.json, …
                ├── 05_bullet_redundancy/
                │   ├── llm_redundancy_identify.json
                │   └── llm_redundancy_merge_*.json
                ├── 06_theme_consolidation/
                │   ├── llm_thematic_clustering.json
                │   └── llm_consolidate_group_*.json
                └── 07_standalone_validation/
                    ├── llm_standalone_analyze.json
                    ├── llm_standalone_merge_*.json
                    └── llm_standalone_rewrite_*.json
    """

    # Files that stay at mode root level (main summaries)
    ROOT_FILES = {"llm_concept_extraction", "concept_search_summary"}

    # Mapping: step_name pattern → (number, folder_name) for organized subfolders
    # Files are saved in details/{number}_{folder_name}/
    STEP_FOLDERS = {
        # 01 - Query API
        "api_call": ("01", "query"),
        
        # 02 - Bullet Generation (various modes)
        "entity_update": ("02", "bullet_generation"),
        # 02 - Relevance check during thematic / iterative bullet generation (per bullet, per theme)
        "relevance_check": ("02", "relevance_check"),
        
        # 03 - Entity Grounding
        "entity_grounding": ("03", "entity_grounding"),
        
        # 04 - Novelty Check (embedding path, search/LangGraph-adjacent LLM, rewrite relevance)
        "novelty_check": ("04", "novelty_check"),
        "novelty_embedding": ("04", "novelty_check"),
        "novelty_search": ("04", "novelty_check"),
        "novelty_search_evaluation": ("04", "novelty_check"),
        
        # 05 - Bullet Redundancy (first pass)
        "redundancy": ("05", "bullet_redundancy"),
        
        # 06 - Theme Consolidation (two patterns, same folder)
        "thematic_clustering": ("06", "theme_consolidation"),
        "consolidate_group": ("06", "theme_consolidation"),
        
        # 07 - Standalone Validation (second round cleanup)
        "standalone": ("07", "standalone_validation"),
    }

    @staticmethod
    def _sanitize_name(name: str) -> str:
        """Sanitize entity name for use in folder names."""
        import re
        # Replace spaces and special chars with underscore
        sanitized = re.sub(r'[^\w\-]', '_', name)
        # Remove consecutive underscores
        sanitized = re.sub(r'_+', '_', sanitized)
        # Remove leading/trailing underscores
        sanitized = sanitized.strip('_')
        # Limit length to avoid too long folder names
        return sanitized[:50] if len(sanitized) > 50 else sanitized

    def _get_step_folder(self, filename: str) -> str | None:
        """Determine the numbered subfolder for a file based on step name pattern.
        
        Args:
            filename: The filename (e.g., "llm_entity_grounding_check_0.json")
            
        Returns:
            Folder name like "01_query" or None if no match found.
        """
        base_name = filename.rsplit(".", 1)[0]  # Remove extension
        
        # Remove llm_ or query_ prefix to get the step part
        if base_name.startswith("llm_"):
            step_part = base_name[4:]  # Remove "llm_"
        elif base_name.startswith("query_"):
            step_part = base_name[6:]  # Remove "query_"
        else:
            step_part = base_name
        
        # Find matching pattern (check if step_part starts with any known pattern)
        for pattern, (num, folder_name) in self.STEP_FOLDERS.items():
            if step_part.startswith(pattern):
                return f"{num}_{folder_name}"
        
        return None  # No match, will use details/ directly

    def __init__(
        self, 
        request_id: UUID | None = None, 
        report_start_date: str | None = None, 
        report_end_date: str | None = None,
        entity_name: str | None = None,
        mode: str | None = None,
        base_dir: Path | str | None = None,
    ):
        self.request_id = request_id
        self.base_dir = Path(base_dir) if base_dir else Path("./debug_logs")
        self.mode = mode
        self._comparison_data = {}  # Store bullet points for comparison
        self._cost_data = {}  # Store cost breakdown
        
        if request_id:
            # Create descriptive directory name: EntityName_Date_ShortID
            short_id = str(request_id)[:5]
            
            # Build folder name components
            name_parts = []
            
            # Add entity name if provided
            if entity_name:
                name_parts.append(self._sanitize_name(entity_name))
            
            # Add date
            if report_start_date:
                # Extract just the date part (YYYY-MM-DD) from ISO format
                start_date = report_start_date.split('T')[0] if 'T' in report_start_date else report_start_date
                # Remove time part if present in format like "2025-11-19 00:00:00"
                start_date = start_date.split(' ')[0]
                name_parts.append(start_date)
            
            # Always add short ID for uniqueness
            name_parts.append(short_id)
            
            dir_name = "_".join(name_parts)
            
            self.debug_dir = self.base_dir / dir_name
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            
            # If mode is specified, create mode subfolder
            if mode:
                self.mode_dir = self.debug_dir / mode
                self.mode_dir.mkdir(parents=True, exist_ok=True)
                self.details_dir = self.mode_dir / "details"
                self.details_dir.mkdir(parents=True, exist_ok=True)
            else:
                self.mode_dir = self.debug_dir
                self.details_dir = self.debug_dir / "details"
                self.details_dir.mkdir(parents=True, exist_ok=True)
            
            logger.info(f"Debug logging enabled for request {short_id}", debug_dir=str(self.mode_dir))
        else:
            self.debug_dir = None
            self.mode_dir = None
            self.details_dir = None

    def set_mode(self, mode: str):
        """Set the current mode and create/switch to its subfolder."""
        if not self.debug_dir:
            return
        
        self.mode = mode
        self.mode_dir = self.debug_dir / mode
        self.mode_dir.mkdir(parents=True, exist_ok=True)
        self.details_dir = self.mode_dir / "details"
        self.details_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Debug mode switched to: {mode}")

    def _get_save_dir(self, filename: str) -> Path | None:
        """Determine which directory to save the file to.
        
        Files are organized into numbered subfolders within details/:
        - 01_query/
        - 02_bullet_generation/
        - 03_entity_grounding/
        - etc.
        """
        if not self.mode_dir:
            return None
        
        # Check if file should be at mode root level
        base_name = filename.rsplit(".", 1)[0]  # Remove extension
        for root_file in self.ROOT_FILES:
            if base_name == root_file or base_name.startswith(root_file):
                return self.mode_dir
        
        # Determine step subfolder based on filename pattern
        step_folder = self._get_step_folder(filename)
        if step_folder:
            subfolder = self.details_dir / step_folder
            subfolder.mkdir(parents=True, exist_ok=True)
            return subfolder
        
        # Fallback: save directly in details/ for unrecognized patterns
        return self.details_dir

    def _save_json(self, filename: str, data: dict, force_root: bool = False, use_main_dir: bool = False):
        """Save data as JSON file.
        
        Args:
            filename: Name of the file
            data: Data to save
            force_root: If True, save at mode root instead of details
            use_main_dir: If True, save at main debug_dir (not mode subfolder)
        """
        if use_main_dir:
            if not self.debug_dir:
                return
            save_dir = self.debug_dir
        else:
            if not self.mode_dir:
                return
            save_dir = self.mode_dir if force_root else self._get_save_dir(filename)
        
        if not save_dir:
            return
        filepath = save_dir / filename
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
        logger.debug(f"Saved debug file: {filepath}")

    def save_query_api(
        self,
        step: str,
        entity_id: str | None,
        endpoint: str,
        method: str,
        payload: dict,
        response: dict,
        status_code: int | None = None,
    ):
        """Save API query with payload and response in JSON format."""
        if not self.mode_dir:
            return

        # Ensure response is a dict
        if not isinstance(response, dict):
            response = {"raw_response": str(response)}

        timestamp = datetime.now().isoformat()
        base_filename = f"query_{step}"

        # JSON format only
        json_data = {
            "timestamp": timestamp,
            "step": step,
            "entity_id": entity_id,
            "endpoint": endpoint,
            "method": method,
            "payload": payload,
            "response": response,
            "status_code": status_code,
        }
        self._save_json(f"{base_filename}.json", json_data)

    def save_llm_call(
        self,
        step_name: str,
        model: str,
        system_prompt: str | list[dict] | None,
        user_messages: list[dict],
        response: dict | str,
        usage: dict | None = None,
        debug_metadata: dict | None = None,
    ):
        """Save LLM call with prompt and response in JSON format."""
        if not self.mode_dir:
            return

        timestamp = datetime.now().isoformat()
        base_filename = f"llm_{step_name}"

        # Normalize system prompt
        if isinstance(system_prompt, list):
            system_prompt_str = "\n".join([msg.get("content", "") for msg in system_prompt])
        elif isinstance(system_prompt, str):
            system_prompt_str = system_prompt
        else:
            system_prompt_str = None

        # Normalize response
        if isinstance(response, dict):
            response_dict = response
        else:
            response_dict = {"text": str(response)}

        # JSON format only
        json_data = {
            "timestamp": timestamp,
            "step": step_name,
            "model": model,
            "system_prompt": system_prompt_str,
            "user_messages": user_messages,
            "response": response_dict,
            "usage": usage or {},
        }
        
        # Add debug metadata if provided (e.g., similar bullets with scores)
        if debug_metadata:
            json_data["debug_metadata"] = debug_metadata
        
        self._save_json(f"{base_filename}.json", json_data)

    def save_novelty_search_langgraph_batch(
        self,
        *,
        entity_id: str,
        entity_name: str,
        reference_date: str,
        sentences: list[str],
        pipe_results: list[Any],
    ) -> None:
        """Persist novelty-via-search (LangGraph) outputs under ``04_novelty_check``."""
        if not self.mode_dir:
            return
        timestamp = datetime.now().isoformat()
        entries: list[dict[str, Any]] = []
        for idx, item in enumerate(pipe_results):
            preview = sentences[idx][:800] if idx < len(sentences) else ""
            if isinstance(item, BaseException):
                entries.append(
                    {
                        "bullet_index": idx,
                        "sentence_preview": preview,
                        "error": True,
                        "error_type": type(item).__name__,
                        "message": str(item),
                    }
                )
            elif isinstance(item, dict):
                entries.append(
                    {
                        "bullet_index": idx,
                        "sentence_preview": preview,
                        "error": False,
                        "langgraph_state": item,
                    }
                )
            else:
                entries.append(
                    {
                        "bullet_index": idx,
                        "sentence_preview": preview,
                        "error": True,
                        "unexpected_type": type(item).__name__,
                    }
                )
        payload: dict[str, Any] = {
            "timestamp": timestamp,
            "kind": "novelty_search_langgraph_batch",
            "entity_id": entity_id,
            "entity_name": entity_name,
            "reference_date": reference_date,
            "bullet_count": len(sentences),
            "results": entries,
        }
        self._save_json("llm_novelty_search_langgraph_batch.json", payload)

    def save_novelty_search_skip(
        self,
        *,
        entity_id: str,
        reason_code: str,
        detail: str,
        novelty_via_search_enabled: bool,
        novelty_via_search_importable: bool,
        search_eligible_count: int,
        embedding_eligible_count: int,
    ) -> None:
        """Record why novelty-via-search did not run (still under ``04_novelty_check``)."""
        if not self.mode_dir:
            return
        timestamp = datetime.now().isoformat()
        payload: dict[str, Any] = {
            "timestamp": timestamp,
            "kind": "novelty_via_search_skipped",
            "entity_id": entity_id,
            "reason_code": reason_code,
            "detail": detail,
            "novelty_via_search_enabled": novelty_via_search_enabled,
            "novelty_via_search_importable": novelty_via_search_importable,
            "search_eligible_bullet_count": search_eligible_count,
            "embedding_eligible_bullet_count": embedding_eligible_count,
        }
        self._save_json("llm_novelty_search_skip.json", payload)

    def save_llm_failure(
        self,
        step_name: str,
        model: str,
        error: str | Exception,
        *,
        raw_response: str | None = None,
        attempt: int | None = None,
        user_messages: list[dict] | None = None,
        debug_metadata: dict | None = None,
    ) -> None:
        """Save a failed LLM call so you can inspect what went wrong (parse error, None, etc.)."""
        if not self.mode_dir:
            return
        timestamp = datetime.now().isoformat()
        base_filename = f"llm_{step_name}_FAILED"
        error_str = str(error) if isinstance(error, Exception) else error
        # Try to extract raw response from Pydantic/validation errors
        raw = raw_response
        if raw is None and isinstance(error, Exception):
            raw = getattr(error, "input_value", None)
            if raw is None and hasattr(error, "errors") and callable(error.errors):
                errs = error.errors()
                if errs and isinstance(errs[0], dict):
                    raw = errs[0].get("input")
            if isinstance(raw, (list, dict)):
                import json as _json
                try:
                    raw = _json.dumps(raw, default=str)
                except Exception:
                    raw = str(raw)
            elif raw is not None and not isinstance(raw, str):
                raw = str(raw)
        json_data = {
            "timestamp": timestamp,
            "step": step_name,
            "model": model,
            "error": error_str,
            "error_type": type(error).__name__ if isinstance(error, Exception) else None,
        }
        if raw is not None:
            json_data["raw_response"] = raw
        if attempt is not None:
            json_data["attempt"] = attempt
        if user_messages:
            json_data["user_messages"] = user_messages
        if debug_metadata:
            json_data["debug_metadata"] = debug_metadata
        self._save_json(f"{base_filename}.json", json_data)

    def update_llm_call_with_discarded(
        self,
        step_name: str,
        discarded_bullets: list[dict],  # [{text, score}, ...]
    ):
        """Update an existing LLM call JSON file with discarded bullets info.
        
        Args:
            step_name: The step name used when saving the LLM call (e.g., "entity_update_iterative_theme_Acquisition")
            discarded_bullets: List of discarded bullets with their scores
        """
        if not self.details_dir:
            return
        
        if not discarded_bullets:
            return  # Don't update if nothing was discarded
        
        # Find the file - use _get_step_folder to determine correct subfolder
        filename = f"llm_{step_name}.json"
        step_folder = self._get_step_folder(filename)
        if step_folder:
            filepath = self.details_dir / step_folder / filename
        else:
            filepath = self.details_dir / filename
        
        if not filepath.exists():
            logger.warning(f"Cannot update LLM call file: {filepath} does not exist")
            return
        
        try:
            # Read existing file
            with open(filepath, 'r', encoding="utf-8") as f:
                data = json.load(f)

            # Add discarded bullets info
            data["discarded_by_relevance_prefilter"] = {
                "count": len(discarded_bullets),
                "bullets": discarded_bullets,
            }

            # Write back
            with open(filepath, 'w', encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                
        except Exception as e:
            logger.warning(f"Failed to update LLM call file {filepath}: {e}")

    def save_concept_search_summary(
        self,
        entity_name: str,
        concepts_data: list[dict],  # [{theme, concepts, results}, ...]
    ):
        """
        Save intermediate concept search data. Will be completed by save_concept_workflow_complete.
        """
        if not self.mode_dir:
            return
        
        # Store concepts_data for later use
        self._concepts_data = concepts_data
        self._entity_name = entity_name

    def save_concept_workflow_complete(
        self,
        entity_name: str,
        concepts_data: list[dict],
        bullet_points_kept: list[str],
        bullet_points_discarded: list[dict],  # [{bullet_point, relevance_score, reason}, ...]
        sources_mapping: dict,
    ):
        """
        Save the complete concept workflow with structure (JSON only):
        1. ALL Concepts (lists from LLM)
        2. BULLET POINTS - KEPT (final report)
        3. BULLET POINTS - DISCARDED (filtered out)
        4. ALL Chunks (by concept)
        """
        if not self.mode_dir:
            return

        timestamp = datetime.now().isoformat()
        
        # Build JSON data
        json_data = {
            "timestamp": timestamp,
            "entity": entity_name,
            "mode": self.mode,
            "concepts": [],
            "bullet_points": {
                "kept": bullet_points_kept,
                "discarded": bullet_points_discarded
            },
            "chunks_by_concept": []
        }
        
        # Process concepts
        for category_data in concepts_data:
            theme = category_data.get("theme", "Unknown")
            concepts_list = category_data.get("concepts", [])
            
            json_data["concepts"].append({
                "theme": theme,
                "concepts": concepts_list
            })
        
        # Process chunks by concept
        for category_data in concepts_data:
            theme = category_data.get("theme", "Unknown")
            
            for concept_result in category_data.get("results", []):
                concept = concept_result.get("concept", "Unknown")
                chunks = concept_result.get("chunks", [])
                
                concept_chunks_json = {
                    "theme": theme,
                    "concept": concept,
                    "chunk_count": len(chunks),
                    "chunks": []
                }
                
                for chunk in chunks:
                    simplified_chunk = {
                        "document_id": chunk.get("document_id", ""),
                        "headline": chunk.get("headline", ""),
                        "ts": chunk.get("ts", ""),
                        "source_name": chunk.get("source_name", ""),
                        "source_rank": chunk.get("source_rank", ""),
                        "text": chunk.get("text", ""),
                    }
                    concept_chunks_json["chunks"].append(simplified_chunk)
                
                json_data["chunks_by_concept"].append(concept_chunks_json)
        
        # Save JSON file only
        self._save_json("concept_search_summary.json", json_data)

    def add_mode_results(
        self, 
        mode: str, 
        stage: str,
        bullets: list[str], 
        discarded: list[dict] | None = None,
        extra_info: dict | None = None,
    ):
        """Add results for a mode at a specific stage to the comparison data.
        
        Args:
            mode: The workflow mode (e.g., 'iterative_sequential')
            stage: Pipeline stage: 'raw' | 'post_consolidation' | 'post_novelty' | 'final'
            bullets: List of bullet points at this stage
            discarded: Optional list of discarded bullets (for novelty stage)
            extra_info: Optional dict with additional stage-specific info
        """
        if mode not in self._comparison_data:
            self._comparison_data[mode] = {}
        
        stage_data = {
            "bullets": bullets,
            "count": len(bullets),
        }
        
        if discarded is not None:
            stage_data["discarded"] = discarded
            stage_data["count_discarded"] = len(discarded)
        
        if extra_info:
            stage_data.update(extra_info)
        
        self._comparison_data[mode][stage] = stage_data

    def save_source_rank_distribution(
        self,
        entity_name: str,
        rank_distribution: dict[int, int],
        total_unique_chunks: int,
        source_breakdown: dict[str, dict],  # {source_name: {rank, count}}
    ):
        """
        Save source rank distribution for unique chunks after concept search.
        
        Args:
            entity_name: Name of the entity
            rank_distribution: {rank: count} e.g. {1: 25, 2: 10, 3: 5}
            total_unique_chunks: Total number of unique chunks
            source_breakdown: Per-source details {source_name: {rank: X, count: Y}}
        """
        if not self.mode_dir:
            return
        
        timestamp = datetime.now().isoformat()
        
        # Calculate percentages
        rank_with_pct = {}
        for rank, count in sorted(rank_distribution.items()):
            pct = (count / total_unique_chunks * 100) if total_unique_chunks > 0 else 0
            rank_with_pct[f"RANK_{rank}"] = {
                "count": count,
                "percentage": round(pct, 1)
            }
        
        # Sort sources by count descending
        sorted_sources = dict(
            sorted(source_breakdown.items(), key=lambda x: x[1]["count"], reverse=True)
        )
        
        data = {
            "timestamp": timestamp,
            "entity": entity_name,
            "retrieved_chunk_pool_note": (
                "rank_distribution, sources, and total_unique_chunks cover every deduplicated chunk "
                "passed through concept search (after optional hash filter), before bullets cite a subset."
            ),
            "total_unique_chunks": total_unique_chunks,
            "rank_distribution": rank_with_pct,
            "sources": sorted_sources,
        }
        
        self._save_json("source_rank_distribution.json", data, force_root=True)
        
        # Also log a summary to terminal
        rank_summary = ", ".join([f"R{r}: {c}" for r, c in sorted(rank_distribution.items())])
        logger.info(
            f"Source rank distribution for {entity_name}: "
            f"{total_unique_chunks} unique chunks → {rank_summary}"
        )

    def merge_cited_ranks_into_source_rank_distribution(
        self,
        source_mapping: "RetrievedSources",
        citation_lists: list[list[str]],
    ) -> None:
        """
        Augment ``source_rank_distribution.json`` under ``debug_dir`` with rank stats for
        citations attached to raw generated bullets (after theme-loop relevance filter).

        The pool histogram is written during concept search (often before ``set_mode``), so
        the file lives at ``debug_dir``, not under ``mode_dir``.
        """
        if not self.debug_dir:
            return
        if not source_mapping:
            logger.debug("Skipping cited rank merge: empty or missing source_mapping")
            return
        if not citation_lists:
            logger.debug("Skipping cited rank merge: no citation lists")
            return

        path = self.debug_dir / "source_rank_distribution.json"
        if not path.is_file():
            logger.warning(
                "Cannot merge cited bullet ranks: %s not found (concept search may have skipped logging)",
                path,
            )
            return

        try:
            with path.open(encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Cannot read source_rank_distribution for cited merge: %s", e)
            return

        cited = _cited_bullet_source_rank_payload(source_mapping, citation_lists)
        data["cited_by_raw_generated_bullets"] = cited
        data["cited_merge_timestamp"] = datetime.now().isoformat()

        try:
            with path.open("w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False, default=str)
        except OSError as e:
            logger.warning("Cannot write cited ranks to source_rank_distribution: %s", e)
            return

        logger.info(
            "Merged cited-source rank stats: %s references (%s resolved, %s unique chunk keys)",
            cited["citation_reference_total"],
            cited["resolved_to_source_mapping"],
            cited["unique_cited_chunk_keys"],
        )

    def save_text_duplicates_within_concepts(
        self,
        entity_name: str,
        duplicates_by_concept: dict[str, list[dict]],
        total_duplicates: int,
    ):
        """
        Save log of text duplicates (same text, different document_id) within concepts.
        
        These are chunks that passed document_id+chunk_num deduplication but have
        identical text content from different source documents.
        
        Args:
            entity_name: Name of the entity
            duplicates_by_concept: {concept: [{text_preview, occurrences: [{doc_id, chunk_num, headline, source}]}]}
            total_duplicates: Total count of duplicate text occurrences
        """
        if not self.mode_dir or total_duplicates == 0:
            return
        
        timestamp = datetime.now().isoformat()
        
        data = {
            "timestamp": timestamp,
            "entity": entity_name,
            "total_text_duplicates": total_duplicates,
            "by_concept": duplicates_by_concept,
        }
        
        self._save_json("log_duplicates_within_concepts.json", data, force_root=True)
        
        logger.info(
            f"Text duplicates within concepts for {entity_name}: "
            f"{total_duplicates} duplicate occurrences across {len(duplicates_by_concept)} concepts"
        )

    def save_chunk_hash_filter_stats(
        self,
        entity_name: str,
        removed_count: int,
        stored_count: int,
        remaining_count: int,
        removed_chunks_details: list[dict] | None = None,
    ):
        """
        Save statistics about chunk hash filtering (already-used content detection).
        
        Args:
            entity_name: Name of the entity
            removed_count: Number of chunks removed (already used in previous runs)
            stored_count: Number of new chunk hashes stored for future runs
            remaining_count: Number of chunks remaining after filtering
            removed_chunks_details: List of dicts with metadata of removed chunks
        """
        if not self.debug_dir:
            return
        
        timestamp = datetime.now().isoformat()
        
        data = {
            "timestamp": timestamp,
            "entity": entity_name,
            "chunks_removed_already_used": removed_count,
            "new_hashes_stored": stored_count,
            "chunks_remaining": remaining_count,
            "removed_chunks_details": removed_chunks_details or [],
        }
        
        self._save_json("chunk_hash_filter_stats.json", data, force_root=True)
        
        logger.info(
            f"Chunk hash filter for {entity_name}: "
            f"{removed_count} removed (already used), {stored_count} new stored, {remaining_count} remaining"
        )

    def save_comparison(self, entity_name: str | None = None):
        """Save comparison.json with bullet points grouped by mode and stage.
        
        Structure:
        {
            "timestamp": "...",
            "entity": "...",
            "modes": {
                "iterative_sequential": {
                    "raw": {"bullets": [...], "count": N},
                    "post_consolidation": {"bullets": [...], "count": N, "consolidated_from": M},
                    "post_novelty": {"bullets": [...], "count": N, "discarded": [...]},
                    "final": {"bullets": [...], "count": N}
                },
                ...
            },
            "summary": {
                "total_modes_run": N,
                "mode_with_most_final_bullets": "...",
                "mode_with_least_final_bullets": "...",
                "stages_tracked": ["raw", "post_consolidation", ...]
            }
        }
        """
        if not self.debug_dir or not self._comparison_data:
            return
        
        timestamp = datetime.now().isoformat()
        
        comparison = {
            "timestamp": timestamp,
            "entity": entity_name,
            "modes": {},
            "summary": {}
        }
        
        max_final = -1
        min_final = float('inf')
        max_mode = None
        min_mode = None
        all_stages = set()
        
        for mode, stages_data in self._comparison_data.items():
            comparison["modes"][mode] = {}
            
            for stage, stage_data in stages_data.items():
                all_stages.add(stage)
                comparison["modes"][mode][stage] = stage_data
            
            # Use 'final' stage for comparison, fallback to 'raw' if not present
            final_data = stages_data.get("final") or stages_data.get("post_novelty") or stages_data.get("raw", {})
            final_count = final_data.get("count", 0)
            
            if final_count > max_final:
                max_final = final_count
                max_mode = mode
            if final_count < min_final:
                min_final = final_count
                min_mode = mode
        
        comparison["summary"] = {
            "total_modes_run": len(self._comparison_data),
            "mode_with_most_final_bullets": max_mode,
            "mode_with_least_final_bullets": min_mode,
            "stages_tracked": sorted(all_stages),
        }
        
        # Add cost breakdown if available
        if self._cost_data:
            comparison["summary"]["cost_breakdown"] = self._cost_data
        
        # Save at main debug_dir level (not in mode subfolder)
        self._save_json("comparison.json", comparison, use_main_dir=True)
        logger.info(f"Saved comparison.json with {len(self._comparison_data)} modes")

    def set_cost_breakdown(
        self,
        llm_cost_usd: float,
        embedding_cost_usd: float,
        total_cost_usd: float,
    ):
        """Set cost breakdown data to be included in comparison.json.
        
        Args:
            llm_cost_usd: Total cost of LLM calls in USD
            embedding_cost_usd: Total cost of embedding calls in USD
            total_cost_usd: Total combined cost in USD
        """
        self._cost_data = {
            "llm_cost_usd": round(llm_cost_usd, 6),
            "embedding_cost_usd": round(embedding_cost_usd, 6),
            "total_cost_usd": round(total_cost_usd, 6),
        }

    @staticmethod
    def save_global_cost_summary(
        request_id: str,
        llm_cost_usd: float,
        embedding_cost_usd: float,
        total_cost_usd: float,
        llm_tokens: int,
        embedding_tokens: int,
        llm_calls: int,
    ):
        """Save a global cost summary to the debug_logs folder.
        
        This is called once per brief generation (not per entity).
        Creates a file: debug_logs/cost_summary_{request_id[:8]}.json
        
        Args:
            request_id: The request UUID as string
            llm_cost_usd: Total cost of LLM calls in USD
            embedding_cost_usd: Total cost of embedding calls in USD
            total_cost_usd: Total combined cost in USD
            llm_tokens: Total LLM tokens used
            embedding_tokens: Total embedding tokens used
            llm_calls: Total number of LLM calls
        """
        base_dir = Path("./debug_logs")
        base_dir.mkdir(parents=True, exist_ok=True)
        
        short_id = request_id[:8] if len(request_id) >= 8 else request_id
        filename = f"cost_summary_{short_id}.json"
        filepath = base_dir / filename
        
        timestamp = datetime.now().isoformat()
        
        data = {
            "timestamp": timestamp,
            "request_id": request_id,
            "cost_breakdown": {
                "llm_cost_usd": round(llm_cost_usd, 6),
                "embedding_cost_usd": round(embedding_cost_usd, 6),
                "total_cost_usd": round(total_cost_usd, 6),
            },
            "usage": {
                "llm_tokens": llm_tokens,
                "embedding_tokens": embedding_tokens,
                "llm_calls": llm_calls,
            },
        }
        
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        
        logger.info(
            f"Saved cost summary: ${total_cost_usd:.4f} "
            f"(LLM: ${llm_cost_usd:.4f}, Embedding: ${embedding_cost_usd:.4f})",
            filepath=str(filepath),
        )

    @staticmethod
    def save_step_metrics(
        request_id: str,
        step_summary: dict[str, dict],
        totals: dict,
    ):
        """Save per-step metrics breakdown to the debug_logs folder.
        
        Creates a file: debug_logs/step_metrics_{request_id[:8]}.json
        
        Args:
            request_id: The request UUID as string
            step_summary: Dict of step_name -> {llm_cost_usd, llm_tokens, etc.}
            totals: Dict with total_cost_usd, total_duration_seconds, etc.
        """
        base_dir = Path("./debug_logs")
        base_dir.mkdir(parents=True, exist_ok=True)
        
        short_id = request_id[:8] if len(request_id) >= 8 else request_id
        filename = f"step_metrics_{short_id}.json"
        filepath = base_dir / filename
        
        timestamp = datetime.now().isoformat()
        
        data = {
            "timestamp": timestamp,
            "request_id": request_id,
            "steps": step_summary,
            "totals": totals,
        }
        
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        
        # Log summary to terminal
        step_count = len(step_summary)
        total_cost = totals.get("total_cost_usd", 0)
        total_duration = totals.get("total_duration_seconds", 0)
        
        logger.info(
            f"Saved step metrics: {step_count} steps, ${total_cost:.4f} total, {total_duration:.2f}s",
            filepath=str(filepath),
        )

    def save_entity_step_metrics(
        self,
        step_summary: dict[str, dict],
        totals: dict,
        entity_name: str | None = None,
        step_wall_timings: list[dict[str, object]] | None = None,
    ):
        """Save per-step metrics to the entity-specific folder.
        
        Creates a file: debug_logs/{entity_folder}/step_metrics.json
        
        Args:
            step_summary: Dict of step_name -> {llm_cost_usd, llm_tokens, etc.}
            totals: Dict with total_cost_usd, total_duration_seconds, etc.
            entity_name: Optional entity name to include in the output
            step_wall_timings: Wall-clock rows per pipeline step (and novelty substeps)
        """
        if not self.debug_dir:
            logger.warning("Cannot save entity step metrics: no debug directory set")
            return
        
        filepath = self.debug_dir / "step_metrics.json"
        
        timestamp = datetime.now().isoformat()
        
        data: dict[str, object] = {
            "timestamp": timestamp,
            "entity": entity_name,
            "request_id": str(self.request_id) if self.request_id else None,
            "steps": step_summary,
            "totals": totals,
        }
        if step_wall_timings is not None:
            data["step_wall_timings"] = step_wall_timings
        
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        
        # Log summary to terminal
        step_count = len(step_summary)
        total_cost = totals.get("total_cost_usd", 0)
        total_duration = totals.get("total_duration_seconds", 0)
        
        logger.info(
            f"Saved entity step metrics for {entity_name or 'unknown'}: "
            f"{step_count} steps, ${total_cost:.4f} total, {total_duration:.2f}s",
            filepath=str(filepath),
        )
