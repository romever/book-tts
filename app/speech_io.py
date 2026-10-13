"""朗读稿导出与导入：在 Codex 等外部工具中整理朗读稿，校验通过后写回 Book TTS。

用法（在项目根目录执行）：
    .venv/bin/python -m app.speech_io list
    .venv/bin/python -m app.speech_io export --book <书籍ID> --style narrated
    .venv/bin/python -m app.speech_io check --dir <导出目录> [--chapter <章节文件名>]
    .venv/bin/python -m app.speech_io import --dir <导出目录>
"""

import argparse
import json
import re
import sys
from pathlib import Path

from .adapters import CLEANUP_RULES, NARRATION_RULES, validate_speech_texts
from .config import get_settings
from .database import Database, utc_now
from .domain.errors import PrecheckError
from .domain.types import SPEECH_STYLES, SegmentInput

STYLE_NAMES = {"faithful": "贴近原文", "narrated": "讲述改写"}


def speech_rules(style: str) -> str:
    """导出给外部工具的规则文件，与内置 LLM 流程使用同一份规则正文。"""
    rules = f"# 朗读稿整理规则（{STYLE_NAMES[style]}）\n\n{CLEANUP_RULES}\n"
    if style == "narrated":
        rules += (
            "\n在上述清理的基础上，再把每段朗读稿改写成讲述稿，speech_text 写最终的讲述稿：\n"
            + NARRATION_RULES
            + "\n"
        )
    return rules


def _chapter_filename(chapter) -> str:
    title = re.sub(r'[\\/:*?"<>|\s]+', "-", chapter["title"]).strip("-")[:40]
    return f"{chapter['chapter_index'] + 1:04d}-{title}.json"


def export_book(database: Database, book_id: str, style: str, out_dir: Path) -> Path:
    book = database.get_book(book_id)
    if book is None:
        raise SystemExit(f"找不到书籍：{book_id}")
    if style not in SPEECH_STYLES:
        raise SystemExit(f"风格只能是 {'/'.join(SPEECH_STYLES)}")
    if out_dir.exists():
        # 已有导出目录可能包含整理到一半的结果，不覆盖
        raise SystemExit(f"导出目录已存在，请继续在其中整理，或删除后重新导出：{out_dir}")
    chapters_dir = out_dir / "chapters"
    chapters_dir.mkdir(parents=True)
    chapter_files = []
    for chapter in database.get_chapters(book_id):
        segments = database.get_chapter_segments(chapter["id"])
        if not segments:
            continue
        filename = _chapter_filename(chapter)
        payload = {
            "chapter_id": chapter["id"],
            "chapter_index": chapter["chapter_index"],
            "title": chapter["title"],
            "segments": [
                {
                    "segment_id": segment["id"],
                    "segment_index": segment["segment_index"],
                    "text_sha256": segment["text_sha256"],
                    "text": segment["text"],
                    "speech_text": None,
                }
                for segment in segments
            ],
        }
        (chapters_dir / filename).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        chapter_files.append(filename)
    (out_dir / "rules.md").write_text(speech_rules(style), encoding="utf-8")
    manifest = {"book_id": book_id, "title": book["title"], "style": style, "chapters": chapter_files}
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return out_dir


def _load_manifest(export_dir: Path) -> dict:
    manifest_path = export_dir / "manifest.json"
    if not manifest_path.is_file():
        raise SystemExit(f"不是有效的导出目录（缺少 manifest.json）：{export_dir}")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def check_chapter(database: Database, book_id: str, chapter_path: Path) -> list[str]:
    """校验一章的整理结果，返回问题列表；为空表示可以导入。"""
    payload = json.loads(chapter_path.read_text(encoding="utf-8"))
    chapter = database.one(
        "SELECT * FROM chapters WHERE id = ? AND book_id = ?", (payload["chapter_id"], book_id)
    )
    if chapter is None:
        return ["章节已不存在，书稿可能重新识别过，需要重新导出"]
    current = {row["id"]: row for row in database.get_chapter_segments(chapter["id"])}
    items = payload["segments"]
    if [item["segment_id"] for item in items] != list(current):
        return ["分段与书稿不一致（有缺失、多余或顺序变化），不要增删或调换分段"]
    problems = []
    for item in items:
        # 编号与章节文件中的 segment_index 字段一致，便于定位
        label = f"分段 {item['segment_index']}"
        if item["text_sha256"] != current[item["segment_id"]]["text_sha256"]:
            problems.append(f"{label}：原文已变化，需要重新导出")
        if item["speech_text"] is None:
            problems.append(f"{label}：还没有整理")
        elif not isinstance(item["speech_text"], str):
            problems.append(f"{label}：speech_text 必须是字符串")
    if problems:
        return problems
    segments = {
        position: SegmentInput(
            id=item["segment_id"],
            chapter_id=chapter["id"],
            segment_index=item["segment_index"],
            text=current[item["segment_id"]]["text"],
        )
        for position, item in enumerate(items)
    }
    try:
        validate_speech_texts(
            segments, {item["segment_index"]: item["speech_text"] for item in items}
        )
    except PrecheckError as error:
        return [str(error)]
    return []


