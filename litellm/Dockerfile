# 検証用の LiteLLM イメージ: 公式イメージ（版を固定）に審査ガードレールを足す
ARG LITELLM_BASE_IMAGE=ghcr.io/berriai/litellm-database:v1.104.2
FROM ${LITELLM_BASE_IMAGE}

COPY guardrails/ /app/guardrails/
ENV PYTHONPATH=/app/guardrails

# ビルド時に単体テストを流し、壊れたガードレールのイメージを作らない
RUN cd /app/guardrails && python -m unittest discover -s tests -q
