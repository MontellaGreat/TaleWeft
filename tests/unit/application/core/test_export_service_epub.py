# Modified for TaleWeft on 2026-10-22: validate EPUB publication and navigation.
import io
import posixpath
from types import SimpleNamespace
import xml.etree.ElementTree as ET
from zipfile import ZIP_STORED, ZipFile

import pytest

from application.core.services.export_service import ExportService
from domain.novel.entities.chapter import Chapter
from domain.novel.entities.novel import Novel
from domain.novel.value_objects.novel_id import NovelId


NS = {
    "container": "urn:oasis:names:tc:opendocument:xmlns:container",
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
    "xhtml": "http://www.w3.org/1999/xhtml",
    "ncx": "http://www.daisy.org/z3986/2005/ncx/",
}


@pytest.fixture
def manuscript():
    novel = Novel(NovelId("book-id"), "雨城 <回声> & 夜", "作者 & 朋友", 2,
                  premise="雨落下。\n灯亮起。")
    first = Chapter("chapter-1", novel.novel_id, 1, '第一章 <雨> & "灯"',
                    "第一段 & 余音。\r\n<script>alert('x')</script>\n第二段 🙂")
    second = Chapter("chapter-2", novel.novel_id, 2, "第二章 归途", "终章。")
    novels = SimpleNamespace(get_by_id=lambda _: novel)
    chapters = SimpleNamespace(list_by_novel=lambda _: [second, first], get_by_id=lambda _: second)
    return ExportService(novels, chapters), novel, first, second


def publication(data):
    archive = ZipFile(io.BytesIO(data))
    container = ET.fromstring(archive.read("META-INF/container.xml"))
    rootfile = container.find("container:rootfiles/container:rootfile", NS)
    assert rootfile is not None
    package_path = rootfile.attrib["full-path"]
    package = ET.fromstring(archive.read(package_path))
    items = {item.attrib["id"]: item.attrib for item in package.findall("opf:manifest/opf:item", NS)}
    base = posixpath.dirname(package_path)
    paths = {key: posixpath.normpath(posixpath.join(base, item["href"])) for key, item in items.items()}
    return archive, package, items, paths


def test_epub_has_ocf_container_metadata_navigation_and_ordered_reading_spine(manuscript):
    service, novel, first, second = manuscript
    data, mime, filename = service.export_novel("book-id", "epub")
    assert mime == "application/epub+zip"
    assert filename.endswith(".epub") and "<" not in filename
    archive, package, items, paths = publication(data)
    with archive:
        entry = archive.infolist()[0]
        assert entry.filename == "mimetype" and entry.compress_type == ZIP_STORED
        assert entry.extra == b"" and archive.read(entry) == b"application/epub+zip"
        assert package.attrib["version"] == "3.0"
        assert package.findtext("opf:metadata/dc:title", namespaces=NS) == novel.title
        assert package.findtext("opf:metadata/dc:creator", namespaces=NS) == novel.author
        assert package.findtext("opf:metadata/dc:language", namespaces=NS) == "zh"
        identifier = package.find("opf:metadata/dc:identifier", NS)
        assert identifier.attrib["id"] == package.attrib["unique-identifier"]
        assert identifier.text == "taleweft:book-id"
        modified = package.find("opf:metadata/opf:meta[@property='dcterms:modified']", NS)
        assert modified is not None and modified.text.endswith("Z")

        nav_ids = [key for key, item in items.items() if "nav" in item.get("properties", "").split()]
        assert len(nav_ids) == 1
        nav_path = paths[nav_ids[0]]
        nav = ET.fromstring(archive.read(nav_path))
        links = nav.findall(".//xhtml:nav/xhtml:ol/xhtml:li/xhtml:a", NS)
        assert [link.text for link in links] == ["简介", first.title, second.title]
        destinations = [posixpath.normpath(posixpath.join(posixpath.dirname(nav_path), link.attrib["href"])) for link in links]
        spine = package.findall("opf:spine/opf:itemref", NS)
        reading_paths = [paths[item.attrib["idref"]] for item in spine if item.attrib.get("linear", "yes") != "no"]
        assert reading_paths == destinations
        # EPUBCheck OPF-096: readers must be able to reach non-linear navigation.
        reachable = set()
        for path in reading_paths:
            document = ET.fromstring(archive.read(path))
            for link in document.findall(".//xhtml:a[@href]", NS):
                reachable.add(posixpath.normpath(posixpath.join(
                    posixpath.dirname(path), link.attrib["href"].split("#", 1)[0])))
        assert nav_path in reachable
        assert set(paths.values()).issubset(archive.namelist())
        for path in paths.values():
            if path.endswith((".xhtml", ".ncx")):
                ET.fromstring(archive.read(path))


