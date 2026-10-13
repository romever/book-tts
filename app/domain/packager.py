import re
import shutil
import subprocess
from pathlib import Path

from .errors import PackagingError


def safe_name(value: str, fallback: str) -> str:
    normalized = re.sub(r"[^\w\u4e00-\u9fff.-]+", "-", value.strip(), flags=re.UNICODE).strip(".-")
    return normalized or fallback


def _escape_ffconcat(path: Path) -> str:
    return str(path.resolve()).replace("'", "'\\''")


def _escape_metadata(value: str) -> str:
    return value.replace("\\", "\\\\").replace("=", "\\=").replace(";", "\\;").replace("#", "\\#").replace("\n", "\\n")


def _probe_duration_ms(path: Path) -> int:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return int(float(result.stdout.strip()) * 1000)
    except (FileNotFoundError, subprocess.CalledProcessError, ValueError) as error:
        raise PackagingError(f"无法读取音频时长：{path.name}") from error


class FfmpegM4bPackager:
    def package_chapter(
        self,
        *,
        book_id: str,
        book_title: str,
        author: str | None,
        chapter: dict,
        segments: list[dict],
        cover_path: Path | None,
        working_dir: Path,
        output_dir: Path,
    ) -> tuple[Path, int]:
        if not segments or any(not segment.get("audio_path") for segment in segments):
            raise PackagingError(f"第 {chapter['chapter_index'] + 1} 章还有分段没有生成音频")
        output_book_dir = self._book_output_dir(output_dir, author, book_title)
        output_book_dir.mkdir(parents=True, exist_ok=True)
        self._copy_cover(cover_path, output_book_dir)
        chapter_name = safe_name(chapter["title"], f"chapter-{chapter['chapter_index'] + 1:04d}")
        output_path = output_book_dir / f"{chapter['chapter_index'] + 1:04d}-{chapter_name}.m4b"
        duration_ms = self._encode_audio(
            title=chapter["title"],
            author=author,
            segments=sorted(segments, key=lambda item: item["segment_index"]),
            cover_path=cover_path,
            working_dir=working_dir / f"chapter-{chapter['chapter_index'] + 1:04d}",
            output_path=output_path,
            chapters=[chapter],
        )
        return output_path, duration_ms

    def package_sample(
        self,
        *,
        title: str,
        author: str | None,
        chapters: list[dict],
        segments: list[dict],
        cover_path: Path | None,
        working_dir: Path,
        output_path: Path,
    ) -> tuple[Path, int]:
        if not segments or any(not segment.get("audio_path") for segment in segments):
            raise PackagingError("样例范围还有分段没有生成音频")
        duration_ms = self._encode_audio(
            title=title,
            author=author,
            segments=sorted(segments, key=lambda item: item.get("global_index", item["segment_index"])),
            cover_path=cover_path,
            working_dir=working_dir,
            output_path=output_path,
            chapters=chapters,
        )
        return output_path, duration_ms

    @staticmethod
    def _book_output_dir(output_dir: Path, author: str | None, title: str) -> Path:
        author_dir = safe_name(author or "未知作者", "unknown-author")
        book_dir = safe_name(title, "untitled-book")
        return output_dir / author_dir / book_dir

    @staticmethod
    def _copy_cover(cover_path: Path | None, output_dir: Path) -> None:
        if cover_path and cover_path.is_file():
            destination = output_dir / "cover.jpg"
            if cover_path.suffix.lower() in {".jpg", ".jpeg"}:
                shutil.copyfile(cover_path, destination)
            else:
                try:
                    subprocess.run(
                        [
                            "ffmpeg",
                            "-y",
                            "-hide_banner",
                            "-loglevel",
                            "error",
                            "-i",
                            str(cover_path),
                            "-frames:v",
                            "1",
                            str(destination),
                        ],
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                except FileNotFoundError as error:
                    raise PackagingError("系统中没有找到 ffmpeg，无法转换封面") from error
                except subprocess.CalledProcessError as error:
                    raise PackagingError(f"封面转换失败：{error.stderr.strip()}") from error

    def _encode_audio(
        self,
        *,
        title: str,
        author: str | None,
        segments: list[dict],
        cover_path: Path | None,
        working_dir: Path,
        output_path: Path,
        chapters: list[dict] | None = None,
    ) -> int:
        working_dir.mkdir(parents=True, exist_ok=True)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        concat_path = working_dir / "concat.txt"
        concat_path.write_text(
            "\n".join(
                f"file '{_escape_ffconcat(Path(segment['audio_path']))}'" for segment in segments
            )
            + "\n",
            encoding="utf-8",
        )
        merged_wav = working_dir / "merged.wav"
        metadata_path = working_dir / "metadata.txt"
        self._run_ffmpeg(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_path),
                "-c",
                "copy",
                str(merged_wav),
            ],
            "音频分段拼接失败",
        )
        duration_ms = sum(
            int(segment.get("duration_ms") or _probe_duration_ms(Path(segment["audio_path"])))
            for segment in segments
        )
        metadata_path.write_text(
            self._build_metadata(
                title,
                author,
                chapters or [{"id": 0, "chapter_index": 0, "title": title}],
                segments,
            ),
            encoding="utf-8",
        )
        temporary_output = output_path.with_name(output_path.stem + ".tmp" + output_path.suffix)
        command = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(merged_wav),
        ]
        if cover_path:
            command.extend(["-i", str(cover_path)])
        metadata_input_index = 2 if cover_path else 1
        command.extend(
            [
                "-f",
                "ffmetadata",
                "-i",
                str(metadata_path),
                "-map",
                "0:a:0",
            ]
        )
        if cover_path:
            command.extend(
                [
                    "-map",
                    "1:v:0",
                    "-c:v",
                    "mjpeg",
                    "-disposition:v:0",
                    "attached_pic",
                ]
            )
        command.extend(
            [
                "-map_metadata",
                str(metadata_input_index),
                "-map_chapters",
                str(metadata_input_index),
                "-c:a",
                "aac",
                "-b:a",
                "64k",
                "-ar",
                "24000",
                "-ac",
                "1",
                "-movflags",
                "+faststart",
                str(temporary_output),
            ]
        )
        self._run_ffmpeg(command, "M4B 打包失败")
        temporary_output.replace(output_path)
        return duration_ms

    @staticmethod
    def _build_metadata(
        title: str,
        author: str | None,
        chapters: list[dict],
        segments: list[dict],
    ) -> str:
        lines = [
            ";FFMETADATA1",
            f"title={_escape_metadata(title)}",
        ]
        if author:
            lines.append(f"artist={_escape_metadata(author)}")
        elapsed = 0
        segments_by_chapter: dict[int, list[dict]] = {}
        for segment in segments:
            segments_by_chapter.setdefault(segment["chapter_id"], []).append(segment)
        for chapter in sorted(chapters, key=lambda item: item["chapter_index"]):
            chapter_segments = segments_by_chapter.get(chapter["id"], [])
            duration = sum(
                int(segment.get("duration_ms") or _probe_duration_ms(Path(segment["audio_path"])))
                for segment in chapter_segments
            )
            start = elapsed
            elapsed += duration
            end = max(start + 1, elapsed)
            lines.extend(
                [
                    "[CHAPTER]",
                    "TIMEBASE=1/1000",
                    f"START={start}",
                    f"END={end}",
                    f"title={_escape_metadata(chapter['title'])}",
                ]
            )
        return "\n".join(lines) + "\n"

    @staticmethod
    def _run_ffmpeg(command: list[str], message: str) -> None:
        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
        except FileNotFoundError as error:
            raise PackagingError("系统中没有找到 ffmpeg/ffprobe") from error
        except subprocess.CalledProcessError as error:
            detail = error.stderr.strip()
            raise PackagingError(f"{message}：{detail}") from error
