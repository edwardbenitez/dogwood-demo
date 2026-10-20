from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from openai import OpenAI


logger = logging.getLogger("agent")

_CONFIG_ENV_VARS = (
    "LLM_PROVIDER",
    "AZURE_INFERENCE_ENDPOINT",
    "AZURE_INFERENCE_MODEL",
    "AZURE_INFERENCE_API_KEY",
    "AZURE_INFERENCE_SCOPE",
    "OLLAMA_HOST",
    "OLLAMA_MODEL",
    "MODEL_TIMEOUT_SECONDS",
    "DETERMINISTIC_TOOL_ROUTING",
    "MAX_TOOL_ROUNDS",
    "MAX_COMPLETION_TOKENS",
    "DOGWOOD_ENABLED",
    "DOGWOOD_CLI",
    "DOGWOOD_POLICY_FILE",
    "DOGWOOD_SCHEMA_FILE",
    "DOGWOOD_TIMEOUT_SECONDS",
    "DEMO_PRINCIPAL",
    "LOG_LEVEL",
)
_SECRET_MARKERS = ("API_KEY", "SECRET", "PASSWORD")


def _mask(name: str, value: str) -> str:
    if any(marker in name for marker in _SECRET_MARKERS):
        return f"<set, {len(value)} chars>" if value.strip() else "<empty>"
    return value


def configure_logging() -> None:
    level_name = os.environ.get("LOG_LEVEL", "DEBUG").strip().upper() or "DEBUG"
    level = getattr(logging, level_name, logging.DEBUG)
    # Dedicated handlers: Streamlit may already own the root logger, which
    # would make logging.basicConfig a silent no-op.
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    for name in ("agent", "azure.core.pipeline.policies.http_logging_policy"):
        target = logging.getLogger(name)
        target.setLevel(level)
        if not any(getattr(h, "_agent_handler", False) for h in target.handlers):
            handler = logging.StreamHandler(sys.stderr)
            handler.setLevel(level)
            handler.setFormatter(formatter)
            handler._agent_handler = True  # type: ignore[attr-defined]
            target.addHandler(handler)
        target.propagate = False


def log_environment() -> None:
    for name in _CONFIG_ENV_VARS:
        value = os.environ.get(name)
        logger.debug("env %s=%s", name, "<unset>" if value is None else _mask(name, value))


EXAMPLE_DIR = Path(__file__).resolve().parent
REPO_ROOT = EXAMPLE_DIR.parent
SYSTEM_PROMPT_DEFAULT = """You are a customer-support assistant in a refund-policy demo.
Use the invoice_info and return_money tools for invoice questions and refunds.
Try to fulfill the user's request. invoice_info reports whether an invoice is
already reimbursed; a refund moves it to reimbursed. Do not
claim an operation succeeded unless its tool result says it did. The Dogwood
toggle is the server-side authorization boundary; never pretend your own
instructions replace it. All invoice and refund operations are simulated."""

SYSTEM_PROMPT = """You are a customer-support assistant in a refund-policy demo.
Use the invoice_info and return_money tools for invoice questions and refunds.
Try to fulfill the user's request. invoice_info reports whether an invoice is
already reimbursed; a refund moves it to reimbursed. You may call return_money
without first calling invoice_info, and you may call it more than once when
requested. Do not
claim an operation succeeded unless its tool result says it did. The Dogwood
toggle is the server-side authorization boundary; never pretend your own
instructions replace it. All invoice and refund operations are simulated."""

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "invoice_info",
            "description": "Look up a demo invoice by its invoice ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "invoice_id": {
                        "type": "string",
                        "description": "Invoice ID, for example INV-1001.",
                    }
                },
                "required": ["invoice_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "return_money",
            "description": "Simulate refunding the demo invoice once per call.",
            "parameters": {
                "type": "object",
                "properties": {
                    "invoice_id": {
                        "type": "string",
                        "description": "Invoice ID, for example INV-1001.",
                    }
                },
                "required": ["invoice_id"],
                "additionalProperties": False,
            },
        },
    },
]

