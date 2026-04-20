from concurrent.futures import as_completed
from concurrent.futures.thread import ThreadPoolExecutor
from uuid import UUID

from jinja2 import Template

from bigdata_briefs import logger
from bigdata_briefs.debug_logger import DebugLogger
from bigdata_briefs.llm_client import LLMClient
from bigdata_briefs.metrics import EntityStepMetrics
from bigdata_briefs.models import (
    ClusteringResult,
    ConceptExtraction,
    ConsolidatedBullet,
    ConsolidationMode,
    Entity,
    MergedBullet,
    ReportDates,
    Result,
    RewrittenBullet,
    StandaloneAction,
    StandaloneAnalysisPlan,
    ThematicGroup,
)
from bigdata_briefs.novelty.embedding_client import EmbeddingClient
from bigdata_briefs.novelty.novelty_service import NoveltyFilteringService
from bigdata_briefs.novelty.storage import EmbeddingStorage
from bigdata_briefs.prompts.prompt_loader import get_prompt_keys
from bigdata_briefs.prompts.user_prompts import (
    get_concept_extraction_user_prompt,
    get_consolidate_theme_user_prompt,
    get_redundancy_identify_user_prompt,
    get_redundancy_merge_user_prompt,
    get_redundancy_rewrite_user_prompt,
    get_standalone_analyze_user_prompt,
    get_standalone_merge_user_prompt,
    get_standalone_rewrite_user_prompt,
    get_thematic_clustering_user_prompt,
)
from bigdata_briefs.settings import settings
from bigdata_briefs.utils import log_performance, track_step


