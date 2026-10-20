# Dogwood refund-agent demo

A small Streamlit chatbot uses the Phi-4-mini-instruct deployment and two
simulated tools, `invoice_info` and `return_money`. Toggle **Enforce Dogwood
policies** to compare unrestricted tool calls with policy-checked calls. The
two deterministic demo buttons reproduce the key cases without depending on
the model's tool choice:

- Without Dogwood, a refund can skip invoice lookup and the same invoice can be
  refunded repeatedly.
- A successful refund changes the invoice status from `paid` to `reimbursed`.
- With Dogwood, a refund requires a lookup of that invoice in the previous
  24 hours whose *response* reported it as not yet reimbursed, so the status
  check must come first and be favorable. After a successful refund, further
  refunds of that invoice are denied.

The action schema and policy are in `schema.cedarschema` and `policy.dw`.
The adapter replays each completed call to Dogwood as a request plus a response
event (carrying `output.reimbursed` / `output.refunded`). Refunds are simulated in
memory; no payment system is contacted. Use **Reset chat and demo state** before
replaying the same invoice after switching modes; successful actions remain in
the chat's history until reset.

## Run

From this directory, copy `.env.example` to `.env`, then run:

```sh
uv sync
uv run --env-file .env streamlit run app.py
```

The Azure connection uses the OpenAI Python SDK and an OpenAI-compatible Azure
endpoint (for example, an endpoint ending in `/openai/v1/` or `/models/v1/`).
Set `AZURE_INFERENCE_MODEL` to the Azure deployment name. Without an API key,
the app authenticates with `DefaultAzureCredential` (for example, sign in with
Azure CLI) and requires an identity allowed to invoke that deployment. The
token audience defaults to `https://cognitiveservices.azure.com/.default`;
override it with `AZURE_INFERENCE_SCOPE` if your resource requires a different
audience. `DOGWOOD_CLI` defaults to this repository's
`target/release/dogwood`; adjust it if the executable is elsewhere. Other
settings, including the endpoint, deployment, request timeout, policy, and
schema paths, are environment variables. Requests that name an `INV-...`
invoice and ask to look it up or refund it are routed through a deterministic
local adapter before contacting the model; other chat messages use the model's
chat-completions endpoint. This also supports deployments that do not return
native function-call payloads.

### Docker

See [DOCKER.md](DOCKER.md) to build, run and publish the container image.

### Azure authentication

Set `AZURE_INFERENCE_API_KEY` to authenticate with an API key. When it is empty
or unset, `DefaultAzureCredential` (Entra ID) is used.

### Local Ollama (e.g. Gemma)

Copy `.env.ollama.example` to `.env.ollama` and run:

```sh
ollama pull gemma4
uv run --env-file .env.ollama streamlit run app.py
```

`LLM_PROVIDER=ollama` uses Ollama's OpenAI-compatible endpoint
(`OLLAMA_HOST`, `OLLAMA_MODEL`) and lets the model choose tools natively, with
every call still checked by Dogwood. Set `DETERMINISTIC_TOOL_ROUTING=true` to
force the local keyword router instead. Local models can be slow; raise
`MODEL_TIMEOUT_SECONDS` if needed.

Run the focused authorization tests with:

```sh
uv run --env-file .env pytest
```
