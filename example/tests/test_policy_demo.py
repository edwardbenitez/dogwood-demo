from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent import (
    TOOL_DEFINITIONS,
    DemoError,
    DemoLedger,
    execute_tool,
    run_chat_turn,
)


def test_dogwood_requires_invoice_lookup_and_blocks_a_repeat_refund() -> None:
    history = []
    ledger = DemoLedger()

    skipped_lookup = execute_tool(
        "return_money",
        {"invoice_id": "INV-1001"},
        history=history,
        ledger=ledger,
        dogwood_enabled=True,
        principal_id="test-agent",
    )
    assert skipped_lookup["blocked_by"] == "Dogwood"
    assert history == []

    other_lookup = execute_tool(
        "invoice_info",
        {"invoice_id": "INV-1002"},
        history=history,
        ledger=ledger,
        dogwood_enabled=True,
        principal_id="test-agent",
    )
    assert other_lookup["result"]["found"] is True
    wrong_invoice = execute_tool(
        "return_money",
        {"invoice_id": "INV-1001"},
        history=history,
        ledger=ledger,
        dogwood_enabled=True,
        principal_id="test-agent",
    )
    assert wrong_invoice["blocked_by"] == "Dogwood"
    assert ledger.refund_counts == {}

    lookup = execute_tool(
        "invoice_info",
        {"invoice_id": "INV-1001"},
        history=history,
        ledger=ledger,
        dogwood_enabled=True,
        principal_id="test-agent",
    )
    assert lookup["result"]["found"] is True

    first_refund = execute_tool(
        "return_money",
        {"invoice_id": "INV-1001"},
        history=history,
        ledger=ledger,
        dogwood_enabled=True,
        principal_id="test-agent",
    )
    assert first_refund["result"]["refund_number"] == 1

    second_refund = execute_tool(
        "return_money",
        {"invoice_id": "INV-1001"},
        history=history,
        ledger=ledger,
        dogwood_enabled=True,
        principal_id="test-agent",
    )
    assert second_refund["blocked_by"] == "Dogwood"
    assert ledger.refund_counts == {"INV-1001": 1}


def test_unguarded_tools_allow_direct_and_repeated_refunds() -> None:
    history = []
    ledger = DemoLedger()

    first = execute_tool(
        "return_money",
        {"invoice_id": "INV-1001"},
        history=history,
        ledger=ledger,
        dogwood_enabled=False,
        principal_id="test-agent",
    )
    second = execute_tool(
        "return_money",
        {"invoice_id": "INV-1001"},
        history=history,
        ledger=ledger,
        dogwood_enabled=False,
        principal_id="test-agent",
    )

    assert first["decision"] == "unguarded"
    assert first["result"]["refund_number"] == 1
    assert second["result"]["refund_number"] == 2


def test_invoice_request_uses_local_tool_adapter_without_model_call() -> None:
    client = Mock()
    client.chat.completions.create.return_value = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=None, tool_calls=[])
            )
        ]
    )

    result = run_chat_turn(
        [{"role": "user", "content": "Look up invoice INV-1001."}],
        client,
        history=[],
        ledger=DemoLedger(),
        dogwood_enabled=False,
        principal_id="test-agent",
    )

    assert "Amira Patel" in result
    assert "$125.00" in result
    client.chat.completions.create.assert_not_called()


def test_chat_refund_is_denied_without_lookup_when_dogwood_is_enabled() -> None:
    result = run_chat_turn(
        [{"role": "user", "content": "Refund INV-1001."}],
        Mock(),
        history=[],
        ledger=DemoLedger(),
        dogwood_enabled=True,
        principal_id="test-agent",
    )

    assert "denied" in result.lower()
    assert "Dogwood" in result


def test_chat_refund_after_invoice_lookup_is_allowed_once() -> None:
    history = []
    ledger = DemoLedger()
    client = Mock()

    result = run_chat_turn(
        [{"role": "user", "content": "Check invoice INV-1001, then refund it twice."}],
        client,
        history=history,
        ledger=ledger,
        dogwood_enabled=True,
        principal_id="test-agent",
    )

    assert "succeeded" in result
    assert "denied" in result.lower()
    assert ledger.refund_counts == {"INV-1001": 1}
    client.chat.completions.create.assert_not_called()


def test_unrecognized_empty_model_response_is_reported_as_an_error() -> None:
    client = Mock()
    client.chat.completions.create.return_value = SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=[]))
        ]
    )

    with pytest.raises(DemoError, match="neither text nor tool calls"):
        run_chat_turn(
            [{"role": "user", "content": "Hello there."}],
            client,
            history=[],
            ledger=DemoLedger(),
            dogwood_enabled=False,
            principal_id="test-agent",
        )


def test_ollama_client_parses_native_tool_calls(monkeypatch) -> None:
    import io
    import json as _json

    from agent import OllamaClient

    body = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "function": {
                                "name": "invoice_info",
                                "arguments": '{"invoice_id": "INV-1001"}',
                            },
                        }
                    ],
                }
            }
        ]
    }
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_a, **_k: io.BytesIO(_json.dumps(body).encode()),
    )
    reply = OllamaClient("http://localhost:11434", 5).complete(
        messages=[], model="m", tools=[], max_tokens=10, temperature=0
    )
    call = reply.choices[0].message.tool_calls[0]
    assert call.function.name == "invoice_info"
    assert reply.choices[0].message.content is None