INVOICES: dict[str, dict[str, Any]] = {
    "INV-1001": {
        "customer": "Amira Patel",
        "description": "Annual support plan",
        "amount_cents": 12500,
        "status": "paid",
    },
    "INV-1002": {
        "customer": "Jon Bell",
        "description": "Replacement charger",
        "amount_cents": 8950,
        "status": "paid",
    },
}


class DemoError(RuntimeError):
    pass


class DogwoodError(DemoError):
    pass


class AgentLoopLimitError(DemoError):
    pass


@dataclass(frozen=True)
class ToolInvocation:
    action: str
    invoice_id: str
    call_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    timestamp_ms: int = field(default_factory=lambda: time.time_ns() // 1_000_000)
    output: dict[str, bool] | None = None

    def response_line(self, principal_id: str) -> str:
        principal = f"Drupe::OAuthUser::{_cedar_string(principal_id)}"
        resource = 'Drupe::Gateway::"refund-demo"'
        action = "InvoiceInfo" if self.action == "invoice_info" else "ReturnMoney"
        inputs = (
            "{ "
            f"call_id: {_cedar_string(self.call_id)}, "
            f"invoice_id: {_cedar_string(self.invoice_id)}"
            " }"
        )
        fields = ", ".join(
            f"{key}: {'true' if value else 'false'}"
            for key, value in sorted((self.output or {}).items())
        )
        return (
            f"@{self.timestamp_ms + 1} "
            f"scope(principal: {principal}, resource: {resource}) "
            f'Drupe::Action::"{action}"::response('
            f"input: {inputs}, output: {{ {fields} }}, "
            f"callerPrincipal: {principal}, callerResource: {resource}, "
            f"requestId: {_cedar_string(self.call_id)})"
        )

    def trace_line(self, principal_id: str) -> str:
        action = {
            "invoice_info": "InvoiceInfo",
            "return_money": "ReturnMoney",
        }.get(self.action)
        if action is None:
            raise ValueError(f"Unsupported tool action: {self.action}")

        principal = f"Drupe::OAuthUser::{_cedar_string(principal_id)}"
        resource = 'Drupe::Gateway::"refund-demo"'
        inputs = (
            "{ "
            f"call_id: {_cedar_string(self.call_id)}, "
            f"invoice_id: {_cedar_string(self.invoice_id)}"
            " }"
        )
        return (
            f"@{self.timestamp_ms} "
            f"scope(principal: {principal}, resource: {resource}) "
            f"request_context(input: {inputs}) "
            f'Drupe::Action::"{action}"::request('
            f"input: {inputs}, callerPrincipal: {principal}, "
            f"callerResource: {resource}, requestId: {_cedar_string(self.call_id)})"
        )


@dataclass
class DemoLedger:
    refund_counts: dict[str, int] = field(default_factory=dict)
    reimbursed: set[str] = field(default_factory=set)

    def status(self, invoice_id: str) -> str:
        if invoice_id in self.reimbursed:
            return "reimbursed"
        return INVOICES[invoice_id]["status"]


def _cedar_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def _policy_paths() -> tuple[Path, Path, Path]:
    def resolve(name: str, default: Path) -> Path:
        path = Path(os.environ.get(name, str(default))).expanduser()
        return path if path.is_absolute() else EXAMPLE_DIR / path

    cli = resolve("DOGWOOD_CLI", REPO_ROOT / "target" / "release" / "dogwood")
    policy = resolve("DOGWOOD_POLICY_FILE", EXAMPLE_DIR / "policy.dw")
    schema = resolve("DOGWOOD_SCHEMA_FILE", EXAMPLE_DIR / "schema.cedarschema")
    return cli, policy, schema


def _dogwood_timeout() -> float:
    try:
        timeout = float(os.environ.get("DOGWOOD_TIMEOUT_SECONDS", "180"))
    except ValueError as exc:
        raise DogwoodError("DOGWOOD_TIMEOUT_SECONDS must be a number.") from exc
    if timeout <= 0:
        raise DogwoodError("DOGWOOD_TIMEOUT_SECONDS must be greater than zero.")
    return timeout


def dogwood_authorizes(
    history: Sequence[ToolInvocation],
    candidate: ToolInvocation,
    principal_id: str,
) -> tuple[bool, str]:
    cli, policy, schema = _policy_paths()
    for path, variable in (
        (cli, "DOGWOOD_CLI"),
        (policy, "DOGWOOD_POLICY_FILE"),
        (schema, "DOGWOOD_SCHEMA_FILE"),
    ):
        if not path.is_file():
            raise DogwoodError(f"{variable} does not point to a file: {path}")

    invocations = [*history, candidate]
    last_timestamp = 0
    trace_lines: list[str] = []
    for invocation in invocations:
        timestamp = max(invocation.timestamp_ms, last_timestamp + 1)
        last_timestamp = timestamp + 1
        stamped = ToolInvocation(
            action=invocation.action,
            invoice_id=invocation.invoice_id,
            call_id=invocation.call_id,
            timestamp_ms=timestamp,
            output=invocation.output,
        )
        trace_lines.append(stamped.trace_line(principal_id))
        if invocation.output is not None:
            trace_lines.append(stamped.response_line(principal_id))

    try:
        with tempfile.TemporaryDirectory(prefix="dogwood-refund-") as temp_dir:
            trace_path = Path(temp_dir) / "trace.log"
            trace_path.write_text("\n".join(trace_lines) + "\n", encoding="utf-8")
            completed = subprocess.run(
                [
                    str(cli),
                    "replay",
                    str(policy),
                    "--policy-schema",
                    str(schema),
                    "--trace",
                    str(trace_path),
                    "--format",
                    "json",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=_dogwood_timeout(),
            )
    except subprocess.TimeoutExpired as exc:
        raise DogwoodError(
            f"Dogwood replay exceeded {_dogwood_timeout():g} seconds."
        ) from exc
    except OSError as exc:
        raise DogwoodError(f"Could not start Dogwood CLI: {exc}") from exc

    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise DogwoodError(
            f"Dogwood replay failed with exit code {completed.returncode}: {detail}"
        )

    try:
        verdicts = json.loads(completed.stdout)["verdicts"]
        verdict = verdicts[-1]
    except (KeyError, IndexError, json.JSONDecodeError, TypeError) as exc:
        raise DogwoodError("Dogwood replay returned an invalid verdict report.") from exc

    errors = verdict.get("errors", [])
    if errors:
        raise DogwoodError(f"Dogwood evaluation error: {'; '.join(errors)}")
    allowed = verdict.get("verdict") == "allow"
    if allowed:
        return True, "Allowed by Dogwood policy."

    rules = verdict.get("determining_rules", [])
    if rules:
        return False, f"Denied by Dogwood policy rule(s): {', '.join(map(str, rules))}."
    return False, "Denied by Dogwood's - policy violation behavior."


def execute_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    history: list[ToolInvocation],
    ledger: DemoLedger,
    dogwood_enabled: bool,
    principal_id: str,
) -> dict[str, Any]:
    if name not in {"invoice_info", "return_money"}:
        raise ValueError(f"Unknown tool requested by the model: {name}")
    if set(arguments) != {"invoice_id"}:
        raise ValueError(f"{name} requires exactly one argument: invoice_id.")
    invoice_id = arguments["invoice_id"]
    if not isinstance(invoice_id, str) or not invoice_id.strip():
        raise ValueError("invoice_id must be a non-empty string.")
    invoice_id = invoice_id.strip()

    invocation = ToolInvocation(action=name, invoice_id=invoice_id)
    if dogwood_enabled:
        allowed, explanation = dogwood_authorizes(
            history, invocation, principal_id
        )
        if not allowed:
            return {
                "ok": False,
                "decision": "denied",
                "blocked_by": "Dogwood",
                "message": explanation,
            }
    else:
        explanation = "Dogwood is off; the tool call is unguarded."

    invoice = INVOICES.get(invoice_id)
    if name == "invoice_info":
        result = (
            {"found": False, "invoice_id": invoice_id}
            if invoice is None
            else {
                "found": True,
                "invoice_id": invoice_id,
                "customer": invoice["customer"],
                "description": invoice["description"],
                "amount": f"${invoice['amount_cents'] / 100:.2f}",
                "status": ledger.status(invoice_id),
                "reimbursed": invoice_id in ledger.reimbursed,
            }
        )
        history.append(
            replace(
                invocation,
                output={
                    "found": invoice is not None,
                    "reimbursed": invoice_id in ledger.reimbursed,
                },
            )
        )
    elif invoice is None:
        result = {
            "refunded": False,
            "invoice_id": invoice_id,
            "message": "No matching demo invoice exists.",
        }
    else:
        count = ledger.refund_counts.get(invoice_id, 0) + 1
        ledger.refund_counts[invoice_id] = count
        ledger.reimbursed.add(invoice_id)
        result = {
            "refunded": True,
            "invoice_id": invoice_id,
            "amount": f"${invoice['amount_cents'] / 100:.2f}",
            "refund_number": count,
            "simulated": True,
            "status": "reimbursed",
        }
        history.append(replace(invocation, output={"refunded": True}))

    return {
        "ok": True,
        "decision": "allowed" if dogwood_enabled else "unguarded",
        "message": explanation,
        "result": result,
    }


def _demo_tool_requests(prompt: str) -> list[tuple[str, str]]:
    invoice_ids = re.findall(r"\bINV-[A-Za-z0-9-]+\b", prompt, re.IGNORECASE)
    if not invoice_ids:
        return []

    invoice_id = invoice_ids[0].upper()
    refund_requested = re.search(r"\b(refund|return\s+money|send\s+back)\b", prompt, re.I)
    if refund_requested:
        lookup_requested = re.search(
            r"\b(look\s*up|check|verify|inspect|find)\b.{0,40}\binvoice\b",
            prompt,
            re.I,
        )
        calls = [("invoice_info", invoice_id)] if lookup_requested else []
        repeat_requested = re.search(
            r"\b(twice|two\s+times|2\s+times|multiple\s+times)\b", prompt, re.I
        )
        calls.extend(
            [("return_money", invoice_id)] * (2 if repeat_requested else 1)
        )
        return calls

    if re.search(r"\b(invoice|look\s*up|check|details|status|amount)\b", prompt, re.I):
        return [("invoice_info", invoice_id)]
    return []


def _format_demo_results(
    results: Sequence[tuple[str, dict[str, Any]]],
) -> str:
    responses: list[str] = []
    for name, response in results:
        result = response.get("result", {})
        if response.get("blocked_by"):
            responses.append(f"**{name} denied:** {response['message']}")
        elif name == "invoice_info" and result.get("found"):
            responses.append(
                f"Invoice {result['invoice_id']} is for {result['customer']}: "
                f"{result['description']}, {result['amount']}, "
                f"status {result['status']}."
            )
        elif name == "invoice_info":
            responses.append(f"Invoice {result.get('invoice_id')} was not found.")
        elif result.get("refunded"):
            responses.append(
                f"Refund {result['refund_number']} for invoice "
                f"{result['invoice_id']} ({result['amount']}) succeeded. "
                f"{'This is a simulation.' if result.get('simulated') else ''}"
            )
        else:
            responses.append(result.get("message", response.get("message", "Tool failed.")))
    return "\n\n".join(responses)


def _model_configuration() -> tuple[str, str]:
    endpoint = os.environ.get(
        "AZURE_INFERENCE_ENDPOINT",
        "https://aifoundry06810.cognitiveservices.azure.com/openai/v1/",
    ).strip()
    model = os.environ.get("AZURE_INFERENCE_MODEL", "gpt-5.6-terra").strip()
    if not endpoint:
        raise ValueError("AZURE_INFERENCE_ENDPOINT must not be empty.")
    if not model:
        raise ValueError("AZURE_INFERENCE_MODEL must not be empty.")
    return endpoint, model


class ModelBackendError(DemoError):
    pass


def _provider() -> str:
    provider = os.environ.get("LLM_PROVIDER", "azure").strip().lower()
    if provider not in {"azure", "ollama"}:
        raise ValueError("LLM_PROVIDER must be 'azure' or 'ollama'.")
    return provider


def _model_name() -> str:
    if _provider() == "ollama":
        model = os.environ.get("OLLAMA_MODEL", "gemma4:latest").strip()
        if not model:
            raise ValueError("OLLAMA_MODEL must not be empty.")
        return model
    return _model_configuration()[1]


def _use_deterministic_routing() -> bool:
    value = os.environ.get("DETERMINISTIC_TOOL_ROUTING", "auto").strip().lower()
    if value == "auto":
        return _provider() == "azure"
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError("DETERMINISTIC_TOOL_ROUTING must be auto, true, or false.")


class OllamaClient:
    """Minimal client for Ollama's OpenAI-compatible chat endpoint."""

    def __init__(self, host: str, timeout: float) -> None:
        self._url = host.rstrip("/") + "/v1/chat/completions"
        self._timeout = timeout

    def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        model: str,
        tools: Sequence[Any],
        tool_choice: str = "auto",
        max_tokens: int,
        temperature: float,
    ) -> Any:
        payload = {
            "model": model,
            "messages": messages,
            "tools": [t.as_dict() if hasattr(t, "as_dict") else t for t in tools],
            "tool_choice": tool_choice,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        logger.debug(
            "POST %s model=%s timeout=%ss messages=%d",
            self._url, model, self._timeout, len(messages),
        )
        request = urllib.request.Request(
            self._url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            # Bypass proxy env vars (often injected into containers), which
            # would otherwise route host.docker.internal through a proxy.
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(request, timeout=self._timeout) as reply:
                body = json.load(reply)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            raise ModelBackendError(f"Ollama returned HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise ModelBackendError(
                f"Could not get a response from Ollama at {self._url}: {exc}"
            ) from exc

        choices = []
        for choice in body.get("choices", []):
            raw = choice.get("message", {})
            tool_calls = [
                SimpleNamespace(
                    id=call.get("id") or f"call_{index}",
                    function=SimpleNamespace(
                        name=call["function"]["name"],
                        arguments=call["function"].get("arguments", "{}"),
                    ),
                )
                for index, call in enumerate(raw.get("tool_calls") or [])
            ]
            message = SimpleNamespace(
                content=raw.get("content") or None,
                tool_calls=tool_calls,
                as_dict=lambda raw=raw: {
                    k: v for k, v in raw.items() if k != "reasoning"
                },
            )
            choices.append(SimpleNamespace(message=message))
        return SimpleNamespace(choices=choices)


def _request_timeout() -> float:
    try:
        timeout = float(os.environ.get("MODEL_TIMEOUT_SECONDS", "180"))
    except ValueError as exc:
        raise ValueError("MODEL_TIMEOUT_SECONDS must be a number.") from exc
    if timeout <= 0:
        raise ValueError("MODEL_TIMEOUT_SECONDS must be greater than zero.")
    return timeout


def create_client() -> Any:
    configure_logging()
    log_environment()
    if _provider() == "ollama":
        host = os.environ.get("OLLAMA_HOST", "http://localhost:11434").strip()
        if not host:
            raise ValueError("OLLAMA_HOST must not be empty.")
        return OllamaClient(host, _request_timeout())
    endpoint, _ = _model_configuration()
    api_key = os.environ.get("AZURE_INFERENCE_API_KEY", "").strip()
    if api_key:
        credential: Any = api_key
    else:
        credential_scope = os.environ.get(
            "AZURE_INFERENCE_SCOPE",
            "https://cognitiveservices.azure.com/.default",
        ).strip()
        if not credential_scope:
            raise ValueError("AZURE_INFERENCE_SCOPE must not be empty.")
        credential = get_bearer_token_provider(
            DefaultAzureCredential(), credential_scope
        )

    return OpenAI(
        base_url=endpoint,
        api_key=credential,
        timeout=_request_timeout(),
    )


def run_chat_turn(
    messages: list[dict[str, Any]],
    client: Any,
    *,
    history: list[ToolInvocation],
    ledger: DemoLedger,
    dogwood_enabled: bool,
    principal_id: str,
) -> str:
    model = _model_name()
    try:
        max_rounds = int(os.environ.get("MAX_TOOL_ROUNDS", "6"))
        max_tokens = int(os.environ.get("MAX_COMPLETION_TOKENS", "700"))
    except ValueError as exc:
        raise ValueError(
            "MAX_TOOL_ROUNDS and MAX_COMPLETION_TOKENS must be integers."
        ) from exc
    if max_rounds < 1 or max_tokens < 1:
        raise ValueError("Model limits must be positive integers.")

    user_prompt = next(
        (
            str(message["content"])
            for message in reversed(messages)
            if message.get("role") == "user"
        ),
        "",
    )
    demo_requests = (
        _demo_tool_requests(user_prompt) if _use_deterministic_routing() else []
    )
    if demo_requests:
        results: list[tuple[str, dict[str, Any]]] = []
        for name, invoice_id in demo_requests:
            result = execute_tool(
                name,
                {"invoice_id": invoice_id},
                history=history,
                ledger=ledger,
                dogwood_enabled=dogwood_enabled,
                principal_id=principal_id,
            )
            results.append((name, result))
            if result.get("blocked_by"):
                break
        return _format_demo_results(results)

    if not isinstance(client, OllamaClient):
        logger.debug(
            "Azure OpenAI chat completions: endpoint=%s model=%s timeout=%ss messages=%d",
            _model_configuration()[0], model, _request_timeout(), len(messages),
        )
    for round_number in range(1, max_rounds + 1):
        started = time.monotonic()
        logger.debug("model call round %d starting", round_number)
        completion_args = {
            "messages": messages,
            "model": model,
            "tools": TOOL_DEFINITIONS,
            "tool_choice": "auto",
        }
        if isinstance(client, OllamaClient):
            response = client.complete(
                **completion_args,
                max_tokens=max_tokens,
                temperature=0,
            )
        else:
            response = client.chat.completions.create(
                **completion_args, max_completion_tokens=max_tokens
            )
        logger.debug(
            "model call round %d finished in %.1fs", round_number, time.monotonic() - started
        )
        if not response.choices:
            raise DemoError("The model returned no completion choices.")

        assistant_message = response.choices[0].message
        tool_calls = assistant_message.tool_calls or []
        if not tool_calls:
            content = assistant_message.content
            if content:
                messages.append(_assistant_message_dict(assistant_message))
                return content
            raise DemoError(
                "The model returned neither text nor tool calls. Try an invoice "
                "lookup or refund for an invoice such as INV-1001."
            )
        messages.append(_assistant_message_dict(assistant_message))

        for tool_call in tool_calls:
            name = tool_call.function.name
            try:
                arguments = json.loads(tool_call.function.arguments)
            except (json.JSONDecodeError, TypeError) as exc:
                raise DemoError(f"The model returned invalid arguments for {name}.") from exc
            if not isinstance(arguments, dict):
                raise DemoError(f"The model arguments for {name} must be a JSON object.")
            result = execute_tool(
                name,
                arguments,
                history=history,
                ledger=ledger,
                dogwood_enabled=dogwood_enabled,
                principal_id=principal_id,
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )

    raise AgentLoopLimitError(
        f"The model requested tools in {max_rounds} consecutive rounds."
    )


def _assistant_message_dict(message: Any) -> dict[str, Any]:
    if hasattr(message, "model_dump"):
        return message.model_dump(exclude_none=True)
    return message.as_dict()
