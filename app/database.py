import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS tts_defaults (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    provider TEXT NOT NULL,
    voice TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS books (
    id TEXT PRIMARY KEY,
    source_filename TEXT NOT NULL,
    source_path TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    title TEXT NOT NULL,
    author TEXT,
    status TEXT NOT NULL,
    stage TEXT NOT NULL,
    chapter_regex TEXT,
    config_json TEXT NOT NULL DEFAULT '{}',
    cover_path TEXT,
    output_path TEXT,
    error_message TEXT,
    lock_token TEXT,
    lock_until TEXT,
    run_until TEXT NOT NULL DEFAULT 'parse',
    stage_limit INTEGER NOT NULL DEFAULT 0,
    stage_current INTEGER NOT NULL DEFAULT 0,
    stage_total INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_books_status ON books(status, lock_until);
CREATE UNIQUE INDEX IF NOT EXISTS idx_books_source_sha256 ON books(source_sha256);

CREATE TABLE IF NOT EXISTS chapters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    chapter_index INTEGER NOT NULL,
    title TEXT NOT NULL,
    text_path TEXT NOT NULL,
    text_sha256 TEXT NOT NULL,
    char_count INTEGER NOT NULL,
    UNIQUE(book_id, chapter_index)
);

CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chapter_id INTEGER NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
    segment_index INTEGER NOT NULL,
    global_index INTEGER,
    text TEXT NOT NULL,
    speech_text TEXT NOT NULL DEFAULT '',
    text_sha256 TEXT NOT NULL,
    status TEXT NOT NULL,
    audio_path TEXT,
    audio_provider TEXT,
    audio_voice TEXT,
    audio_taken_over INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(chapter_id, segment_index)
);

CREATE INDEX IF NOT EXISTS idx_segments_status ON segments(status, chapter_id);

CREATE TABLE IF NOT EXISTS precheck_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    chapter_id INTEGER NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
    batch_index INTEGER NOT NULL,
    segment_ids_json TEXT NOT NULL,
    char_count INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    attempts INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    response_path TEXT,
    response_excerpt TEXT,
    error_context_json TEXT,
    started_at TEXT,
    finished_at TEXT,
    UNIQUE(book_id, batch_index)
);

CREATE INDEX IF NOT EXISTS idx_precheck_batches_book ON precheck_batches(book_id, batch_index);

CREATE TABLE IF NOT EXISTS execution_windows (
    id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    selection_mode TEXT NOT NULL,
    selection_json TEXT NOT NULL,
    resolved_start_sequence INTEGER NOT NULL,
    resolved_end_sequence INTEGER NOT NULL,
    actual_char_count INTEGER NOT NULL,
    chapter_count INTEGER NOT NULL,
    precheck_count INTEGER NOT NULL,
    segment_count INTEGER NOT NULL,
    estimated_duration_ms INTEGER NOT NULL,
    start_stage TEXT NOT NULL,
    end_stage TEXT NOT NULL,
    output_mode TEXT NOT NULL DEFAULT 'none',
    status TEXT NOT NULL DEFAULT 'queued',
    current_stage TEXT,
    current_item INTEGER NOT NULL DEFAULT 0,
    current_total INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    warning_message TEXT,
    sample_output_path TEXT,
    pause_requested INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    lock_token TEXT,
    lock_until TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_execution_windows_queue
ON execution_windows(status, lock_until, created_at);
CREATE INDEX IF NOT EXISTS idx_execution_windows_book
ON execution_windows(book_id, created_at);

CREATE TABLE IF NOT EXISTS chapter_outputs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chapter_id INTEGER NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
    output_path TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    duration_ms INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(chapter_id)
);

CREATE INDEX IF NOT EXISTS idx_chapter_outputs_status ON chapter_outputs(status, chapter_id);

