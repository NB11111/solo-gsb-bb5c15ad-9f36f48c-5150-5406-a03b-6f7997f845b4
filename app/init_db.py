"""命令行数据库初始化:幂等建表并执行增量迁移。

容器启动入口(docker-entrypoint.sh)先运行本模块,确保
`docker compose up --build` 后立即可用;应用 lifespan 中也会再次幂等初始化。
"""
from __future__ import annotations

from .config import DB_PATH
from .db import connect, init_db


def main() -> None:
    conn = connect(DB_PATH)
    try:
        init_db(conn)
    finally:
        conn.close()
    print(f"数据库初始化完成: {DB_PATH}")


if __name__ == "__main__":
    main()
