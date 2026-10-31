# 監査ログの整形の定期処理（compactor）と検索アプリ（viewer）のイメージ（変更設計書 1 章）
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt egress-ca.pem ./
# egress-ca.pem は ./lab images が置く（出口プロキシの中では中継の CA、無ければ空）。中身があれば OS と pip に信頼させる
RUN if [ -s egress-ca.pem ]; then cp egress-ca.pem /usr/local/share/ca-certificates/lab-egress.crt && update-ca-certificates \
      && export PIP_CERT=/etc/ssl/certs/ca-certificates.crt; fi \
 && pip install --no-cache-dir -r requirements.txt \
 && python -c "import duckdb; duckdb.connect().execute('INSTALL httpfs')"
COPY audit/ /app/audit/
COPY tests/ /app/tests/
# ビルドの中で単体テストを流し、壊れたイメージを作らない
RUN python -m unittest discover -s tests -q
RUN useradd -u 10001 -m app && cp -r /root/.duckdb /home/app/.duckdb && chown -R app /home/app/.duckdb
USER 10001
EXPOSE 8000
CMD ["python", "-m", "audit.viewer"]
