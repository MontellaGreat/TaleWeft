# Modified for TaleWeft on 2026-10-22: product branding; compatibility identifiers retained.
"""Validated manuscript JSON transfer using the existing SQLite writer."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import time
from uuid import uuid4

FORMAT = 'plotpilot-manuscript'
MAX_BYTES = 32 * 1024 * 1024
COMMAND = 'IMPORT_MANUSCRIPT'


class ManuscriptValidationError(ValueError):
    def __init__(self, errors):
        self.errors = errors
        super().__init__('；'.join(errors))


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def text_hash(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def make_document(novel, chapters):
    document = {'format': FORMAT, 'format_version': 1, 'novel': {
        'title': novel.title, 'author': novel.author or '', 'premise': novel.premise or '',
        'target_chapters': max(novel.target_chapters, max((chapter.number for chapter in chapters), default=0)),
        'target_words_per_chapter': getattr(novel, 'target_words_per_chapter', 2500)},
        'chapters': [{'number': chapter.number, 'title': chapter.title or '', 'content': chapter.content or '',
            'outline': chapter.outline or '', 'status': getattr(chapter.status, 'value', chapter.status),
            'content_sha256': text_hash(chapter.content or '')} for chapter in sorted(chapters, key=lambda item: item.number)]}
    return {**document, 'payload_sha256': digest(document)}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'JSON字段重复：{key}')
        result[key] = value
    return result


def validate_document(raw):
    errors = []
    try:
        encoded = raw.encode('utf-8')
        if len(encoded) > MAX_BYTES:
            raise ValueError('稿件文件超过32MB')
        document = json.loads(raw.lstrip('\ufeff'), object_pairs_hook=_unique_object,
                              parse_constant=lambda value: (_ for _ in ()).throw(ValueError('JSON包含非有限数值')))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ManuscriptValidationError([f'稿件JSON解析失败：{exc}']) from exc
    if not isinstance(document, dict) or set(document) != {'format', 'format_version', 'novel', 'chapters', 'payload_sha256'}:
        raise ManuscriptValidationError(['顶层字段不符合稿件交换格式；不支持的字段不会被静默丢弃'])
    if document['format'] != FORMAT or type(document['format_version']) is not int or document['format_version'] != 1:
        errors.append('稿件格式或版本不支持')
    novel = document['novel']
    if not isinstance(novel, dict) or set(novel) != {'title', 'author', 'premise', 'target_chapters', 'target_words_per_chapter'}:
        raise ManuscriptValidationError(errors + ['作品元数据字段不匹配'])
    for field, maximum in [('title', 200), ('author', 200), ('premise', 100000)]:
        value = novel[field]
        if not isinstance(value, str) or len(value) > maximum or '\x00' in value:
            errors.append(f'作品{field}必须是有效文本，最大{maximum}字符')
    if isinstance(novel['title'], str) and not novel['title'].strip():
        errors.append('作品标题不能为空')
    for field, low, high in [('target_chapters', 0, 100000), ('target_words_per_chapter', 1, 100000)]:
        if type(novel[field]) is not int or not low <= novel[field] <= high:
            errors.append(f'{field}必须是{low}至{high}的整数')
    chapters = document['chapters']
    if not isinstance(chapters, list) or len(chapters) > 10000:
        raise ManuscriptValidationError(errors + ['章节列表无效或超过10000章'])
    numbers, count = set(), 0
    for index, chapter in enumerate(chapters):
        prefix = f'章节[{index + 1}]'
        if not isinstance(chapter, dict) or set(chapter) != {'number', 'title', 'content', 'outline', 'status', 'content_sha256'}:
            errors.append(prefix + '字段不匹配')
            continue
        number = chapter['number']
        if type(number) is not int or not 1 <= number <= 100000 or number in numbers:
            errors.append(prefix + '编号无效或重复')
        else:
            numbers.add(number)
        for field, maximum in [('title', 500), ('content', 4000000), ('outline', 100000)]:
            value = chapter[field]
            if not isinstance(value, str) or len(value) > maximum or '\x00' in value:
                errors.append(prefix + f'{field}必须是有效文本，最大{maximum}字符')
        if not isinstance(chapter['status'], str) or chapter['status'] not in {'draft', 'reviewing', 'completed'}:
            errors.append(prefix + '状态无效')
        if isinstance(chapter['content'], str):
            count += len(chapter['content'])
            if chapter['content_sha256'] != text_hash(chapter['content']):
                errors.append(prefix + '正文SHA-256校验不匹配')
    if type(novel['target_chapters']) is int and numbers and novel['target_chapters'] < max(numbers):
        errors.append('目标章节数量小于最大章节编号')
    try:
        if document['payload_sha256'] != digest({key: value for key, value in document.items() if key != 'payload_sha256'}):
            errors.append('稿件整体SHA-256校验不匹配')
    except (ValueError, UnicodeError, RecursionError):
        errors.append('稿件无法规范化')
    if errors:
        raise ManuscriptValidationError(errors[:100])
    warnings = []
    if sorted(numbers) != list(range(1, len(numbers) + 1)):
        warnings.append('章节编号存在间隔；将保留原编号，不自动重排')
    empty = sum(not chapter['content'].strip() for chapter in chapters)
    if empty:
        warnings.append(f'{empty}个章节没有正文，将按空稿保留')
    return document, {'status': 'valid', 'chapter_count': len(chapters), 'content_character_count': count,
        'empty_chapter_count': empty, 'warnings': warnings, 'errors': [],
        'payload_sha256': document['payload_sha256'], 'input_sha256': hashlib.sha256(encoded).hexdigest(),
        'chapters': [{'number': chapter['number'], 'title': chapter['title'], 'characters': len(chapter['content']),
                     'content_sha256': chapter['content_sha256']} for chapter in sorted(chapters, key=lambda item: item['number'])]}


def handle_manuscript_import(payload):
    from infrastructure.persistence.database.connection import get_database
    conn = get_database(payload['db_path']).get_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM manuscript_imports WHERE preview_id=?', (payload['preview_id'],)).fetchone()
        if not row:
            raise ValueError('导入预览不存在')
        row = dict(row)
        document, report = validate_document(row['document_json'])
        if row['payload_sha256'] != report['payload_sha256'] or payload['expected_payload_sha256'] != report['payload_sha256']:
            raise ValueError('确认的稿件版本与预览不同')
        if row['status'] == 'imported':
            conn.rollback()
            return json.loads(row['receipt_json'])
        if row['status'] != 'pending' or time.time() > row['expires_at']:
            raise ValueError('导入预览已过期，请重新验证稿件')
        novel_id = row['new_novel_id']
        if conn.execute('SELECT 1 FROM novels WHERE id=? OR slug=?', (novel_id, novel_id)).fetchone():
            raise ValueError('新作品编号已存在，导入已阻断')
        novel = document['novel']
        conn.execute('''INSERT INTO novels(id,slug,title,author,premise,target_chapters,target_words_per_chapter,
            autopilot_status,current_stage) VALUES (?,?,?,?,?,?,?,'stopped','planning')''',
            (novel_id, novel_id, novel['title'], novel['author'], novel['premise'], novel['target_chapters'], novel['target_words_per_chapter']))
        for chapter in document['chapters']:
            conn.execute('''INSERT INTO chapters(id,novel_id,number,title,content,outline,status,version_source)
                VALUES (?,?,?,?,?,?,?,'manuscript_import')''', (uuid4().hex, novel_id, chapter['number'], chapter['title'],
                chapter['content'], chapter['outline'], chapter['status']))
        actual = list(conn.execute('SELECT number,content FROM chapters WHERE novel_id=? ORDER BY number', (novel_id,)))
        expected = sorted(document['chapters'], key=lambda item: item['number'])
        if len(actual) != len(expected) or any(a['number'] != b['number'] or text_hash(a['content']) != b['content_sha256'] for a, b in zip(actual, expected)):
            raise ValueError('导入后正文数量或校验不匹配，全部变更已撤销')
        if list(conn.execute('PRAGMA foreign_key_check')):
            raise ValueError('导入后外键校验失败，全部变更已撤销')
        receipt = {'status': 'imported', 'preview_id': row['preview_id'], 'novel_id': novel_id,
            'title': novel['title'], 'chapter_count': report['chapter_count'], 'content_character_count': report['content_character_count'],
            'payload_sha256': report['payload_sha256'], 'completed_at': datetime.now(timezone.utc).isoformat(), 'original_novels_changed': False}
        conn.execute("UPDATE manuscript_imports SET status='imported',receipt_json=? WHERE preview_id=?", (canonical(receipt), row['preview_id']))
        conn.commit()
        return receipt
    except Exception:
        conn.rollback()
        raise


class ManuscriptTransfer:
    def __init__(self, db):
        self.db = db

    def preview(self, raw):
        document, report = validate_document(raw)
        preview_id, novel_id = uuid4().hex, uuid4().hex
        created = datetime.now(timezone.utc).isoformat()
        self.db.execute('''INSERT INTO manuscript_imports(preview_id,new_novel_id,document_json,payload_sha256,
            validation_json,created_at,expires_at) VALUES (?,?,?,?,?,?,?)''',
            (preview_id, novel_id, canonical(document), document['payload_sha256'], canonical(report), created, time.time() + 900))
        self.db.commit()
        return self.get(preview_id)

    def get(self, preview_id):
        row = self.db.fetch_one('SELECT * FROM manuscript_imports WHERE preview_id=?', (preview_id,))
        if row is None:
            raise LookupError('导入预览不存在')
        document = json.loads(row['document_json'])
        return {'preview_id': preview_id, 'new_novel_id': row['new_novel_id'], 'status': row['status'],
            'novel': document['novel'], 'validation': json.loads(row['validation_json']),
            'created_at': row['created_at'], 'expired': row['status'] == 'pending' and time.time() > row['expires_at'],
            'payload_sha256': row['payload_sha256'], 'receipt': json.loads(row['receipt_json']) if row['receipt_json'] else None}

    def confirm(self, preview_id, expected_payload_sha256):
        from application.engine.services.persistence_queue import get_persistence_queue, PersistenceCommitTimeout
        try:
            return get_persistence_queue().push_and_wait(COMMAND, {'db_path': self.db.db_path,
                'preview_id': preview_id, 'expected_payload_sha256': expected_payload_sha256}).result
        except PersistenceCommitTimeout:
            raise
        except Exception as exc:
            raise ValueError(str(exc)) from exc