def test_ollama_provider_skips_deterministic_routing(monkeypatch) -> None:
    from agent import OllamaClient

    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    client = OllamaClient("http://localhost:11434", 5)
    monkeypatch.setattr(
        client,
        "complete",
        Mock(
            return_value=SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content="model answer",
                            tool_calls=[],
                            as_dict=lambda: {
                                "role": "assistant",
                                "content": "model answer",
                            },
                        )
                    )
                ]
            )
        ),
    )
    result = run_chat_turn(
        [{"role": "user", "content": "Look up invoice INV-1001."}],
        client,
        history=[],
        ledger=DemoLedger(),
        dogwood_enabled=False,
        principal_id="test-agent",
    )
    assert result == "model answer"
    client.complete.assert_called_once()


def test_openai_client_chat_completions_support_tool_calls(monkeypatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "azure")
    monkeypatch.setenv("DETERMINISTIC_TOOL_ROUTING", "false")

    assistant_tool_calls = [
        {
            "id": "call-1",
            "type": "function",
            "function": {
                "name": "invoice_info",
                "arguments": '{"invoice_id":"INV-1001"}',
            },
        }
    ]
    tool_message = SimpleNamespace(
        content=None,
        tool_calls=[
            SimpleNamespace(
                id="call-1",
                function=SimpleNamespace(
                    name="invoice_info",
                    arguments='{"invoice_id":"INV-1001"}',
                ),
            )
        ],
        model_dump=Mock(
            return_value={"role": "assistant", "tool_calls": assistant_tool_calls}
        ),
    )
    final_message = SimpleNamespace(
        content="Invoice checked.",
        tool_calls=[],
        model_dump=Mock(
            return_value={"role": "assistant", "content": "Invoice checked."}
        ),
    )
    client = Mock()
    client.chat.completions.create.side_effect = [
        SimpleNamespace(choices=[SimpleNamespace(message=tool_message)]),
        SimpleNamespace(choices=[SimpleNamespace(message=final_message)]),
    ]
    messages = [{"role": "user", "content": "Please check this invoice."}]

    result = run_chat_turn(
        messages,
        client,
        history=[],
        ledger=DemoLedger(),
        dogwood_enabled=False,
        principal_id="test-agent",
    )

    assert result == "Invoice checked."
    assert client.chat.completions.create.call_count == 2
    assert client.chat.completions.create.call_args_list[0].kwargs["tools"] == (
        TOOL_DEFINITIONS
    )
    first_request = client.chat.completions.create.call_args_list[0].kwargs
    assert first_request["max_completion_tokens"] == 700
    assert "max_tokens" not in first_request
    assert "temperature" not in first_request
    followup_messages = client.chat.completions.create.call_args_list[1].kwargs[
        "messages"
    ]
    tool_reply = next(
        message for message in followup_messages if message["role"] == "tool"
    )
    assert tool_reply["tool_call_id"] == "call-1"


def _call(name, ledger, history, dogwood=True):
    return execute_tool(
        name,
        {"invoice_id": "INV-1001"},
        history=history,
        ledger=ledger,
        dogwood_enabled=dogwood,
        principal_id="test-agent",
    )


def test_refund_changes_status_and_blocks_refund_after_fresh_lookup() -> None:
    ledger, history = DemoLedger(), []
    assert _call("invoice_info", ledger, history)["result"]["status"] == "paid"
    assert _call("return_money", ledger, history)["result"]["refunded"] is True

    info = _call("invoice_info", ledger, history)["result"]
    assert info["status"] == "reimbursed" and info["reimbursed"] is True

    denied = _call("return_money", ledger, history)
    assert denied["decision"] == "denied"
    assert ledger.refund_counts == {"INV-1001": 1}


def test_refund_denied_when_lookup_shows_already_reimbursed() -> None:
    ledger, history = DemoLedger(), []
    _call("return_money", ledger, [], dogwood=False)
    assert _call("invoice_info", ledger, history)["result"]["reimbursed"] is True
    assert _call("return_money", ledger, history)["decision"] == "denied"
    assert ledger.refund_counts == {"INV-1001": 1}


def test_azure_client_uses_api_key_when_set_else_default_credential(monkeypatch) -> None:
    import agent

    monkeypatch.setenv("LLM_PROVIDER", "azure")
    seen = {}
    monkeypatch.setattr(agent, "OpenAI", lambda **kw: seen.update(kw) or object())
    monkeypatch.setenv("AZURE_INFERENCE_API_KEY", "secret")
    agent.create_client()
    assert seen["api_key"] == "secret"
    assert seen["base_url"] == agent._model_configuration()[0]
    assert seen["timeout"] == agent._request_timeout()

    seen.clear()
    monkeypatch.setenv("AZURE_INFERENCE_API_KEY", "  ")
    monkeypatch.delenv("AZURE_INFERENCE_SCOPE", raising=False)
    azure_credential = object()
    monkeypatch.setattr(agent, "DefaultAzureCredential", lambda: azure_credential)
    token_provider = Mock(return_value="token")

    def make_token_provider(credential, scope):
        seen["azure_credential"] = credential
        seen["credential_scope"] = scope
        return token_provider

    monkeypatch.setattr(agent, "get_bearer_token_provider", make_token_provider)
    agent.create_client()
    assert seen["api_key"] is token_provider
    assert seen["azure_credential"] is azure_credential
    assert seen["credential_scope"] == "https://cognitiveservices.azure.com/.default"
