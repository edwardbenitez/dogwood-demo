from __future__ import annotations

import json
import os
from typing import Any

import streamlit as st
from azure.core.exceptions import AzureError

from agent import (
    DemoError,
    DemoLedger,
    SYSTEM_PROMPT,
    create_client,
    execute_tool,
    run_chat_turn,
)


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name, str(default)).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false.")


def _initialize_session() -> None:
    if "dogwood_enabled" not in st.session_state:
        st.session_state.dogwood_enabled = _env_bool("DOGWOOD_ENABLED")
    if "messages" not in st.session_state:
        st.session_state.messages = [
            {"role": "system", "content": SYSTEM_PROMPT}
        ]
    if "tool_history" not in st.session_state:
        st.session_state.tool_history = []
    if "ledger" not in st.session_state:
        st.session_state.ledger = DemoLedger()


def _render_tool_results(results: list[tuple[str, dict[str, Any]]]) -> str:
    sections = []
    for name, result in results:
        heading = "Blocked" if result.get("blocked_by") else "Tool result"
        sections.append(
            f"**{heading}: `{name}`**\n\n```json\n"
            f"{json.dumps(result, indent=2, ensure_ascii=False)}\n```"
        )
    return "\n\n".join(sections)


def _run_demo(label: str, calls: list[tuple[str, str]]) -> None:
    messages = st.session_state.messages
    messages.append({"role": "user", "content": f"Demo: {label}"})
    results: list[tuple[str, dict[str, Any]]] = []
    for name, invoice_id in calls:
        result = execute_tool(
            name,
            {"invoice_id": invoice_id},
            history=st.session_state.tool_history,
            ledger=st.session_state.ledger,
            dogwood_enabled=st.session_state.dogwood_enabled,
            principal_id=os.environ.get("DEMO_PRINCIPAL", "refund-agent"),
        )
        results.append((name, result))
        if result.get("blocked_by"):
            break
    messages.append(
        {"role": "assistant", "content": _render_tool_results(results)}
    )
    st.rerun()


@st.cache_resource
def _client():
    return create_client()


st.set_page_config(page_title="Dogwood Refund Demo")
_initialize_session()

st.title("Refund assistant: with and without Dogwood")
st.write(
    "Compare an LLM agent with unguarded tool access against the same agent "
    "with Dogwood enforcing authorization before each simulated tool call."
)

with st.sidebar:
    st.header("Authorization")
    st.toggle(
        "Enforce Dogwood policies",
        key="dogwood_enabled",
        help="When enabled, the local Dogwood CLI checks every tool call.",
    )
    st.caption(
        "Policy: look up the same invoice first; allow only one successful "
        "refund per invoice in the last 24 hours."
    )
    st.metric("Successful tool events in this chat", len(st.session_state.tool_history))
    st.caption("All invoice and refund tools use an in-memory demo ledger.")
    if st.button("Reset chat and demo state", use_container_width=True):
        st.session_state.messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        st.session_state.tool_history = []
        st.session_state.ledger = DemoLedger()
        st.rerun()

st.subheader("Deterministic policy demos")
st.caption(
    "These buttons call the exact same tool adapter as the agent, without a "
    "model round-trip, so the policy contrast is reproducible. Reset the demo "
    "state before replaying the same invoice under the other setting."
)
skip_col, repeat_col = st.columns(2)
with skip_col:
    if st.button("Skip lookup and refund INV-1001", use_container_width=True):
        _run_demo(
            "attempt a refund without looking up the invoice",
            [("return_money", "INV-1001")],
        )
with repeat_col:
    if st.button("Look up, then refund twice", use_container_width=True):
        _run_demo(
            "look up INV-1001 and refund it twice",
            [
                ("invoice_info", "INV-1001"),
                ("return_money", "INV-1001"),
                ("return_money", "INV-1001"),
            ],
        )

st.subheader("Chat")
for message in st.session_state.messages:
    if message["role"] not in {"user", "assistant"} or not message.get("content"):
        continue
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

if prompt := st.chat_input("Ask about an invoice or request a refund"):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)
    with st.chat_message("assistant"):
        with st.spinner("Checking policy and preparing your response..."):
            try:
                answer = run_chat_turn(
                    st.session_state.messages,
                    _client(),
                    history=st.session_state.tool_history,
                    ledger=st.session_state.ledger,
                    dogwood_enabled=st.session_state.dogwood_enabled,
                    principal_id=os.environ.get("DEMO_PRINCIPAL", "refund-agent"),
                )
            except (AzureError, DemoError, ValueError) as exc:
                st.error(f"Request failed: {exc}")
            else:
                st.markdown(answer)
                st.session_state.messages.append(
                    {"role": "assistant", "content": answer}
                )