CREATE TABLE IF NOT EXISTS batch_validation_runs (
    id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    batch_index INTEGER NOT NULL,
    start_batch_index INTEGER NOT NULL DEFAULT 0,
    end_batch_index INTEGER NOT NULL DEFAULT 0,
    current_batch_index INTEGER,
    status TEXT NOT NULL DEFAULT 'queued',
    stage TEXT NOT NULL DEFAULT 'precheck',
    attempts INTEGER NOT NULL DEFAULT 0,
    output_path TEXT,
    error_message TEXT,
    lock_token TEXT,
    lock_until TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_batch_validation_queue
ON batch_validation_runs(status, lock_until, created_at);
CREATE INDEX IF NOT EXISTS idx_batch_validation_book
ON batch_validation_runs(book_id, created_at);

CREATE TABLE IF NOT EXISTS pronunciation_hints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    chapter_id INTEGER NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
    segment_id INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
    surface TEXT NOT NULL,
    occurrence_index INTEGER NOT NULL,
    pronunciation TEXT NOT NULL,
    kind TEXT NOT NULL,
    confidence TEXT NOT NULL,
    source TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_hints_segment ON pronunciation_hints(segment_id, active);

CREATE TABLE IF NOT EXISTS job_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_job_attempts_book ON job_attempts(book_id, started_at);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.executescript(SCHEMA)
            existing_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(books)").fetchall()
            }
            for column, definition in (
                ("run_until", "TEXT NOT NULL DEFAULT 'parse'"),
                ("stage_limit", "INTEGER NOT NULL DEFAULT 0"),
                ("stage_current", "INTEGER NOT NULL DEFAULT 0"),
                ("stage_total", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if column not in existing_columns:
                    connection.execute(f"ALTER TABLE books ADD COLUMN {column} {definition}")
            segment_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(segments)").fetchall()
            }
            if "global_index" not in segment_columns:
                connection.execute("ALTER TABLE segments ADD COLUMN global_index INTEGER")
            if "audio_provider" not in segment_columns:
                connection.execute("ALTER TABLE segments ADD COLUMN audio_provider TEXT")
            if "audio_voice" not in segment_columns:
                connection.execute("ALTER TABLE segments ADD COLUMN audio_voice TEXT")
            if "audio_taken_over" not in segment_columns:
                connection.execute(
                    "ALTER TABLE segments ADD COLUMN audio_taken_over INTEGER NOT NULL DEFAULT 0"
                )
            speech_text_added = False
            if "speech_text" not in segment_columns:
                connection.execute(
                    "ALTER TABLE segments ADD COLUMN speech_text TEXT NOT NULL DEFAULT ''"
                )
                speech_text_added = True
            if speech_text_added:
                # 空朗读稿有“整段不朗读”的含义，只在首次加列时用原文填充旧数据
                connection.execute("UPDATE segments SET speech_text = text")
                # 旧版本没有朗读稿，已有预检和音频不能代表新的朗读稿链路，需重新执行；磁盘上的原始文件不删除。
                connection.execute(
                    """
                    UPDATE precheck_batches
                    SET status = 'queued', attempts = 0, error_message = NULL,
                        response_path = NULL, response_excerpt = NULL,
                        error_context_json = NULL, started_at = NULL, finished_at = NULL
                    """
                )
                connection.execute(
                    """
                    UPDATE segments
                    SET status = 'queued', attempts = 0, audio_path = NULL,
                        duration_ms = NULL, error_message = NULL, updated_at = ?
                    """,
                    (utc_now(),),
                )
                connection.execute(
                    """
                    UPDATE chapter_outputs
                    SET status = 'stale', duration_ms = NULL, error_message = NULL,
                        completed_at = NULL, updated_at = ?
                    """,
                    (utc_now(),),
                )
            batch_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(precheck_batches)").fetchall()
            }
            for column, definition in (
                ("response_path", "TEXT"),
                ("response_excerpt", "TEXT"),
                ("error_context_json", "TEXT"),
            ):
                if column not in batch_columns:
                    connection.execute(
                        f"ALTER TABLE precheck_batches ADD COLUMN {column} {definition}"
                    )
            validation_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(batch_validation_runs)").fetchall()
            }
            for column, definition in (
                ("start_batch_index", "INTEGER NOT NULL DEFAULT 0"),
                ("end_batch_index", "INTEGER NOT NULL DEFAULT 0"),
                ("current_batch_index", "INTEGER"),
            ):
                if column not in validation_columns:
                    connection.execute(
                        f"ALTER TABLE batch_validation_runs ADD COLUMN {column} {definition}"
                    )
            connection.execute(
                """
                UPDATE batch_validation_runs
                SET start_batch_index = batch_index,
                end_batch_index = batch_index
                WHERE start_batch_index = 0 AND end_batch_index = 0 AND batch_index != 0
                """
            )
            book_ids = [
                row["id"] for row in connection.execute("SELECT id FROM books").fetchall()
            ]
            for book_id in book_ids:
                book_row = connection.execute(
                    "SELECT config_json FROM books WHERE id = ?", (book_id,)
                ).fetchone()
                config = json.loads(book_row["config_json"] or "{}")
                changed = False
                if "tts_provider" not in config:
                    # 旧数据中的音色均来自 MiMo，通过一次迁移固化供应商语义。
                    config["tts_provider"] = "mimo"
                    changed = True
                if "speech_style" not in config:
                    # 朗读稿风格出现前的书按贴近原文处理，保持既有行为
                    config["speech_style"] = "faithful"
                    changed = True
                if changed:
                    connection.execute(
                        "UPDATE books SET config_json = ?, updated_at = ? WHERE id = ?",
                        (json.dumps(config, ensure_ascii=False), utc_now(), book_id),
                    )
                segment_rows = connection.execute(
                    """
                    SELECT s.id
                    FROM segments s
                    JOIN chapters c ON c.id = s.chapter_id
                    WHERE c.book_id = ?
                    ORDER BY c.chapter_index, s.segment_index
                    """,
                    (book_id,),
                ).fetchall()
                for global_index, segment_row in enumerate(segment_rows):
                    connection.execute(
                        "UPDATE segments SET global_index = ? WHERE id = ?",
                        (global_index, segment_row["id"]),
                    )
                connection.execute(
                    """
                    UPDATE books
                    SET status = 'attention', updated_at = ?
                    WHERE id = ? AND status = 'failed' AND stage != 'import'
                      AND EXISTS (SELECT 1 FROM chapters WHERE book_id = ?)
                    """,
                    (utc_now(), book_id, book_id),
                )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_segments_book_global ON segments(global_index, chapter_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_segments_global ON segments(chapter_id, global_index)"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_execution_windows_legacy ON execution_windows(id)"
            )
            legacy_runs = connection.execute(
                "SELECT * FROM batch_validation_runs WHERE id NOT IN (SELECT id FROM execution_windows)"
            ).fetchall()
            for legacy in legacy_runs:
                start_batch = int(legacy["start_batch_index"] or legacy["batch_index"])
                end_batch = int(legacy["end_batch_index"] or legacy["batch_index"])
                batch_rows = connection.execute(
                    """
                    SELECT * FROM precheck_batches
                    WHERE book_id = ? AND batch_index BETWEEN ? AND ?
                    ORDER BY batch_index
                    """,
                    (legacy["book_id"], start_batch, end_batch),
                ).fetchall()
                if not batch_rows:
                    continue
                segment_ids = []
                for batch_row in batch_rows:
                    segment_ids.extend(json.loads(batch_row["segment_ids_json"]))
                placeholders = ",".join("?" for _ in segment_ids)
                segments_for_run = connection.execute(
                    f"SELECT * FROM segments WHERE id IN ({placeholders}) ORDER BY global_index",
                    tuple(segment_ids),
                ).fetchall()
                if not segments_for_run:
                    continue
                created_at = legacy["created_at"] or utc_now()
                connection.execute(
                    """
                    INSERT INTO execution_windows (
                        id, book_id, selection_mode, selection_json,
                        resolved_start_sequence, resolved_end_sequence,
                        actual_char_count, chapter_count, precheck_count, segment_count,
                        estimated_duration_ms, start_stage, end_stage, output_mode,
                        status, current_stage, current_item, current_total,
                        error_message, sample_output_path, attempts, created_at, updated_at
                    ) VALUES (?, ?, 'advanced', ?, ?, ?, ?, ?, ?, ?, ?, 'precheck', 'package',
                              'sample', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        legacy["id"],
                        legacy["book_id"],
                        json.dumps(
                            {
                                "start_batch": start_batch + 1,
                                "end_batch": end_batch + 1,
                                "migrated_from": "batch_validation_runs",
                            },
                            ensure_ascii=False,
                        ),
                        segments_for_run[0]["global_index"],
                        segments_for_run[-1]["global_index"],
                        sum(len(row["text"]) for row in segments_for_run),
                        len({row["chapter_id"] for row in segments_for_run}),
                        len(batch_rows),
                        len(segments_for_run),
                        int(sum(len(row["text"]) for row in segments_for_run) / 4.5 * 1000),
                        legacy["status"],
                        legacy["stage"],
                        int(legacy["current_batch_index"] or 0),
                        len(batch_rows),
                        legacy["error_message"],
                        legacy["output_path"],
                        int(legacy["attempts"] or 0),
                        created_at,
                        legacy["updated_at"] or created_at,
                    ),
                )
            connection.commit()
            connection.execute("PRAGMA journal_mode = WAL")

    def one(self, query: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self.connection() as connection:
            return connection.execute(query, params).fetchone()

    def all(self, query: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self.connection() as connection:
            return connection.execute(query, params).fetchall()

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> int:
        with self.connection() as connection:
            cursor = connection.execute(query, params)
            return cursor.lastrowid

    def executescript(self, script: str) -> None:
        with self.connection() as connection:
            connection.executescript(script)

    def get_tts_defaults(self) -> sqlite3.Row | None:
        return self.one("SELECT provider, voice FROM tts_defaults WHERE id = 1")

    def save_tts_defaults(self, provider: str, voice: str) -> None:
        self.execute(
            "INSERT INTO tts_defaults (id, provider, voice) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET provider = excluded.provider, voice = excluded.voice",
            (provider, voice),
        )

    def create_book(self, record: dict[str, Any]) -> None:
        now = utc_now()
        self.execute(
            """
            INSERT INTO books (
                id, source_filename, source_path, source_sha256, title, author,
                status, stage, chapter_regex, config_json, run_until, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'uploaded', 'import', ?, ?, ?, ?, ?)
            """,
            (
                record["id"],
                record["source_filename"],
                record["source_path"],
                record["source_sha256"],
                record["title"],
                record.get("author"),
                record.get("chapter_regex"),
                json.dumps(record.get("config", {}), ensure_ascii=False),
                record.get("run_until", "parse"),
                now,
                now,
            ),
        )

    def update_book(self, book_id: str, **values: Any) -> None:
        if not values:
            return
        values["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in values)
        self.execute(f"UPDATE books SET {assignments} WHERE id = ?", (*values.values(), book_id))

    def claim_book(self, token: str, lease_seconds: int) -> sqlite3.Row | None:
        now = datetime.now(timezone.utc)
        now_string = now.isoformat()
        lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM books
                WHERE status IN ('uploaded', 'parsing')
                  AND (lock_until IS NULL OR lock_until < ?)
                ORDER BY updated_at ASC
                LIMIT 1
                """,
                (now_string,),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE books SET lock_token = ?, lock_until = ?, updated_at = ? WHERE id = ?",
                (token, lease_until, now_string, row["id"]),
            )
            return connection.execute("SELECT * FROM books WHERE id = ?", (row["id"],)).fetchone()

    def release_book(self, book_id: str, token: str) -> None:
        self.execute(
            "UPDATE books SET lock_token = NULL, lock_until = NULL, updated_at = ? WHERE id = ? AND lock_token = ?",
            (utc_now(), book_id, token),
        )

    def renew_book(self, book_id: str, token: str, lease_seconds: int) -> bool:
        now = datetime.now(timezone.utc)
        with self.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE books
                SET lock_until = ?, updated_at = ?
                WHERE id = ? AND lock_token = ?
                """,
                (
                    (now + timedelta(seconds=lease_seconds)).isoformat(),
                    now.isoformat(),
                    book_id,
                    token,
                ),
            )
            return cursor.rowcount == 1

    def insert_chapter(self, record: dict[str, Any]) -> int:
        return self.execute(
            """
            INSERT INTO chapters (book_id, chapter_index, title, text_path, text_sha256, char_count)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                record["book_id"],
                record["chapter_index"],
                record["title"],
                record["text_path"],
                record["text_sha256"],
                record["char_count"],
            ),
        )

    def get_book(self, book_id: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM books WHERE id = ?", (book_id,))

    def list_books(self) -> list[sqlite3.Row]:
        return self.all("SELECT * FROM books ORDER BY created_at DESC")

    def get_chapters(self, book_id: str) -> list[sqlite3.Row]:
        return self.all("SELECT * FROM chapters WHERE book_id = ? ORDER BY chapter_index", (book_id,))

    def get_segments(self, book_id: str) -> list[sqlite3.Row]:
        return self.all(
            """
            SELECT s.*, c.book_id, c.chapter_index, c.title AS chapter_title
            FROM segments s
            JOIN chapters c ON c.id = s.chapter_id
            WHERE c.book_id = ?
            ORDER BY s.global_index, c.chapter_index, s.segment_index
            """,
            (book_id,),
        )

    def get_precheck_batches(self, book_id: str) -> list[sqlite3.Row]:
        return self.all(
            "SELECT * FROM precheck_batches WHERE book_id = ? ORDER BY batch_index",
            (book_id,),
        )

    def ensure_precheck_batches(self, book_id: str, batches: list[list[sqlite3.Row]]) -> None:
        if self.get_precheck_batches(book_id):
            return
        with self.connection() as connection:
            for batch_index, batch in enumerate(batches):
                connection.execute(
                    """
                    INSERT INTO precheck_batches (
                        book_id, chapter_id, batch_index, segment_ids_json, char_count
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        book_id,
                        batch[0]["chapter_id"],
                        batch_index,
                        json.dumps([row["id"] for row in batch]),
                        sum(len(row["text"]) for row in batch),
                    ),
                )

    def reset_chapter_precheck_batches(self, chapter_id: int) -> None:
        """把章节的读音检查批次恢复为待检查，重新检查时会覆盖朗读稿和 LLM 提示。"""
        self.execute(
            """
            UPDATE precheck_batches
            SET status = 'queued', attempts = 0, error_message = NULL, response_path = NULL,
                response_excerpt = NULL, error_context_json = NULL, started_at = NULL,
                finished_at = NULL
            WHERE chapter_id = ?
            """,
            (chapter_id,),
        )

    def update_precheck_batch(self, batch_id: int, **values: Any) -> None:
        if not values:
            return
        assignments = ", ".join(f"{key} = ?" for key in values)
        self.execute(
            f"UPDATE precheck_batches SET {assignments} WHERE id = ?",
            (*values.values(), batch_id),
        )

    def get_chapter_segments(self, chapter_id: int) -> list[sqlite3.Row]:
        return self.all(
            "SELECT * FROM segments WHERE chapter_id = ? ORDER BY segment_index",
            (chapter_id,),
        )

    def create_execution(self, record: dict[str, Any]) -> str:
        execution_id = record.get("id") or uuid.uuid4().hex
        now = utc_now()
        self.execute(
            """
            INSERT INTO execution_windows (
                id, book_id, selection_mode, selection_json,
                resolved_start_sequence, resolved_end_sequence, actual_char_count,
                chapter_count, precheck_count, segment_count, estimated_duration_ms,
                start_stage, end_stage, output_mode, status, current_stage,
                current_item, current_total, error_message, warning_message,
                sample_output_path, pause_requested, attempts, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', NULL,
                      0, 0, NULL, ?, NULL, 0, 0, ?, ?)
            """,
            (
                execution_id,
                record["book_id"],
                record["selection_mode"],
                json.dumps(record["selection"], ensure_ascii=False),
                record["resolved_start_sequence"],
                record["resolved_end_sequence"],
                record["actual_char_count"],
                record["chapter_count"],
                record["precheck_count"],
                record["segment_count"],
                record["estimated_duration_ms"],
                record["start_stage"],
                record["end_stage"],
                record.get("output_mode", "none"),
                record.get("warning_message"),
                now,
                now,
            ),
        )
        return execution_id

    def get_execution(self, execution_id: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM execution_windows WHERE id = ?", (execution_id,))

    def get_executions(self, book_id: str, limit: int = 50) -> list[sqlite3.Row]:
        return self.all(
            """
            SELECT * FROM execution_windows
            WHERE book_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (book_id, limit),
        )

    def get_active_execution(self, book_id: str) -> sqlite3.Row | None:
        return self.one(
            """
            SELECT * FROM execution_windows
            WHERE book_id = ? AND status IN ('queued', 'running')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (book_id,),
        )

    def update_execution(self, execution_id: str, **values: Any) -> None:
        if not values:
            return
        values["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in values)
        self.execute(
            f"UPDATE execution_windows SET {assignments} WHERE id = ?",
            (*values.values(), execution_id),
        )

    def claim_execution(self, token: str, lease_seconds: int) -> sqlite3.Row | None:
        now = datetime.now(timezone.utc)
        now_string = now.isoformat()
        lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM execution_windows
                WHERE status IN ('queued', 'running')
                  AND (lock_until IS NULL OR lock_until < ?)
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (now_string,),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE execution_windows
                SET status = 'running', lock_token = ?, lock_until = ?,
                    attempts = attempts + 1, updated_at = ?
                WHERE id = ?
                """,
                (token, lease_until, now_string, row["id"]),
            )
            connection.execute(
                "UPDATE books SET status = 'executing', error_message = NULL, updated_at = ? WHERE id = ?",
                (now_string, row["book_id"]),
            )
            return connection.execute(
                "SELECT * FROM execution_windows WHERE id = ?", (row["id"],)
            ).fetchone()

    def renew_execution(self, execution_id: str, token: str, lease_seconds: int) -> bool:
        now = datetime.now(timezone.utc)
        with self.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE execution_windows
                SET lock_until = ?, updated_at = ?
                WHERE id = ? AND lock_token = ? AND status = 'running'
                """,
                (
                    (now + timedelta(seconds=lease_seconds)).isoformat(),
                    now.isoformat(),
                    execution_id,
                    token,
                ),
            )
            return cursor.rowcount == 1

    def release_execution(self, execution_id: str, token: str) -> None:
        self.execute(
            """
            UPDATE execution_windows
            SET lock_token = NULL, lock_until = NULL, updated_at = ?
            WHERE id = ? AND lock_token = ?
            """,
            (utc_now(), execution_id, token),
        )

    def get_execution_segments(self, execution_id: str) -> list[sqlite3.Row]:
        execution = self.get_execution(execution_id)
        if execution is None:
            return []
        return self.all(
            """
            SELECT s.*, c.book_id, c.chapter_index, c.title AS chapter_title
            FROM segments s
            JOIN chapters c ON c.id = s.chapter_id
            WHERE c.book_id = ? AND s.global_index BETWEEN ? AND ?
            ORDER BY s.global_index
            """,
            (
                execution["book_id"],
                execution["resolved_start_sequence"],
                execution["resolved_end_sequence"],
            ),
        )

    def get_chapter_outputs(self, book_id: str) -> list[sqlite3.Row]:
        return self.all(
            """
            SELECT co.*, c.book_id, c.chapter_index, c.title AS chapter_title
            FROM chapter_outputs co
            JOIN chapters c ON c.id = co.chapter_id
            WHERE c.book_id = ?
            ORDER BY c.chapter_index
            """,
            (book_id,),
        )

    def get_chapter_output(self, chapter_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM chapter_outputs WHERE chapter_id = ?", (chapter_id,))

    def upsert_chapter_output(self, chapter_id: int, **values: Any) -> None:
        current = self.get_chapter_output(chapter_id)
        if current is None:
            defaults = {
                "output_path": None,
                "status": "queued",
                "duration_ms": None,
                "attempts": 0,
                "error_message": None,
                "completed_at": None,
            }
            defaults.update(values)
            self.execute(
                """
                INSERT INTO chapter_outputs (
                    chapter_id, output_path, status, duration_ms, attempts,
                    error_message, completed_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chapter_id,
                    defaults["output_path"],
                    defaults["status"],
                    defaults["duration_ms"],
                    defaults["attempts"],
                    defaults["error_message"],
                    defaults["completed_at"],
                    utc_now(),
                ),
            )
            return
        values["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in values)
        self.execute(
            f"UPDATE chapter_outputs SET {assignments} WHERE chapter_id = ?",
            (*values.values(), chapter_id),
        )

    def create_batch_validation(self, book_id: str, start_batch_index: int, end_batch_index: int) -> str:
        if start_batch_index < 0 or end_batch_index < start_batch_index:
            raise ValueError("批次范围无效")
        run_id = uuid.uuid4().hex
        now = utc_now()
        self.execute(
            """
            INSERT INTO batch_validation_runs (
                id, book_id, batch_index, start_batch_index, end_batch_index,
                current_batch_index, status, stage, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, NULL, 'queued', 'precheck', ?, ?)
            """,
            (
                run_id,
                book_id,
                start_batch_index,
                start_batch_index,
                end_batch_index,
                now,
                now,
            ),
        )
        return run_id

    def get_batch_validation(self, run_id: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM batch_validation_runs WHERE id = ?", (run_id,))

    def get_batch_validations(self, book_id: str) -> list[sqlite3.Row]:
        return self.all(
            """
            SELECT * FROM batch_validation_runs
            WHERE book_id = ?
            ORDER BY created_at DESC
            LIMIT 30
            """,
            (book_id,),
        )

    def get_active_batch_validation(self, book_id: str) -> sqlite3.Row | None:
        return self.one(
            """
            SELECT * FROM batch_validation_runs
            WHERE book_id = ? AND status IN ('queued', 'running')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (book_id,),
        )

    def claim_batch_validation(self, token: str, lease_seconds: int) -> sqlite3.Row | None:
        now = datetime.now(timezone.utc)
        now_string = now.isoformat()
        lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM batch_validation_runs
                WHERE status IN ('queued', 'running')
                  AND (lock_until IS NULL OR lock_until < ?)
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (now_string,),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE batch_validation_runs
                SET status = 'running', lock_token = ?, lock_until = ?,
                    attempts = attempts + 1, updated_at = ?
                WHERE id = ?
                """,
                (token, lease_until, now_string, row["id"]),
            )
            return connection.execute(
                "SELECT * FROM batch_validation_runs WHERE id = ?", (row["id"],)
            ).fetchone()

    def renew_batch_validation(self, run_id: str, token: str, lease_seconds: int) -> bool:
        now = datetime.now(timezone.utc)
        with self.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE batch_validation_runs
                SET lock_until = ?, updated_at = ?
                WHERE id = ? AND lock_token = ? AND status = 'running'
                """,
                (
                    (now + timedelta(seconds=lease_seconds)).isoformat(),
                    now.isoformat(),
                    run_id,
                    token,
                ),
            )
            return cursor.rowcount == 1

    def release_batch_validation(self, run_id: str, token: str) -> None:
        self.execute(
            """
            UPDATE batch_validation_runs
            SET lock_token = NULL, lock_until = NULL, updated_at = ?
            WHERE id = ? AND lock_token = ?
            """,
            (utc_now(), run_id, token),
        )

    def update_batch_validation(self, run_id: str, **values: Any) -> None:
        if not values:
            return
        values["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in values)
        self.execute(
            f"UPDATE batch_validation_runs SET {assignments} WHERE id = ?",
            (*values.values(), run_id),
        )

    def delete_llm_hints_for_segments(self, segment_ids: list[int]) -> None:
        if not segment_ids:
            return
        placeholders = ", ".join("?" for _ in segment_ids)
        self.execute(
            f"DELETE FROM pronunciation_hints WHERE source = 'llm' AND segment_id IN ({placeholders})",
            tuple(segment_ids),
        )

    def get_segment(self, segment_id: int) -> sqlite3.Row | None:
        return self.one(
            """
            SELECT s.*, c.book_id, c.chapter_index, c.title AS chapter_title
            FROM segments s
            JOIN chapters c ON c.id = s.chapter_id
            WHERE s.id = ?
            """,
            (segment_id,),
        )

    def get_hints(self, segment_id: int, active_only: bool = False) -> list[sqlite3.Row]:
        condition = " AND active = 1" if active_only else ""
        return self.all(
            f"SELECT * FROM pronunciation_hints WHERE segment_id = ?{condition} ORDER BY id",
            (segment_id,),
        )

    def get_hint(self, hint_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM pronunciation_hints WHERE id = ?", (hint_id,))

    def update_hint(self, hint_id: int, **values: Any) -> None:
        if not values:
            return
        values["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in values)
        self.execute(
            f"UPDATE pronunciation_hints SET {assignments} WHERE id = ?",
            (*values.values(), hint_id),
        )

    def update_segment(self, segment_id: int, **values: Any) -> None:
        if not values:
            return
        values["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in values)
        self.execute(f"UPDATE segments SET {assignments} WHERE id = ?", (*values.values(), segment_id))

    def delete_book_content(self, book_id: str) -> None:
        with self.connection() as connection:
            connection.execute("DELETE FROM execution_windows WHERE book_id = ?", (book_id,))
            connection.execute("DELETE FROM batch_validation_runs WHERE book_id = ?", (book_id,))
            connection.execute("DELETE FROM chapters WHERE book_id = ?", (book_id,))

    def delete_llm_hints(self, book_id: str) -> None:
        self.execute("DELETE FROM pronunciation_hints WHERE book_id = ? AND source = 'llm'", (book_id,))

    def reset_failed_segments(self, book_id: str) -> None:
        self.execute(
            """
            UPDATE segments
            SET status = 'queued', attempts = 0, error_message = NULL, updated_at = ?
            WHERE chapter_id IN (SELECT id FROM chapters WHERE book_id = ?) AND status = 'failed'
            """,
            (utc_now(), book_id),
        )

    def reset_segment(self, segment_id: int) -> None:
        self.update_segment(
            segment_id,
            status="queued",
            attempts=0,
            error_message=None,
            audio_path=None,
            audio_provider=None,
            audio_voice=None,
            audio_taken_over=0,
            duration_ms=None,
        )

    def update_book_tts_config(self, book_id: str, provider: str, voice: str) -> None:
        """只更新书籍的配音设置；已生成的分段保留原音频，新设置用于之后生成的分段。"""
        now = utc_now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            book = connection.execute(
                "SELECT config_json FROM books WHERE id = ?", (book_id,)
            ).fetchone()
            if book is None:
                raise ValueError("找不到书籍")
            active = connection.execute(
                """
                SELECT id FROM execution_windows
                WHERE book_id = ? AND status IN ('queued', 'running')
                LIMIT 1
                """,
                (book_id,),
            ).fetchone()
            if active is not None:
                raise ValueError("当前仍有执行窗口在运行，不能修改配音设置")

            config = json.loads(book["config_json"] or "{}")
            config.update({"tts_provider": provider, "voice": voice})
            connection.execute(
                "UPDATE books SET config_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(config, ensure_ascii=False), now, book_id),
            )

    def stage_counts(self, book_id: str) -> dict[str, int]:
        rows = self.all(
            """
            SELECT s.status, COUNT(*) AS count
            FROM segments s
            JOIN chapters c ON c.id = s.chapter_id
            WHERE c.book_id = ?
            GROUP BY s.status
            """,
            (book_id,),
        )
        return {row["status"]: row["count"] for row in rows}

    def get_attempts(self, book_id: str) -> list[sqlite3.Row]:
        return self.all(
            "SELECT * FROM job_attempts WHERE book_id = ? ORDER BY started_at DESC LIMIT 30",
            (book_id,),
        )

    def insert_segment(self, record: dict[str, Any]) -> int:
        return self.execute(
            """
            INSERT INTO segments (
                chapter_id, segment_index, global_index, text, speech_text, text_sha256, status, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?)
            """,
            (
                record["chapter_id"],
                record["segment_index"],
                record.get("global_index"),
                record["text"],
                record.get("speech_text", record["text"]),
                record["text_sha256"],
                utc_now(),
            ),
        )

    def insert_hint(self, record: dict[str, Any]) -> int:
        return self.execute(
            """
            INSERT INTO pronunciation_hints (
                book_id, chapter_id, segment_id, surface, occurrence_index,
                pronunciation, kind, confidence, source, active, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record["book_id"],
                record["chapter_id"],
                record["segment_id"],
                record["surface"],
                record["occurrence_index"],
                record["pronunciation"],
                record["kind"],
                record["confidence"],
                record["source"],
                1 if record.get("active", True) else 0,
                utc_now(),
                utc_now(),
            ),
        )

    def start_attempt(self, book_id: str, stage: str) -> int:
        return self.execute(
            "INSERT INTO job_attempts (book_id, stage, status, started_at) VALUES (?, ?, 'running', ?)",
            (book_id, stage, utc_now()),
        )

    def finish_attempt(self, attempt_id: int, status: str, error_message: str | None = None) -> None:
        self.execute(
            "UPDATE job_attempts SET status = ?, finished_at = ?, error_message = ? WHERE id = ?",
            (status, utc_now(), error_message, attempt_id),
        )
