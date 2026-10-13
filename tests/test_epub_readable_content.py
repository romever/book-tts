import tempfile
import unittest
from pathlib import Path

from ebooklib import epub

from app.domain.parser import parse_epub


def _paragraphs(*values: str) -> str:
    return "".join(f"<p>{value}</p>" for value in values)


class EpubReadableContentTest(unittest.TestCase):
    def test_only_keeps_narratable_documents_and_trims_structural_prefixes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sample.epub"
            book = epub.EpubBook()
            book.set_identifier("readable-content-fixture")
            book.set_title("测试书稿")
            book.set_language("zh-CN")

            cover = epub.EpubHtml(uid="cover", file_name="cover.xhtml", title="Cover")
            cover.content = "<html><body><img src='cover.jpg'/></body></html>"

            intro = epub.EpubHtml(uid="intro", file_name="intro.xhtml", title="Intro")
            intro.content = "<html><body>" + _paragraphs(
                "测试书稿",
                "某出版社",
                "ISBN 000-0-00-000000-0",
                "出版与印刷信息",
                "总序",
                "这是第一段适合连续朗读的正文。它介绍了全书的主题，也给读者说明了写作背景。作者从历史脉络开始展开，并逐步交代这本书所要讨论的核心问题。",
                "这是第二段适合连续朗读的正文。它与上一段语义连续，并且包含完整的句子。读者能够自然理解前因后果，不需要借助版式或页码才能继续阅读。",
                "这是第三段正文，用来证明这里不是只有标题或出版字段，而是真正的叙述内容。它延续前文观点，并为下一节内容建立清晰而连贯的过渡。",
            ) + "</body></html>"

            chapter = epub.EpubHtml(uid="chapter", file_name="chapter.xhtml", title="Chapter")
            chapter.content = "<html><body>" + _paragraphs(
                "目录",
                "总序 / 1",
                "第一章 / 10",
                "参考资料 / 99",
                "第一章",
                "真正的开始",
                "这一段开始进入故事正文，应该被保留下来用于朗读。这里有完整的句子和自然的语义，也能够脱离原始页面版式独立表达作者的观点。",
                "第二段继续讲述故事的发展，也应当成为生成语音时使用的书稿内容。上下文之间具有明确联系，朗读时不会变成孤立的标题或编号。",
                "第三段保持连续叙述，确保正文起点能够通过段落结构稳定识别。它继续补充必要细节，让整个章节形成可连贯收听的内容。",
            ) + "</body></html>"

            references = epub.EpubHtml(uid="references", file_name="references.xhtml", title="References")
            reference_items = [
                f"Author {number}. Collected Documents, Volume {number}, Archive Press, 19{number:02d}."
                for number in range(30)
            ]
            references.content = "<html><body>" + _paragraphs("资料", *reference_items) + "</body></html>"

            navigation = epub.EpubHtml(uid="navigation", file_name="navigation.xhtml", title="Navigation")
            navigation.content = """
                <html><body><nav epub:type="toc">
                <ol><li><a href="intro.xhtml">总序</a></li>
                <li><a href="chapter.xhtml">第一章</a></li>
                <li><a href="references.xhtml">资料</a></li></ol>
                </nav></body></html>
            """
            navigation.properties.append("nav")

            for item in (cover, intro, chapter, references, navigation):
                book.add_item(item)
            book.add_item(epub.EpubNcx())
            book.spine = [cover, intro, chapter, references, navigation]
            book.guide = [
                {"type": "cover", "title": "Cover", "href": "cover.xhtml"},
                {"type": "toc", "title": "Contents", "href": "navigation.xhtml"},
            ]
            epub.write_epub(str(source), book)

            parsed = parse_epub(source, root)

            self.assertEqual(2, len(parsed.chapters))
            self.assertEqual("总序", parsed.chapters[0].text.splitlines()[0])
            self.assertNotIn("ISBN", parsed.chapters[0].text)
            self.assertEqual("第一章", parsed.chapters[1].text.splitlines()[0])
            self.assertNotIn("目录", parsed.chapters[1].text)
            self.assertNotIn("Author A", "\n".join(item.text for item in parsed.chapters))


if __name__ == "__main__":
    unittest.main()
