from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import sqlite3

from tests.unit.engine.test_daemon_rewrite_candidate_integrity import daemon_writer
from infrastructure.persistence.database.sqlite_voice_fingerprint_repository import SQLiteVoiceFingerprintRepository
from infrastructure.persistence.database.sqlite_voice_vault_repository import SqliteVoiceVaultRepository
from application.analyst.services.voice_fingerprint_service import VoiceFingerprintService
from application.analyst.services.voice_sample_service import VoiceSampleService


def test_tenth_sample_commits_fingerprint_and_does_not_leave_database_locked(daemon_writer):
    db = daemon_writer[0]
    repo = SQLiteVoiceFingerprintRepository(db)
    samples = VoiceSampleService(SqliteVoiceVaultRepository(db), VoiceFingerprintService(repo, SqliteVoiceVaultRepository(db)))
    with ThreadPoolExecutor(max_workers=1) as executor:
        for number in range(1, 11):
            executor.submit(samples.append_sample, 'novel', number, 'general', 'AI原稿', '漂亮的天气。作者原稿。').result()
    with closing(sqlite3.connect(db.db_path, timeout=1)) as conn:
        assert conn.execute("SELECT sample_count FROM voice_fingerprint WHERE novel_id='novel'").fetchone()[0] == 10
    db.execute("UPDATE novels SET title='指纹提交后可继续编辑' WHERE id='novel'")
    assert db.fetch_one("SELECT title FROM novels WHERE id='novel'")['title'] == '指纹提交后可继续编辑'


def test_parallel_fingerprint_upserts_are_one_writer_transaction_and_one_record(daemon_writer):
    db = daemon_writer[0]
    repo = SQLiteVoiceFingerprintRepository(db)
    data = {'metrics': {'adjective_density': .2, 'avg_sentence_length': 12, 'sentence_count': 10}, 'sample_count': 10}
    with ThreadPoolExecutor(max_workers=2) as executor:
        ids = list(executor.map(lambda _: repo.upsert('novel', data), range(2)))
    assert ids[0] == ids[1]
    assert db.fetch_one("SELECT COUNT(*) count FROM voice_fingerprint WHERE novel_id='novel'")['count'] == 1
