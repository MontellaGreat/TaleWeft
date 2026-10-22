"""Real-browser QA must not start against author data or unfinished paid work."""
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location('live_browser_edit_under_test', ROOT / 'scripts/verification/live_browser_edit.py')
driver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(driver)


def test_browser_launcher_rejects_author_data_and_overlapping_run_directories(tmp_path, monkeypatch):
    verification = tmp_path / 'project' / 'out' / 'verification'
    monkeypatch.setattr(driver, 'VERIFICATION', verification)
    with pytest.raises(driver.CheckFailed, match='isolated run'):
        driver.validate_paths(tmp_path / 'author-data', verification / 'browser')
    with pytest.raises(driver.CheckFailed, match='separate sibling'):
        driver.validate_paths(verification / 'source', verification / 'source' / 'browser')
    with pytest.raises(driver.CheckFailed, match='separate sibling'):
        driver.validate_paths(verification / 'source' / 'nested', verification / 'source')


def make_live_source(tmp_path, cascade_status):
    source = tmp_path / 'source'
    (source / 'data').mkdir(parents=True)
    (source / 'checkpoint.json').write_text(json.dumps({'novel_id': 'live-test', 'passed': ['chapter_1', 'chapter_2']}), encoding='utf-8')
    with sqlite3.connect(source / 'data' / 'plotpilot.db') as db:
        db.executescript('''
            CREATE TABLE novels(id TEXT);
            INSERT INTO novels VALUES('live-test');
            CREATE TABLE chapters(id TEXT,novel_id TEXT,number INTEGER,content TEXT,revision INTEGER);
            INSERT INTO chapters VALUES('c1','live-test',1,'real body 1',1),('c2','live-test',2,'real body 2',1);
            CREATE TABLE chapter_derivation_jobs(chapter_id TEXT,novel_id TEXT,revision INTEGER,status TEXT,cascade_status TEXT);
            CREATE TABLE memory_atoms(id TEXT,novel_id TEXT,chapter_number INTEGER,status TEXT,source TEXT);
            INSERT INTO memory_atoms VALUES('old-atom','live-test',1,'candidate','chapter_extract');
        ''')
        db.executemany('INSERT INTO chapter_derivation_jobs VALUES(?,?,?,?,?)', [
            ('c1', 'live-test', 1, 'completed', cascade_status),
            ('c2', 'live-test', 1, 'completed', 'completed'),
        ])
    return source


@pytest.mark.parametrize('cascade', ['running', 'unavailable'])
def test_unfinished_cascade_cannot_be_copied_or_start_a_new_paid_browser_run(tmp_path, cascade):
    source = make_live_source(tmp_path, cascade)
    output = tmp_path / 'browser'
    with pytest.raises(driver.CheckFailed, match='completed'):
        driver.copy_live_run(source, output)
    assert not output.exists()


def test_browser_data_is_independent_snapshot_of_the_live_test_not_author_database(tmp_path):
    source = make_live_source(tmp_path, 'completed')
    output = tmp_path / 'browser'
    novel, atoms = driver.copy_live_run(source, output)
    assert novel == 'live-test' and atoms == ['old-atom']
    with sqlite3.connect(output / 'data' / 'plotpilot.db') as copied:
        copied.execute("UPDATE chapters SET content='browser edit' WHERE number=1")
    with sqlite3.connect(source / 'data' / 'plotpilot.db') as original:
        assert original.execute('SELECT content FROM chapters WHERE number=1').fetchone()[0] == 'real body 1'


class PublicPropClient:
    def __init__(self):
        self.prop = None
        self.writes = []

    def json(self, route, method='GET', payload=None):
        if method == 'POST':
            self.writes.append((route, method, payload))
            self.prop = {'id': 'key', **payload}
            return self.prop
        if route.endswith('/props'):
            return [self.prop] if self.prop else []
        return self.prop


def make_baseline_db(tmp_path, content):
    database = tmp_path / 'baseline.db'
    with sqlite3.connect(database) as db:
        db.executescript('CREATE TABLE chapters(novel_id TEXT,number INTEGER,content TEXT); CREATE TABLE unified_characters(id TEXT,novel_id TEXT,name TEXT,last_updated_chapter INTEGER);')
        db.execute('INSERT INTO chapters VALUES(?,?,?)', ('n', 1, content))
        db.execute('INSERT INTO unified_characters VALUES(?,?,?,?)', ('canonical-shen', 'n', '沈舟', 1))
    return database


def test_registered_prop_baseline_is_explicit_public_api_setup_without_sql_seeding(tmp_path):
    content = '钥匙现在由沈舟独自保管，林雁没有钥匙。'
    database = make_baseline_db(tmp_path, content)
    client = PublicPropClient()
    evidence = driver.setup_registered_prop_baseline(client, database, 'n')
    assert evidence['automatic_prop_discovery_tested'] is False
    assert evidence['database_writes_by_driver'] is False
    assert evidence['model_calls_by_setup'] == 0
    assert len(client.writes) == 1
    route, method, payload = client.writes[0]
    assert route == '/api/v1/novels/n/props' and method == 'POST'
    assert payload['holder_character_id'] == 'canonical-shen' and payload['introduced_chapter'] == 1
    with sqlite3.connect(database) as db:
        assert db.execute('SELECT content FROM chapters').fetchone()[0] == content
        assert db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='unified_props'").fetchone()[0] == 0


def test_baseline_registration_cannot_guess_final_holder_from_a_historical_handoff(tmp_path):
    database = make_baseline_db(tmp_path, '沈舟曾将钥匙交给林雁。')
    client = PublicPropClient()
    with pytest.raises(driver.CheckFailed, match='end with Shen'):
        driver.setup_registered_prop_baseline(client, database, 'n')
    assert client.writes == []
