FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SAMPLE_DB_PATH=/data/samples.db

WORKDIR /app

# 安装全部运行依赖(FastAPI/Uvicorn/Pydantic,含样品召回模块所需)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# 非 root 运行,数据库目录显式授权
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /data
USER appuser

VOLUME ["/data"]

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health', timeout=2).status==200 else 1)"

# 启动即建表:应用 lifespan 在 /data/samples.db 上幂等执行全部建表与增量迁移
# (含样品召回 recalls / recall_events 表),无需手工初始化
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
