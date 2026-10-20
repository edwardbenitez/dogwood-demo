# Running the demo in Docker

The image bundles the Streamlit app, the policy/schema and a Linux build of the
`dogwood` CLI. Secrets are never baked in; pass them at run time.

## Build

Build from the **repository root** (the Dockerfile compiles `dogwood-cli`):

```sh
docker build -f example/Dockerfile -t <dockerhub-user>/dogwood-refund-demo:latest .
```

The first build compiles the Rust CLI and takes several minutes. Add
`--platform linux/amd64,linux/arm64` with `docker buildx` for multi-arch images.

## Run`

With a local Ollama model (Ollama must be running on the host):

```sh
cp example/.env.ollama.example example/.env.ollama
docker run --rm -p 8501:8501 \
  --env-file example/.env.ollama \
  -e OLLAMA_HOST=http://host.docker.internal:11434 \
  -e DOGWOOD_CLI=/usr/local/bin/dogwood \
  -e DOGWOOD_POLICY_FILE=policy.dw \
  -e DOGWOOD_SCHEMA_FILE=schema.cedarschema \
  <dockerhub-user>/dogwood-refund-demo:latest
```

With Azure AI Foundry (`.env.example` is the template; `-e` flags override
the file, so pass the key and log level this way):

```sh
docker run --rm -p 8501:8501 --env-file example/.env.example \
  -e AZURE_INFERENCE_API_KEY=<key> -e LOG_LEVEL=INFO \
  <dockerhub-user>/dogwood-refund-demo:latest
```

Or without an env file:

```sh
docker run --rm -p 8501:8501 \
  -e AZURE_INFERENCE_ENDPOINT=https://<resource>.openai.azure.com/openai/v1/ \
  -e AZURE_INFERENCE_MODEL=<deployment> \
  -e AZURE_INFERENCE_API_KEY=<key> \
  -e DOGWOOD_CLI=/usr/local/bin/dogwood \
  -e DOGWOOD_POLICY_FILE=policy.dw \
  -e DOGWOOD_SCHEMA_FILE=schema.cedarschema \
  <dockerhub-user>/dogwood-refund-demo:latest
```

Without `AZURE_INFERENCE_API_KEY`, the OpenAI SDK uses
`DefaultAzureCredential`; inside a container that needs
`AZURE_CLIENT_ID`/`AZURE_TENANT_ID`/`AZURE_CLIENT_SECRET` (or a managed
identity). The endpoint must expose the Azure OpenAI-compatible v1 API. Open
<http://localhost:8501>.

Notes:
- Add `-e LOG_LEVEL=DEBUG` (the default) to log the model URL, model name and
  config env vars (secrets masked) to the container's stderr (`docker logs`).
  Env vars are read at runtime, but the image must be **rebuilt** after code
  changes (`docker build --no-cache ...`) and the `:latest` tag you run must be
  that new build.
- The `DOGWOOD_*` path overrides are required when using `--env-file` with the
  `.env.*.example` files, because those contain host-relative paths
  (`../target/release/dogwood`).
- On Linux add `--add-host=host.docker.internal:host-gateway` to reach host
  Ollama, and make sure Ollama listens on an address reachable from the
  container (`OLLAMA_HOST=0.0.0.0 ollama serve`).

## Publish to Docker Hub

```sh
docker login
docker tag dogwood-refund-demo:local <dockerhub-user>/dogwood-refund-demo:latest
docker tag dogwood-refund-demo:local <dockerhub-user>/dogwood-refund-demo:0.1.0
docker push <dockerhub-user>/dogwood-refund-demo:latest
docker push <dockerhub-user>/dogwood-refund-demo:0.1.0
```

(If you built with the Docker Hub name directly, skip the `tag` commands.)
For multi-arch, build and push in one step:

```sh
docker buildx build --platform linux/amd64,linux/arm64 \
  -f example/Dockerfile -t mightydevs/dogwood-refund-demo:latest-amd --push .
```

docker buildx build --platform linux/amd64,linux/arm64 --no-cache \
  -f example/Dockerfile -t mightydevs/dogwood-refund-demo:latest-amd --push .
