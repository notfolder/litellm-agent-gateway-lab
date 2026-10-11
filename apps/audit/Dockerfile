# 監査ログの整形の定期処理（compactor）と検索アプリ（viewer）のイメージ（変更設計書 1 章）
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && python -c "import duckdb; duckdb.connect().execute('INSTALL httpfs')"
COPY audit/ /app/audit/
COPY tests/ /app/tests/
# ビルドの中で単体テストを流し、壊れたイメージを作らない
RUN python -m unittest discover -s tests -q
RUN useradd -u 10001 -m app && cp -r /root/.duckdb /home/app/.duckdb && chown -R app /home/app/.duckdb
USER 10001
EXPOSE 8000
CMD ["python", "-m", "audit.viewer"]