def import_book(database: Database, export_dir: Path) -> bool:
    manifest = _load_manifest(export_dir)
    book_id = manifest["book_id"]
    if database.get_active_execution(book_id) is not None:
        raise SystemExit("这本书有执行窗口正在运行或排队，请等待完成后再导入")
    all_passed = True
    imported = 0
    for filename in manifest["chapters"]:
        chapter_path = export_dir / "chapters" / filename
        problems = check_chapter(database, book_id, chapter_path)
        if problems:
            all_passed = False
            print(f"✗ {filename}：未导入")
            for problem in problems[:10]:
                print(f"    {problem}")
            continue
        payload = json.loads(chapter_path.read_text(encoding="utf-8"))
        segment_ids = [item["segment_id"] for item in payload["segments"]]
        for item in payload["segments"]:
            database.update_segment(item["segment_id"], speech_text=item["speech_text"].strip())
        # 朗读稿已替换，旧的自动发音提示位置失效；手动提示保留，与重新检查章节的行为一致
        database.delete_llm_hints_for_segments(segment_ids)
        database.execute(
            """
            UPDATE precheck_batches
            SET status = 'completed', error_message = NULL, error_context_json = NULL,
                finished_at = ?
            WHERE chapter_id = ?
            """,
            (utc_now(), payload["chapter_id"]),
        )
        imported += 1
        print(f"✓ {filename}：已导入 {len(segment_ids)} 段")
    if imported:
        # 书籍的朗读稿风格跟随导入结果，页面展示和之后的内置检查保持一致
        book = database.get_book(book_id)
        config = json.loads(book["config_json"] or "{}")
        config["speech_style"] = manifest["style"]
        database.update_book(book_id, config_json=json.dumps(config, ensure_ascii=False))
    return all_passed


def main() -> None:
    parser = argparse.ArgumentParser(description="朗读稿导出与导入")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="列出书籍 ID")
    export_parser = commands.add_parser("export", help="导出待整理的朗读稿")
    export_parser.add_argument("--book", required=True)
    export_parser.add_argument("--style", required=True, choices=SPEECH_STYLES)
    export_parser.add_argument("--out", type=Path, help="默认 DATA_DIR/speech-exports/<书籍ID>-<风格>")
    check_parser = commands.add_parser("check", help="只校验，不写入")
    check_parser.add_argument("--dir", type=Path, required=True)
    check_parser.add_argument("--chapter", help="只校验某一章的文件名")
    import_parser = commands.add_parser("import", help="校验并导入整理结果")
    import_parser.add_argument("--dir", type=Path, required=True)
    args = parser.parse_args()

    settings = get_settings()
    database = Database(settings.database_path)
    database.initialize()

    if args.command == "list":
        for book in database.list_books():
            print(f"{book['id']}  {book['title']}")
    elif args.command == "export":
        out_dir = args.out or settings.data_dir / "speech-exports" / f"{args.book}-{args.style}"
        export_book(database, args.book, args.style, out_dir)
        print(f"已导出到：{out_dir}\n规则文件：{out_dir / 'rules.md'}")
    elif args.command == "check":
        manifest = _load_manifest(args.dir)
        filenames = [args.chapter] if args.chapter else manifest["chapters"]
        failed = 0
        for filename in filenames:
            problems = check_chapter(database, manifest["book_id"], args.dir / "chapters" / filename)
            if problems:
                failed += 1
                print(f"✗ {filename}")
                for problem in problems[:10]:
                    print(f"    {problem}")
            else:
                print(f"✓ {filename}")
        sys.exit(1 if failed else 0)
    elif args.command == "import":
        sys.exit(0 if import_book(database, args.dir) else 1)


if __name__ == "__main__":
    main()