def test_epub_preserves_unicode_and_treats_author_text_as_plain_text(manuscript):
    service, novel, first, _ = manuscript
    archive, package, _, paths = publication(service.export_novel("book-id", "epub")[0])
    with archive:
        spine = package.findall("opf:spine/opf:itemref", NS)
        reading = [ET.fromstring(archive.read(paths[item.attrib["idref"]])) for item in spine if item.attrib.get("linear", "yes") != "no"]
        assert reading[0].findtext(".//xhtml:h1", namespaces=NS) == novel.title
        assert [p.text for p in reading[0].findall(".//xhtml:p", NS)][-2:] == ["雨落下。", "灯亮起。"]
        assert reading[1].findtext("xhtml:head/xhtml:title", namespaces=NS) == first.title
        assert [p.text for p in reading[1].findall(".//xhtml:p", NS)] == ["第一段 & 余音。", "<script>alert('x')</script>", "第二段 🙂"]
        assert not reading[1].findall(".//xhtml:script", NS)


def test_epub_retains_legacy_ncx_table_of_contents(manuscript):
    service, _, first, second = manuscript
    archive, package, items, paths = publication(service.export_novel("book-id", "epub")[0])
    with archive:
        ncx_ids = [key for key, item in items.items() if item["media-type"] == "application/x-dtbncx+xml"]
        assert len(ncx_ids) == 1
        assert package.find("opf:spine", NS).attrib["toc"] == ncx_ids[0]
        ncx = ET.fromstring(archive.read(paths[ncx_ids[0]]))
        assert [point.findtext("ncx:navLabel/ncx:text", namespaces=NS) for point in ncx.findall("ncx:navMap/ncx:navPoint", NS)] == ["简介", first.title, second.title]


def test_single_chapter_export_has_only_selected_chapter(manuscript):
    service, _, first, second = manuscript
    data, mime, filename = service.export_chapter(second.id, "epub")
    assert mime == "application/epub+zip" and filename.endswith("-第2章.epub")
    archive, _, items, paths = publication(data)
    with archive:
        texts = "\n".join(archive.read(path).decode() for key, path in paths.items() if items[key]["media-type"] == "application/xhtml+xml")
        assert second.title in texts and first.title not in texts


@pytest.mark.parametrize("with_chapter", [False, True])
def test_empty_book_or_chapter_still_produces_readable_epub(with_chapter):
    novel = Novel(NovelId("empty"), "", "", 1)
    chapters = [Chapter("empty-chapter", novel.novel_id, 7, "", "")] if with_chapter else []
    service = ExportService(SimpleNamespace(get_by_id=lambda _: novel),
                            SimpleNamespace(list_by_novel=lambda _: chapters))
    archive, package, items, paths = publication(service.export_novel("empty", "epub")[0])
    with archive:
        assert package.findtext("opf:metadata/dc:title", namespaces=NS) == "未命名"
        nav_id = next(key for key, item in items.items() if "nav" in item.get("properties", "").split())
        nav = ET.fromstring(archive.read(paths[nav_id]))
        labels = [link.text for link in nav.findall(".//xhtml:nav/xhtml:ol/xhtml:li/xhtml:a", NS)]
        assert labels == (["简介", "第 7 章"] if with_chapter else ["简介"])


def test_xml_forbidden_control_characters_cannot_corrupt_epub(manuscript):
    service, novel, first, _ = manuscript
    novel.title = "标题\x00尾部"
    first.title = "章名\x01尾部"
    first.update_content("正文\x00尾部\n保留\t制表与🙂")
    archive, package, _, paths = publication(service.export_novel("book-id", "epub")[0])
    with archive:
        assert package.findtext("opf:metadata/dc:title", namespaces=NS) == "标题\ufffd尾部"
        text = "\n".join(archive.read(path).decode() for path in paths.values())
        assert "正文\ufffd尾部" in text and "保留\t制表与🙂" in text
