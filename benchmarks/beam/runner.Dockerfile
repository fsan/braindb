# benchmarks/beam/runner.Dockerfile
#
# Small orchestrator image for the bench. Lives separately from BrainDB's
# main image because:
#   - it needs the docker CLI (to recreate api_bench between conversations)
#   - it only needs bench-side Python deps (no FastAPI, no embeddings, no
#     pgvector — just enough to drive the bench)
#
# Built only when running the bench:
#   docker compose -f docker-compose.bench.yml --profile runner build bench_runner
# Run with:
#   docker compose -f docker-compose.bench.yml run --rm bench_runner \
#     python -m benchmarks.beam.bench --split 100K --limit 1

FROM python:3.12-slim

# Docker CLI lets the runner do `docker compose up -d --force-recreate api_bench`
# between conversations to swap DATABASE_URL. ca-certificates + curl are
# for TLS + simple health probes.
RUN apt-get update \
    && apt-get install -y --no-install-recommends docker.io curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Bench Python deps. Repo code itself comes in via the `.:/app` bind mount;
# we just need the runtime libraries here.
RUN pip install --no-cache-dir \
    datasets \
    huggingface_hub \
    requests \
    psycopg2-binary

WORKDIR /app
ENV PYTHONPATH=/app

# Default: sit idle so `docker compose run --rm bench_runner <cmd>` can
# spawn ephemeral instances with whatever command we want.
CMD ["sleep", "infinity"]