class BriefPipelineService:
    """Dependency container + LLM helpers used by post-processing graph nodes.

    Held by ``RuntimeDependencies`` so graph nodes can reach ``llm_client`` and
    ``novelty_filter_service``. The remaining methods are called directly by
    nodes in ``bigdata_briefs.graph.nodes`` (concept extraction + post-processing).
    """

    def __init__(
        self,
        llm_client: LLMClient,
        novelty_filter_service: NoveltyFilteringService,
    ):
        self.llm_client = llm_client
        self.novelty_filter_service = novelty_filter_service

    @log_performance
    def extract_concepts(
        self,
        entity: Entity,
        report_dates: ReportDates,
        results: list[Result],
        request_id: UUID | None = None,
        debug_logger: "DebugLogger | None" = None,
        entity_metrics: "EntityStepMetrics | None" = None,
    ) -> ConceptExtraction:
        """
        Extract thematic concepts from exploratory search results.
        Used in the concept-based workflow (when topics=["{entity}"]).
        """
        prompt_keys = get_prompt_keys("concept_extraction")
        user_prompt = get_concept_extraction_user_prompt(
            entity=entity,
            results=results,
            report_dates=report_dates,
            user_template=prompt_keys.user_template,
            response_format=f"{ConceptExtraction.model_json_schema()}",
        )
        messages = [
            {"role": "user", "content": user_prompt},
            {"role": "assistant", "content": "```json\n{"},
        ]

        concepts = self.llm_client.call_with_response_format(
            system=[{"role": "system", "content": prompt_keys.system_prompt}],
            messages=messages,
            text_format=ConceptExtraction,
            step_name="concept_extraction",
            debug_logger=debug_logger,
            entity_metrics=entity_metrics,
            **prompt_keys.llm_kwargs,
        )

        # Log extracted concepts
        total_concepts = sum(len(cat.concepts) for cat in concepts.categories)
        logger.info(
            f"Extracted {len(concepts.categories)} categories with {total_concepts} concepts for {entity.name}"
        )

        return concepts

    def _consolidate_bullets(
        self,
        bulletpoints: list[str],
        bullet_citations: list[list[str]],
        relevance_scores: list[int],
        entity: Entity,
        consolidation_mode: ConsolidationMode = ConsolidationMode.LOOSE,
        debug_logger: DebugLogger | None = None,
        entity_metrics: "EntityStepMetrics | None" = None,
    ) -> tuple[list[str], list[list[str]], list[int], list[str], list[list[str]], list[int]]:
        """
        Step 9: Theme Consolidation (core logic)

        Clusters bullets by theme and consolidates multi-bullet groups.
        Returns standalone bullets separately for Step 10 processing.

        Step 1: Cluster bullets by theme (1 LLM call)
        Step 2: Consolidate each group (parallel LLM calls)

        Returns:
            Tuple of (consolidated_bullets, consolidated_citations, consolidated_scores,
                     standalone_bullets, standalone_citations, standalone_scores)
        """
        if len(bulletpoints) < 2:
            return bulletpoints, bullet_citations, relevance_scores, [], [], []

        while len(bullet_citations) < len(bulletpoints):
            bullet_citations.append([])

        bullets_data = [
            {"text": text, "score": score, "index": idx, "citations": bullet_citations[idx]}
            for idx, (text, score) in enumerate(zip(bulletpoints, relevance_scores))
        ]

        clustering_result = self._cluster_bullets_by_theme(
            bullets_data, entity, consolidation_mode, debug_logger=debug_logger,
            entity_metrics=entity_metrics,
        )

        if not clustering_result or not clustering_result.thematic_groups:
            logger.info("No thematic groups identified, skipping consolidation")
            return bulletpoints, bullet_citations, relevance_scores, [], [], []

        groups_to_consolidate = [g for g in clustering_result.thematic_groups if len(g.indices) > 1]

        all_indices = set(range(len(bullets_data)))
        grouped_indices = set()
        for group in clustering_result.thematic_groups:
            grouped_indices.update(group.indices)
        standalone_indices = list(all_indices - grouped_indices)

        if not groups_to_consolidate:
            logger.info("No groups with multiple bullets, skipping consolidation")
            return bulletpoints, bullet_citations, relevance_scores, [], [], []

        logger.info(
            f"Consolidation: {len(groups_to_consolidate)} groups to consolidate, "
            f"{len(standalone_indices)} standalone bullets"
        )

        consolidated_bullets = []
        consolidated_citations = []
        consolidated_scores = []

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {
                executor.submit(
                    self._consolidate_group,
                    group,
                    [bullets_data[i] for i in group.indices],
                    entity,
                    consolidation_mode,
                    debug_logger,
                    entity_metrics,
                ): group
                for group in groups_to_consolidate
            }

            for future in as_completed(futures):
                group = futures[future]
                try:
                    consolidated_text, combined_citations = future.result()
                    if consolidated_text:
                        consolidated_bullets.append(consolidated_text)
                        consolidated_citations.append(combined_citations)
                        max_score = max(bullets_data[i]["score"] for i in group.indices)
                        consolidated_scores.append(max_score)
                    else:
                        for idx in group.indices:
                            consolidated_bullets.append(bullets_data[idx]["text"])
                            consolidated_citations.append(bullets_data[idx]["citations"])
                            consolidated_scores.append(bullets_data[idx]["score"])
                except Exception as e:
                    logger.warning(f"Consolidation failed for group with indices {group.indices}: {e}")
                    for idx in group.indices:
                        consolidated_bullets.append(bullets_data[idx]["text"])
                        consolidated_citations.append(bullets_data[idx]["citations"])
                        consolidated_scores.append(bullets_data[idx]["score"])

        for group in clustering_result.thematic_groups:
            if len(group.indices) == 1:
                idx = group.indices[0]
                if 0 <= idx < len(bullets_data):
                    consolidated_bullets.append(bullets_data[idx]["text"])
                    consolidated_citations.append(bullets_data[idx]["citations"])
                    consolidated_scores.append(bullets_data[idx]["score"])

        standalone_bullets = []
        standalone_citations = []
        standalone_scores = []
        for idx in standalone_indices:
            if 0 <= idx < len(bullets_data):
                standalone_bullets.append(bullets_data[idx]["text"])
                standalone_citations.append(bullets_data[idx]["citations"])
                standalone_scores.append(bullets_data[idx]["score"])

        logger.info(
            f"Theme consolidation complete: {len(consolidated_bullets)} consolidated/single, "
            f"{len(standalone_bullets)} standalone (to be processed in Step 10)"
        )

        return (
            consolidated_bullets, consolidated_citations, consolidated_scores,
            standalone_bullets, standalone_citations, standalone_scores,
        )

    def _apply_validation_bullet_redundancy(
        self,
        entity_report,
        entity: Entity,
        debug_logger: DebugLogger | None = None,
        entity_metrics: "EntityStepMetrics | None" = None,
    ):
        """
        Step 8: Validation Step: Bullet Point Redundancy

        Validates whether there are redundant bullet points with same specific data.
        Identifies and merges bullets containing the same specific information
        (numbers, companies, events) regardless of phrasing.

        Citations are managed via bullet_citations (already separate from text):
        - KEEP/REWRITE: preserve original citations
        - MERGED: combine citations from all merged bullets

        Returns:
            Tuple of (modified entity_report, original count if validated else None)
        """
        if len(entity_report.report_bulletpoints) <= 1:
            return entity_report, None

        with track_step("bullet_redundancy", entity_metrics):
            raw_count = len(entity_report.report_bulletpoints)
            logger.info(f"Step 8: Bullet Redundancy starting for {entity.name} with {raw_count} bullets")

            redundancy_plan = self._identify_redundant_bullets(
                entity_report.report_bulletpoints,
                entity.name,
                debug_logger=debug_logger,
                entity_metrics=entity_metrics,
            )

            if not redundancy_plan.actions:
                logger.info("Step 8: No redundant bullets found")
                return entity_report, None

            validated_bullets, validated_citations, validated_scores = self._apply_redundancy_validation(
                entity_report.report_bulletpoints,
                entity_report.bullet_citations,
                entity_report.relevance_score,
                redundancy_plan,
                entity.name,
                debug_logger=debug_logger,
                entity_metrics=entity_metrics,
            )

            entity_report.report_bulletpoints = validated_bullets
            entity_report.bullet_citations = validated_citations
            entity_report.relevance_score = validated_scores

            kept_count = len(validated_bullets)
            discarded_count = raw_count - kept_count
            entity_metrics.track_bullets(kept=kept_count, discarded=discarded_count)

            logger.info(f"Step 8 complete: {raw_count} → {len(validated_bullets)} bullets")
            return entity_report, raw_count

    def _identify_redundant_bullets(
        self,
        bullets: list[str],
        entity_name: str,
        debug_logger: DebugLogger | None = None,
        entity_metrics: "EntityStepMetrics | None" = None,
    ) -> StandaloneAnalysisPlan:
        """Step 8 Phase 1: Use LLM to identify which bullets contain redundant data."""
        prompt_keys = get_prompt_keys("redundancy_identify")

        system_template = Template(prompt_keys.system_prompt)
        system_prompt = system_template.render(entity_name=entity_name)
        user_prompt = get_redundancy_identify_user_prompt(
            entity_name=entity_name,
            bullets=bullets,
            user_template=prompt_keys.user_template,
            response_format=f"{StandaloneAnalysisPlan.model_json_schema()}",
        )

        messages = [
            {"role": "user", "content": user_prompt},
            {"role": "assistant", "content": "```json\n{"},
        ]

        try:
            result = self.llm_client.call_with_response_format(
                system=[{"role": "system", "content": system_prompt}],
                messages=messages,
                text_format=StandaloneAnalysisPlan,
                step_name="redundancy_identify",
                debug_logger=debug_logger,
                entity_metrics=entity_metrics,
                **prompt_keys.llm_kwargs,
            )
            logger.info(f"Step 8: Identified {len(result.actions)} redundancy actions")
            return result
        except Exception as e:
            logger.error(f"Step 8: Redundancy identification failed: {e}")
            return StandaloneAnalysisPlan(actions=[])

    def _apply_redundancy_validation(
        self,
        bullets: list[str],
        bullet_citations: list[list[str]],
        scores: list[int],
        plan: StandaloneAnalysisPlan,
        entity_name: str,
        debug_logger: DebugLogger | None = None,
        entity_metrics: "EntityStepMetrics | None" = None,
    ) -> tuple[list[str], list[list[str]], list[int]]:
        """Step 8 Phase 2: Apply redundancy validation (merge, rewrite, discard).

        Citations are managed programmatically (kept separate from text):
        - MERGED: combine citations from all merged bullets
        - REWRITE/KEEP: preserve original citations
        """
        while len(bullet_citations) < len(bullets):
            bullet_citations.append([])

        bullet_status: dict[int, str] = {i: "keep" for i in range(len(bullets))}
        merged_groups: list[set[int]] = []
        rewrite_info: dict[int, str] = {}

        for action in plan.actions:
            try:
                idx = int(action.index)
                if idx < 0 or idx >= len(bullets):
                    continue

                if action.action == StandaloneAction.DISCARDED:
                    bullet_status[idx] = "discarded"
                elif action.action == StandaloneAction.REWRITE:
                    bullet_status[idx] = "rewrite"
                    rewrite_info[idx] = action.rationale
                elif action.action == StandaloneAction.MERGED:
                    merge_indices = {idx}
                    for target_idx_str in action.merge_with:
                        try:
                            target_idx = int(target_idx_str)
                            if 0 <= target_idx < len(bullets):
                                merge_indices.add(target_idx)
                        except ValueError:
                            continue

                    if len(merge_indices) > 1:
                        for m_idx in merge_indices:
                            bullet_status[m_idx] = "merged"
                        merged_groups.append(merge_indices)
            except ValueError:
                continue

        merged_groups = self._consolidate_merge_groups(merged_groups)

        merged_bullets: dict[int, str] = {}
        merged_scores: dict[int, int] = {}
        merged_citations: dict[int, list[str]] = {}

        merge_counter = 0
        for group in merged_groups:
            group_list = sorted(group)
            first_idx = group_list[0]
            bullets_to_merge = [bullets[i] for i in group_list]

            merged_text = self._execute_redundancy_merge(
                bullets_to_merge, entity_name,
                debug_logger=debug_logger, merge_index=merge_counter,
                entity_metrics=entity_metrics,
            )
            merge_counter += 1
            merged_bullets[first_idx] = merged_text
            merged_scores[first_idx] = max(scores[i] for i in group_list)

            combined_refs = []
            for idx in group_list:
                combined_refs.extend(bullet_citations[idx])
            merged_citations[first_idx] = list(dict.fromkeys(combined_refs))

        rewritten_bullets: dict[int, str] = {}
        for idx, rationale in rewrite_info.items():
            rewritten = self._execute_redundancy_rewrite(
                bullets[idx], rationale, entity_name,
                debug_logger=debug_logger, rewrite_index=idx,
                entity_metrics=entity_metrics,
            )
            rewritten_bullets[idx] = rewritten

        validated_bullets = []
        validated_citations = []
        validated_scores = []
        processed_merge_indices: set[int] = set()

        for i, bullet in enumerate(bullets):
            status = bullet_status[i]

            if status == "discarded":
                continue
            elif status == "merged":
                if i in processed_merge_indices:
                    continue
                for group in merged_groups:
                    if i in group:
                        first_idx = min(group)
                        if first_idx in merged_bullets:
                            validated_bullets.append(merged_bullets[first_idx])
                            validated_citations.append(merged_citations.get(first_idx, []))
                            validated_scores.append(merged_scores[first_idx])
                        processed_merge_indices.update(group)
                        break
            elif status == "rewrite":
                if i in rewritten_bullets:
                    validated_bullets.append(rewritten_bullets[i])
                    validated_citations.append(bullet_citations[i])
                    validated_scores.append(scores[i])
                else:
                    validated_bullets.append(bullet)
                    validated_citations.append(bullet_citations[i])
                    validated_scores.append(scores[i])
            else:
                validated_bullets.append(bullet)
                validated_citations.append(bullet_citations[i])
                validated_scores.append(scores[i])

        return validated_bullets, validated_citations, validated_scores

    def _consolidate_merge_groups(self, groups: list[set[int]]) -> list[set[int]]:
        """Consolidate overlapping merge groups into single groups."""
        if not groups:
            return []

        consolidated = []
        for group in groups:
            merged = False
            for i, existing in enumerate(consolidated):
                if group & existing:
                    consolidated[i] = existing | group
                    merged = True
                    break
            if not merged:
                consolidated.append(group.copy())

        return consolidated

    def _execute_redundancy_merge(
        self,
        bullets_to_merge: list[str],
        entity_name: str,
        debug_logger: DebugLogger | None = None,
        merge_index: int = 0,
        entity_metrics: "EntityStepMetrics | None" = None,
    ) -> str:
        """Merge multiple redundant bullets into one."""
        prompt_keys = get_prompt_keys("redundancy_merge")

        system_template = Template(prompt_keys.system_prompt)
        system_prompt = system_template.render(entity_name=entity_name)
        user_prompt = get_redundancy_merge_user_prompt(
            entity_name=entity_name,
            bullets_to_merge=bullets_to_merge,
            user_template=prompt_keys.user_template,
            response_format=f"{MergedBullet.model_json_schema()}",
        )

        messages = [
            {"role": "user", "content": user_prompt},
            {"role": "assistant", "content": "```json\n{"},
        ]

        try:
            result = self.llm_client.call_with_response_format(
                system=[{"role": "system", "content": system_prompt}],
                messages=messages,
                text_format=MergedBullet,
                step_name=f"redundancy_merge_{merge_index}",
                debug_logger=debug_logger,
                entity_metrics=entity_metrics,
                **prompt_keys.llm_kwargs,
            )
            return result.merged_text
        except Exception as e:
            logger.error(f"Step 8: Redundancy merge failed: {e}")
            return bullets_to_merge[0]

    def _execute_redundancy_rewrite(
        self,
        original_bullet: str,
        rationale: str,
        entity_name: str,
        debug_logger: DebugLogger | None = None,
        rewrite_index: int = 0,
        entity_metrics: "EntityStepMetrics | None" = None,
    ) -> str:
        """Rewrite a bullet to remove redundant information."""
        prompt_keys = get_prompt_keys("redundancy_rewrite")

        system_template = Template(prompt_keys.system_prompt)
        system_prompt = system_template.render(entity_name=entity_name)
        user_prompt = get_redundancy_rewrite_user_prompt(
            entity_name=entity_name,
            original_bullet=original_bullet,
            rationale=rationale,
            user_template=prompt_keys.user_template,
            response_format=f"{RewrittenBullet.model_json_schema()}",
        )

        messages = [
            {"role": "user", "content": user_prompt},
            {"role": "assistant", "content": "```json\n{"},
        ]

        try:
            result = self.llm_client.call_with_response_format(
                system=[{"role": "system", "content": system_prompt}],
                messages=messages,
                text_format=RewrittenBullet,
                step_name=f"redundancy_rewrite_{rewrite_index}",
                debug_logger=debug_logger,
                entity_metrics=entity_metrics,
                **prompt_keys.llm_kwargs,
            )
            return result.rewritten_text
        except Exception as e:
            logger.error(f"Step 8: Redundancy rewrite failed: {e}")
            return original_bullet

    def _cluster_bullets_by_theme(
        self,
        bullets_data: list[dict],
        entity: Entity,
        consolidation_mode: ConsolidationMode = ConsolidationMode.LOOSE,
        debug_logger: DebugLogger | None = None,
        entity_metrics: "EntityStepMetrics | None" = None,
    ) -> ClusteringResult | None:
        """
        Step 1: Use LLM to identify thematic groups of bullet points.
        """
        prompt_name = "thematic_clustering" if consolidation_mode == ConsolidationMode.LOOSE else "thematic_clustering_aggressive"
        prompt_keys = get_prompt_keys(prompt_name)

        system_template = Template(prompt_keys.system_prompt)
        rendered_system_prompt = system_template.render(entity_name=entity.name)

        user_prompt = get_thematic_clustering_user_prompt(
            entity_name=entity.name,
            bullets=[{"text": b["text"]} for b in bullets_data],
            user_template=prompt_keys.user_template,
            response_format=f"{ClusteringResult.model_json_schema()}",
        )

        messages = [
            {"role": "user", "content": user_prompt},
            {"role": "assistant", "content": "```json\n{"},
        ]

        try:
            result = self.llm_client.call_with_response_format(
                system=[{"role": "system", "content": rendered_system_prompt}],
                messages=messages,
                text_format=ClusteringResult,
                step_name="thematic_clustering",
                debug_logger=debug_logger,
                entity_metrics=entity_metrics,
                **prompt_keys.llm_kwargs,
            )

            return result
        except Exception as e:
            logger.error(f"Clustering LLM call failed: {e}")
            return None

    def _consolidate_group(
        self,
        group: "ThematicGroup",
        group_bullets: list[dict],
        entity: Entity,
        consolidation_mode: ConsolidationMode = ConsolidationMode.LOOSE,
        debug_logger: DebugLogger | None = None,
        entity_metrics: "EntityStepMetrics | None" = None,
    ) -> tuple[str | None, list[str]]:
        """
        Step 2: Consolidate a single group of bullet points into one.

        Citations are managed programmatically: all citations from the group
        are combined and returned separately.
        """
        prompt_name = "consolidate_theme" if consolidation_mode == ConsolidationMode.LOOSE else "consolidate_theme_aggressive"
        prompt_keys = get_prompt_keys(prompt_name)

        system_template = Template(prompt_keys.system_prompt)
        rendered_system_prompt = system_template.render(entity_name=entity.name)

        all_citations: list[str] = []
        bullets_for_prompt = []

        for b in group_bullets:
            all_citations.extend(b.get("citations", []))
            bullets_for_prompt.append({"text": b["text"], "citations": ""})

        all_citations = list(dict.fromkeys(all_citations))

        user_prompt = get_consolidate_theme_user_prompt(
            entity_name=entity.name,
            rationale=group.consolidation_rationale,
            group_bullets=bullets_for_prompt,
            user_template=prompt_keys.user_template,
            response_format=f"{ConsolidatedBullet.model_json_schema()}",
        )

        messages = [
            {"role": "user", "content": user_prompt},
            {"role": "assistant", "content": "```json\n{"},
        ]

        step_name = f"consolidate_group_{'-'.join(str(i) for i in group.indices)}"
        try:
            result = self.llm_client.call_with_response_format(
                system=[{"role": "system", "content": rendered_system_prompt}],
                messages=messages,
                text_format=ConsolidatedBullet,
                step_name=step_name,
                debug_logger=debug_logger,
                entity_metrics=entity_metrics,
                **prompt_keys.llm_kwargs,
            )

            if result:
                return result.consolidated_text, all_citations
            return None, all_citations
        except Exception as e:
            logger.error(f"Consolidate LLM call failed for group {group.indices}: {e}")
            return None, all_citations

    def _validate_standalone_bullets(
        self,
        consolidated_bullets: list[str],
        consolidated_citations: list[list[str]],
        consolidated_scores: list[int],
        standalone_bullets: list[str],
        standalone_citations: list[list[str]],
        standalone_scores: list[int],
        entity: Entity,
        debug_logger: DebugLogger | None = None,
        entity_metrics: "EntityStepMetrics | None" = None,
    ) -> tuple[list[str], list[list[str]], list[int]]:
        """
        Step 10 core logic: validate standalone bullets against consolidated ones.

        Phase 1: LLM identifies actions (KEEP, MERGED, REWRITE, DISCARDED) for each bullet
        Phase 2: Execute actions in parallel (merge, rewrite)
        """
        if not standalone_bullets:
            return consolidated_bullets, consolidated_citations, consolidated_scores

        logger.info(
            f"Step 10: Validating {len(standalone_bullets)} standalone bullets "
            f"against {len(consolidated_bullets)} consolidated"
        )

        cleanup_plan = self._analyze_standalone_bullets(
            consolidated_bullets, standalone_bullets, entity,
            debug_logger=debug_logger,
            entity_metrics=entity_metrics,
        )

        if not cleanup_plan or not cleanup_plan.actions:
            logger.info("No cleanup actions identified, keeping all bullets")
            return (
                consolidated_bullets + standalone_bullets,
                consolidated_citations + standalone_citations,
                consolidated_scores + standalone_scores,
            )

        validated_bullets = list(consolidated_bullets)
        validated_citations = list(consolidated_citations)
        validated_scores = list(consolidated_scores)

        standalone_keep = set(range(len(standalone_bullets)))

        merge_actions = []
        rewrite_actions = []

        for action_item in cleanup_plan.actions:
            idx_str = action_item.index
            is_standalone = idx_str.startswith("S")
            idx = int(idx_str[1:])

            if action_item.action == StandaloneAction.DISCARDED:
                if is_standalone and idx in standalone_keep:
                    standalone_keep.discard(idx)
                    logger.info(f"Discarding standalone bullet S{idx}: {action_item.rationale[:50]}...")
            elif action_item.action == StandaloneAction.MERGED:
                if is_standalone and idx in standalone_keep and action_item.merge_with:
                    merge_actions.append((idx, action_item))
            elif action_item.action == StandaloneAction.REWRITE:
                if is_standalone and idx in standalone_keep:
                    rewrite_actions.append((idx, action_item))

        merged_results = {}
        indices_to_remove_from_consolidated = set()
        if merge_actions:
            with ThreadPoolExecutor(max_workers=5) as executor:
                futures = {}
                for idx, action_item in merge_actions:
                    bullets_to_merge = [standalone_bullets[idx]]
                    citations_to_merge = [standalone_citations[idx] if idx < len(standalone_citations) else []]
                    merge_indices = {"S": [idx], "C": []}

                    for target_str in action_item.merge_with:
                        if target_str.startswith("C"):
                            target_idx = int(target_str[1:])
                            if 0 <= target_idx < len(consolidated_bullets):
                                bullets_to_merge.append(consolidated_bullets[target_idx])
                                citations_to_merge.append(consolidated_citations[target_idx] if target_idx < len(consolidated_citations) else [])
                                merge_indices["C"].append(target_idx)
                        elif target_str.startswith("S"):
                            target_idx = int(target_str[1:])
                            if 0 <= target_idx < len(standalone_bullets) and target_idx in standalone_keep:
                                bullets_to_merge.append(standalone_bullets[target_idx])
                                citations_to_merge.append(standalone_citations[target_idx] if target_idx < len(standalone_citations) else [])
                                merge_indices["S"].append(target_idx)

                    if len(bullets_to_merge) > 1:
                        futures[executor.submit(
                            self._execute_merge_action,
                            bullets_to_merge,
                            citations_to_merge,
                            action_item.rationale,
                            entity,
                            debug_logger,
                            idx,
                            entity_metrics,
                        )] = (idx, merge_indices)

                for future in as_completed(futures):
                    idx, merge_indices = futures[future]
                    try:
                        merged_text, combined_citations = future.result()
                        if merged_text:
                            merged_results[idx] = (merged_text, combined_citations)
                            for s_idx in merge_indices["S"]:
                                standalone_keep.discard(s_idx)
                            for c_idx in merge_indices["C"]:
                                indices_to_remove_from_consolidated.add(c_idx)
                    except Exception as e:
                        logger.warning(f"Merge action failed for S{idx}: {e}")

        for idx, (merged_text, combined_citations) in merged_results.items():
            validated_bullets.append(merged_text)
            validated_citations.append(combined_citations)
            validated_scores.append(standalone_scores[idx])

        if indices_to_remove_from_consolidated:
            orig_cons_len = len(consolidated_bullets)
            validated_bullets = [b for i, b in enumerate(validated_bullets[:orig_cons_len])
                           if i not in indices_to_remove_from_consolidated] + validated_bullets[orig_cons_len:]
            validated_citations = [c for i, c in enumerate(validated_citations[:orig_cons_len])
                             if i not in indices_to_remove_from_consolidated] + validated_citations[orig_cons_len:]
            validated_scores = [s for i, s in enumerate(validated_scores[:orig_cons_len])
                          if i not in indices_to_remove_from_consolidated] + validated_scores[orig_cons_len:]

        rewritten_results = {}
        if rewrite_actions:
            with ThreadPoolExecutor(max_workers=5) as executor:
                futures = {
                    executor.submit(
                        self._execute_rewrite_action,
                        standalone_bullets[idx],
                        action_item.rationale,
                        entity,
                        debug_logger,
                        idx,
                        entity_metrics,
                    ): idx
                    for idx, action_item in rewrite_actions
                    if idx in standalone_keep
                }

                for future in as_completed(futures):
                    idx = futures[future]
                    try:
                        result = future.result()
                        if result:
                            rewritten_results[idx] = result
                    except Exception as e:
                        logger.warning(f"Rewrite action failed for S{idx}: {e}")

        for idx in sorted(standalone_keep):
            if idx in rewritten_results:
                validated_bullets.append(rewritten_results[idx])
            else:
                validated_bullets.append(standalone_bullets[idx])
            validated_citations.append(standalone_citations[idx] if idx < len(standalone_citations) else [])
            validated_scores.append(standalone_scores[idx])

        logger.info(
            f"Step 10 validation complete: {len(validated_bullets)} total "
            f"(merged: {len(merge_actions)}, rewritten: {len(rewrite_actions)}, "
            f"discarded: {len(standalone_bullets) - len(standalone_keep)})"
        )

        return validated_bullets, validated_citations, validated_scores

    def _analyze_standalone_bullets(
        self,
        consolidated_bullets: list[str],
        standalone_bullets: list[str],
        entity: Entity,
        debug_logger: DebugLogger | None = None,
        entity_metrics: "EntityStepMetrics | None" = None,
    ) -> StandaloneAnalysisPlan | None:
        """Step 10 Phase 1: Analyze standalone bullets to determine validation actions."""
        prompt_keys = get_prompt_keys("standalone_analyze")

        system_template = Template(prompt_keys.system_prompt)
        rendered_system_prompt = system_template.render(entity_name=entity.name)

        user_prompt = get_standalone_analyze_user_prompt(
            entity_name=entity.name,
            consolidated_bullets=consolidated_bullets,
            standalone_bullets=standalone_bullets,
            user_template=prompt_keys.user_template,
            response_format=f"{StandaloneAnalysisPlan.model_json_schema()}",
        )

        messages = [
            {"role": "user", "content": user_prompt},
            {"role": "assistant", "content": "```json\n{"},
        ]

        try:
            result = self.llm_client.call_with_response_format(
                system=[{"role": "system", "content": rendered_system_prompt}],
                messages=messages,
                text_format=StandaloneAnalysisPlan,
                step_name="standalone_analyze",
                debug_logger=debug_logger,
                entity_metrics=entity_metrics,
                **prompt_keys.llm_kwargs,
            )

            return result
        except Exception as e:
            logger.error(f"Cleanup identify LLM call failed: {e}")
            return None

    def _execute_merge_action(
        self,
        bullets_to_merge: list[str],
        citations_to_merge: list[list[str]],
        rationale: str,
        entity: Entity,
        debug_logger: DebugLogger | None = None,
        merge_index: int = 0,
        entity_metrics: "EntityStepMetrics | None" = None,
    ) -> tuple[str | None, list[str]]:
        """Execute a merge action: merge multiple bullets into one.

        Citations are managed programmatically: all citations from merged bullets
        are combined and returned separately.
        """
        prompt_keys = get_prompt_keys("standalone_merge")

        system_template = Template(prompt_keys.system_prompt)
        rendered_system_prompt = system_template.render(entity_name=entity.name)

        all_citations: list[str] = []
        for citations in citations_to_merge:
            all_citations.extend(citations)
        all_citations = list(dict.fromkeys(all_citations))

        user_prompt = get_standalone_merge_user_prompt(
            entity_name=entity.name,
            bullets_to_merge=bullets_to_merge,
            rationale=rationale,
            user_template=prompt_keys.user_template,
            response_format=f"{ConsolidatedBullet.model_json_schema()}",
        )

        messages = [
            {"role": "user", "content": user_prompt},
            {"role": "assistant", "content": "```json\n{"},
        ]

        try:
            result = self.llm_client.call_with_response_format(
                system=[{"role": "system", "content": rendered_system_prompt}],
                messages=messages,
                text_format=ConsolidatedBullet,
                step_name=f"standalone_merge_{merge_index}",
                debug_logger=debug_logger,
                entity_metrics=entity_metrics,
                **prompt_keys.llm_kwargs,
            )

            if result:
                return result.consolidated_text, all_citations
            return None, all_citations
        except Exception as e:
            logger.error(f"Cleanup merge LLM call failed: {e}")
            return None, all_citations

    def _execute_rewrite_action(
        self,
        original_bullet: str,
        rationale: str,
        entity: Entity,
        debug_logger: DebugLogger | None = None,
        rewrite_index: int = 0,
        entity_metrics: "EntityStepMetrics | None" = None,
    ) -> str | None:
        """Execute a rewrite action: remove redundant parts and keep only unique information.

        Text comes in without citations (already separated).
        Citations are preserved by the caller in _validate_standalone_bullets.
        """
        prompt_keys = get_prompt_keys("standalone_rewrite")

        system_template = Template(prompt_keys.system_prompt)
        rendered_system_prompt = system_template.render(entity_name=entity.name)

        user_prompt = get_standalone_rewrite_user_prompt(
            entity_name=entity.name,
            original_bullet=original_bullet,
            rationale=rationale,
            user_template=prompt_keys.user_template,
            response_format=f"{RewrittenBullet.model_json_schema()}",
        )

        messages = [
            {"role": "user", "content": user_prompt},
            {"role": "assistant", "content": "```json\n{"},
        ]

        try:
            result = self.llm_client.call_with_response_format(
                system=[{"role": "system", "content": rendered_system_prompt}],
                messages=messages,
                text_format=RewrittenBullet,
                step_name=f"standalone_rewrite_{rewrite_index}",
                debug_logger=debug_logger,
                entity_metrics=entity_metrics,
                **prompt_keys.llm_kwargs,
            )

            if result:
                return result.rewritten_text
            return None
        except Exception as e:
            logger.error(f"Cleanup rewrite LLM call failed: {e}")
            return None

    @classmethod
    def factory(cls, embedding_storage: EmbeddingStorage):
        embedding_client = EmbeddingClient(settings.NOVELTY_MODEL)
        llm_client = LLMClient()
        # Do NOT pre-build evaluators here — filter_by_novelty_llm / novelty_embedding_step builds them lazily
        # so it can also wire up the shared_judge needed for the two-step rewrite (Step 2).
        # Injecting evaluators without the judge would silently disable Step 2.
        novelty_filter_service = NoveltyFilteringService(embedding_client, embedding_storage)
        return cls(llm_client, novelty_filter_service)
