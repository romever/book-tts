import random
import time

from .database import Database


class MimoRequestGate:
    """通过共享数据库协调 Web、Worker 和线程中的 MiMo 请求。"""

    def __init__(self, database: Database, scope: str, rpm: int):
        self.database = database
        self.scope = scope
        self.interval = 60 / rpm
        with database.connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS mimo_request_limits (
                    scope TEXT PRIMARY KEY,
                    next_request_at REAL NOT NULL DEFAULT 0,
                    cooldown_until REAL NOT NULL DEFAULT 0,
                    consecutive_limits INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO mimo_request_limits (scope) VALUES (?)", (scope,)
            )

    def acquire(self) -> float:
        """返回还需等待的秒数；仅在可以立即发送时占用请求名额。"""
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM mimo_request_limits WHERE scope = ?", (self.scope,)
            ).fetchone()
            now = time.time()
            wait = max(row["next_request_at"], row["cooldown_until"]) - now
            if wait > 0:
                return wait
            connection.execute(
                "UPDATE mimo_request_limits SET next_request_at = ? WHERE scope = ?",
                (now + self.interval, self.scope),
            )
        return 0

    def cool_down(self, retry_after: float | None) -> float:
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM mimo_request_limits WHERE scope = ?", (self.scope,)
            ).fetchone()
            streak = row["consecutive_limits"] + 1
            # 连续限流时按约 10、20、40、60 秒退避；服务商的等待要求优先。
            base = min(10 * 2 ** min(streak - 1, 3), 60)
            delay = max(min(base * random.uniform(1, 1.2), 60), retry_after or 0)
            now = time.time()
            until = max(row["cooldown_until"], now + delay)
            connection.execute(
                """
                UPDATE mimo_request_limits
                SET cooldown_until = ?, consecutive_limits = ? WHERE scope = ?
                """,
                (until, streak, self.scope),
            )
        return until - now

    def succeeded(self) -> None:
        with self.database.connection() as connection:
            # 已在途的成功请求不能取消另一请求刚触发的冷却。
            connection.execute(
                """
                UPDATE mimo_request_limits SET consecutive_limits = 0
                WHERE scope = ? AND cooldown_until <= ?
                """,
                (self.scope, time.time()),
            )
