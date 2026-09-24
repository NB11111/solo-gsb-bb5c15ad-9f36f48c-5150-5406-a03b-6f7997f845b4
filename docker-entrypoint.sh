#!/bin/sh
# 容器入口:先幂等建表(初始化/升级 SQLite),再启动 API。
# 依赖已在镜像构建时(Dockerfile 的 pip install)安装完毕;
# 因此 `docker compose up --build` 会自动完成依赖安装与建表,无需手工初始化。
set -e

echo "[entrypoint] 初始化数据库(幂等建表): ${SAMPLE_DB_PATH:-/data/samples.db}"
python -m app.init_db

exec uvicorn app.main:app --host 0.0.0.0 --port 8000
