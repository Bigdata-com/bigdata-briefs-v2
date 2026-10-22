"""Tests for Bigdata retrieval-token pricing and its tracking."""
from unittest.mock import MagicMock

import pytest

from bigdata_briefs import pricing
from bigdata_briefs.metrics import EntityStepMetrics
from bigdata_briefs.pricing import (
    _usage_key_for_unit,
    calculate_retrieval_cost,
    calculate_search_cost,
    merge_usage,
    retrieval_tokens_from_usage,
)
from bigdata_briefs.query_service.api import APIQueryService

PRICES = {"web_tokens": 8e-6, "premium_news_tokens": 96e-6}


@pytest.fixture(autouse=True)
def _stub_prices(monkeypatch):
    monkeypatch.setattr(pricing, "get_token_prices", lambda: PRICES)


class TestRetrievalTokensFromUsage:
    def test_keeps_every_token_counter(self):
        usage = {"premium_news_tokens": 186, "web_tokens": 14}
        assert retrieval_tokens_from_usage(usage) == usage

    def test_ignores_non_token_fields(self):
        # Legacy field from before 2026-10-21; it is not a token count.
        usage = {"api_query_units": 1.5, "premium_news_tokens": 10}
        assert retrieval_tokens_from_usage(usage) == {"premium_news_tokens": 10}

    @pytest.mark.parametrize("usage", [None, {}, {"api_query_units": 2.0}])
    def test_missing_tokens_is_empty(self, usage):
        assert retrieval_tokens_from_usage(usage) == {}

    def test_merge_usage(self):
        assert merge_usage({"web_tokens": 1}, {"web_tokens": 2, "premium_news_tokens": 3}) == {
            "web_tokens": 3, "premium_news_tokens": 3,
        }


class TestPrices:
    @pytest.mark.parametrize("unit_id, key", [
        ("search:fast:content-premium-news:tokens", "premium_news_tokens"),
        ("search:fast:content-web:tokens", "web_tokens"),
        ("search:smart:content-web:tokens", None),
        ("search:volume::tokens", None),
    ])
    def test_unit_id_to_usage_key(self, unit_id, key):
        assert _usage_key_for_unit(unit_id) == key

    def test_cost_uses_each_tier_price(self):
        cost = calculate_retrieval_cost({"web_tokens": 1_000_000, "premium_news_tokens": 1_000_000})
        assert cost == pytest.approx(8.0 + 96.0)

    def test_unpriced_tier_costs_zero(self):
        assert calculate_retrieval_cost({"investment_research_bundle_tokens": 500}) == 0.0

    def test_recorded_token_cost_wins_over_chunks(self):
        assert calculate_search_cost(chunks=500, retrieval_cost_usd=2.0) == pytest.approx(2.0)

    def test_legacy_run_uses_chunk_price(self):
        assert calculate_search_cost(chunks=100, retrieval_cost_usd=None) == pytest.approx(0.15)


class TestApiSearchUsageTracking:
    def _service(self, response: dict) -> APIQueryService:
        svc = APIQueryService.__new__(APIQueryService)
        svc._api_key = "test"
        svc._call_api = MagicMock(return_value=response)
        return svc

    def test_reports_usage_to_callback(self):
        svc = self._service({"results": [], "usage": {"premium_news_tokens": 186}})
        seen: list[dict] = []
        svc.api_search("/v1/search", "POST", {}, on_usage=seen.append)
        assert seen == [{"premium_news_tokens": 186}]

    def test_usage_without_query_units_does_not_crash(self):
        # Regression: the account moved to token billing on 2026-10-21, usage
        # lost api_query_units and every search raised KeyError.
        svc = self._service({"results": [], "usage": {"premium_news_tokens": 186}})
        assert svc.api_search("/v1/search", "POST", {}) == []


def test_entity_metrics_accumulate_tokens_and_cost_per_step():
    m = EntityStepMetrics("ACME")
    m.track_search_usage({"web_tokens": 100}, attributee_step="exploratory_search")
    m.track_search_usage({"premium_news_tokens": 50}, attributee_step="exploratory_search")
    m.track_api_call(2, {"web_tokens": 30}, attributee_step="novelty_search_fetch")

    assert m._total_retrieval_tokens == 180
    assert m._retrieval_tokens_by_tier == {"web_tokens": 130, "premium_news_tokens": 50}
    assert m._total_retrieval_cost == pytest.approx(130 * 8e-6 + 50 * 96e-6)
    summary = m.get_step_summary()
    explo = summary["exploratory_search"]["operational"]
    assert explo["retrieval_tokens"] == 150
    assert explo["retrieval_cost_usd"] == pytest.approx(100 * 8e-6 + 50 * 96e-6)
    assert summary["novelty_search_fetch"]["operational"]["api_calls"] == 2
