# Build from the repository root:
#   docker build -f example/Dockerfile -t <user>/dogwood-refund-demo .

# Run natively on the build host and cross-compile, so multi-arch builds
# don't depend on QEMU emulation (which segfaults for cargo/uv).
FROM --platform=$BUILDPLATFORM rust:1-bookworm AS dogwood-build
ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends \
      gcc-x86-64-linux-gnu gcc-aarch64-linux-gnu libc6-dev-amd64-cross libc6-dev-arm64-cross \
    && rm -rf /var/lib/apt/lists/*
ENV CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_LINKER=x86_64-linux-gnu-gcc \
    CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER=aarch64-linux-gnu-gcc \
    CC_x86_64_unknown_linux_gnu=x86_64-linux-gnu-gcc \
    CC_aarch64_unknown_linux_gnu=aarch64-linux-gnu-gcc
WORKDIR /src
COPY Cargo.toml Cargo.lock ./
COPY dogwood-language ./dogwood-language
COPY dogwood-cli ./dogwood-cli
COPY dogwood-docs ./dogwood-docs
RUN case "$TARGETARCH" in \
      amd64) T=x86_64-unknown-linux-gnu ;; \
      arm64) T=aarch64-unknown-linux-gnu ;; \
      *) echo "unsupported arch $TARGETARCH" >&2; exit 1 ;; \
    esac \
    && rustup target add "$T" \
    && cargo build --release -p dogwood-cli --target "$T" \
    && cp "target/$T/release/dogwood" /dogwood

# Resolve locked dependencies natively; uv itself is not run under emulation.
FROM --platform=$BUILDPLATFORM python:3.13-slim-bookworm AS requirements
RUN pip install --no-cache-dir uv
WORKDIR /app
COPY example/pyproject.toml example/uv.lock ./
RUN uv export --frozen --no-dev --no-emit-project -o requirements.txt

FROM python:3.13-slim-bookworm
ENV PYTHONUNBUFFERED=1
WORKDIR /app/example
COPY --from=requirements /app/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY example/agent.py example/app.py example/policy.dw example/schema.cedarschema ./
COPY --from=dogwood-build /dogwood /usr/local/bin/dogwood
# .env.example uses the host-relative DOGWOOD_CLI=../target/release/dogwood; make it resolve here too.
RUN mkdir -p /app/target/release && ln -s /usr/local/bin/dogwood /app/target/release/dogwood

ENV DOGWOOD_CLI=/usr/local/bin/dogwood \
    PATH="/usr/local/bin:$PATH"
RUN useradd --create-home app
USER app
EXPOSE 8501
HEALTHCHECK CMD python -c "import urllib.request as u; u.urlopen('http://localhost:8501/_stcore/health')"
CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true"]
