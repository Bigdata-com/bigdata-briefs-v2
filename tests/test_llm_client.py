from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel

from bigdata_briefs.llm_client import (
    LLMClient,
)
from bigdata_briefs.llm_client import (
    openai as llm_client_openai,
)
from bigdata_briefs.settings import settings
from bigdata_briefs.utils import time as utils_time


class DummyResponseFormat(BaseModel):
    result: str


@pytest.fixture
def mock_openai_client():
    """Mock OpenAI client for testing"""
    return MagicMock()


@pytest.fixture
def mock_llm_client(mock_openai_client):
    """LLM client with mocked OpenAI client"""
    return LLMClient(client=mock_openai_client)


@pytest.fixture
def mock_messages():
    """Sample messages for testing"""
    return [{"role": "user", "content": "Test message"}]


@pytest.fixture
def mock_system_message():
    """Sample system messages for testing"""
    return [{"role": "system", "content": "You are a helpful assistant"}]


def test_init_with_client(mock_openai_client):
    """Test initialization with provided client"""
    client = LLMClient(client=mock_openai_client)
    assert client.client == mock_openai_client


def test_init_without_client(monkeypatch):
    """Test initialization without providing client"""
    mock_openai_class = MagicMock()
    monkeypatch.setattr(llm_client_openai, "OpenAI", mock_openai_class)
    client = LLMClient()
    mock_openai_class.assert_called_once()
    assert client.client == mock_openai_class.return_value


def test_call_with_response_format(mock_llm_client, mock_system_message, mock_messages):
    mock_response = MagicMock()
    mock_response.usage.input_tokens = 100
    mock_response.usage.output_tokens = 50
    mock_response.usage.total_tokens = 150
    mock_response.output_parsed = DummyResponseFormat(result="test result")

    mock_llm_client.client.responses.parse.return_value = mock_response

    result = mock_llm_client.call_with_response_format(
        system=mock_system_message,
        messages=mock_messages,
        model="gpt-4",
        max_tokens=1000,
        response_format=DummyResponseFormat,
    )

    mock_llm_client.client.responses.parse.assert_called_once_with(
        input=mock_system_message + mock_messages,
        model="gpt-4",
        max_output_tokens=1000,
        response_format=DummyResponseFormat,
    )

    assert result.result == "test result", "Expected result to match mocked response"


def test_call_without_response_format(mock_llm_client, mock_messages):
    mock_response = {
        "usage": {"inputTokens": 100, "outputTokens": 50, "totalTokens": 150},
        "output": {"message": {"content": [{"text": "test response"}]}},
    }

    mock_llm_client.client.chat.completions.create.return_value = mock_response

    result = mock_llm_client.call_without_response_format(
        messages=mock_messages, model="gpt-4", max_tokens=1000, temperature=0.7
    )

    mock_llm_client.client.chat.completions.create.assert_called_once_with(
        messages=mock_messages,
        model="gpt-4",
        temperature=0.7,
        max_tokens=1000,
    )

    assert result == "test response", "Expected result to match mocked response"


def test_call_with_response_format_reasoning_model(mock_llm_client, mock_system_message, mock_messages):
    """Reasoning models receive reasoning={"effort": "low"} and no temperature."""
    mock_response = MagicMock()
    mock_response.usage.input_tokens = 100
    mock_response.usage.output_tokens = 50
    mock_response.usage.total_tokens = 150
    mock_response.output_parsed = DummyResponseFormat(result="ok")

    mock_llm_client.client.responses.parse.return_value = mock_response

    mock_llm_client.call_with_response_format(
        system=mock_system_message,
        messages=mock_messages,
        model="gpt-5-mini",
        max_tokens=2000,
        reasoning_effort="low",
        response_format=DummyResponseFormat,
    )

    call_kwargs = mock_llm_client.client.responses.parse.call_args[1]
    assert "reasoning" in call_kwargs
    assert call_kwargs["reasoning"] == {"effort": "low"}
    assert "reasoning_effort" not in call_kwargs
    assert "temperature" not in call_kwargs


def test_call_with_retries_success_after_failure(
    monkeypatch, mock_llm_client, mock_system_message, mock_messages
):
    """Test retry logic with success after initial failure"""
    mock_response = MagicMock()
    mock_response.usage.input_tokens = 100
    mock_response.usage.output_tokens = 50
    mock_response.usage.total_tokens = 150
    mock_response.output_parsed = DummyResponseFormat(result="test result")
    # Mock first call to fail, second to succeed
    mock_llm_client.client.responses.parse.side_effect = [
        Exception("API Error"),
        mock_response,
    ]
    monkeypatch.setattr(utils_time, "sleep", lambda _: None)

    result = mock_llm_client.call_with_response_format(
        system=mock_system_message,
        messages=mock_messages,
        model="gpt-4",
        max_tokens=1000,
        response_format=DummyResponseFormat,
    )

    assert mock_llm_client.client.responses.parse.call_count == 2, (
        "Expected 1 retry but got a different count"
    )

    assert result.result == "test result", "Expected result to match mocked response"


def test_call_with_retries_but_failure(
    monkeypatch, mock_llm_client, mock_system_message, mock_messages
):
    # Mock all calls to fail — use a single exception (not a list) so it never
    # runs out regardless of how many retry attempts LLM_RETRIES specifies.
    mock_llm_client.client.responses.parse.side_effect = Exception("API Error")
    monkeypatch.setattr(utils_time, "sleep", lambda _: None)

    with pytest.raises(Exception, match="API Error"):
        mock_llm_client.call_with_response_format(
            system=mock_system_message,
            messages=mock_messages,
            model="gpt-4",
            max_tokens=1000,
            response_format=DummyResponseFormat,
        )

    assert mock_llm_client.client.responses.parse.call_count == settings.LLM_RETRIES, (
        f"Expected {settings.LLM_RETRIES} attempts but got a different count"
    )
