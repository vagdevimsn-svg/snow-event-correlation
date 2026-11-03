FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY event_corr ./event_corr
COPY sample_data ./sample_data
RUN pip install --no-cache-dir . && useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"
ENTRYPOINT ["event-corr-serve"]
CMD ["--topology", "sample_data/topology.json", "--host", "0.0.0.0", "--port", "8000"]
