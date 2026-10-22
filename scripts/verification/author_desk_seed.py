"""Idempotent, self-authored fixture through existing TaleWeft HTTP APIs.

No author novels are copied. Seed creation does not call a model.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
PREVIEW = ROOT / 'out' / 'author-desk-preview'
NOVEL = 'author-desk-fog-harbor'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-db', type=Path)
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    data = PREVIEW / 'data'
    data.mkdir(parents=True, exist_ok=True)
    os.environ.update(TALEWEFT_PROD_DATA_DIR=str(data), PLOTPILOT_PROD_DATA_DIR=str(data),
                      PLOTPILOT_SESSION_TOKEN=secrets.token_urlsafe(32))
    if args.source_db and not (data / 'plotpilot.db').exists():
        from scripts.verification.live_novel_fullflow import copy_configuration
        copy_configuration(args.source_db, data / 'plotpilot.db')
    if args.live:
        import httpx
        import re
        private_file = PREVIEW / 'session-private.url'
        log_bytes = (private_file if private_file.exists() else PREVIEW / 'launch-private.log').read_bytes()
        log = log_bytes.decode('utf-16' if log_bytes.startswith(b'\xff\xfe') else 'utf-8-sig')
        launch = re.search(r'http://127\.0\.0\.1:(\d+)/#plotpilot-session=([A-Za-z0-9_-]+)', log)
        if not launch or not 1 <= int(launch[1]) <= 65535:
            raise RuntimeError('Start the local preview before checking its sample.')
        client_context = httpx.Client(trust_env=False, base_url='http://127.0.0.1:' + launch[1], headers={'Authorization': 'Bearer ' + launch[2]}, timeout=60)
    else:
        from fastapi.testclient import TestClient
        from interfaces.main import app
        client_context = TestClient(app, base_url='http://127.0.0.1', headers={'Authorization': 'Bearer ' + os.environ['PLOTPILOT_SESSION_TOKEN']})
    with client_context as client:
        def api(method, path, payload=None):
            response = client.request(method, '/api/v1' + path, json=payload)
            if response.status_code >= 400:
                raise RuntimeError(f'{method} {path}: HTTP {response.status_code}: {response.text[:600]}')
            return response.json()
        existing = client.get('/api/v1/novels/' + NOVEL)
        if existing.status_code == 200 and (PREVIEW / 'seed-manifest.json').is_file():
            print('Seed already installed; existing edits preserved.')
            return
        if existing.status_code == 200:
            raise RuntimeError('An incomplete initial sample exists. Existing text was preserved; finish or inspect it explicitly before reseeding.')
        if existing.status_code != 200:
            api('POST', '/novels/', dict(novel_id=NOVEL, title='雾港来信·功能测试', author='叙织测试工作室', target_chapters=6,
                premise='档案修复师林澄收到一封来自停航灯塔的信。她与摆渡人周渡追查三年前的失踪船，在雾港潮汐档案中发现被人为改写的航线。六章内揭示写信者、铜钥匙与九十二号仓库的关系。',
                writing_style='第三人称近距离视角，克制、具体，以行动推动悬疑。', target_words_per_chapter=2000))
            b = f'/bible/novels/{NOVEL}/bible'
            api('POST', b, dict(bible_id=NOVEL+'-bible', novel_id=NOVEL))
            api('PUT', b, dict(characters=[dict(id='fog-lin', name='林澄', description='二十八岁的档案修复师，寻找失踪的姐姐。', relationships=[dict(target='周渡',relation='合作',description='互相信任但各有隐瞒')], core_motivation='查明姐姐失踪的真相', personality='耐心、敏锐，不轻易承诺'),dict(id='fog-zhou',name='周渡',description='熟悉暗潮的摆渡人，曾参与最后一次灯塔航行。',relationships=[])],
                locations=[dict(id='fog-port',name='雾港',description='受潮汐与常年海雾影响的旧港口。',location_type='city'),dict(id='fog-archive',name='潮汐档案馆',description='保存原始航线和航标记录的石砌档案馆。',location_type='building',parent_id='fog-port'),dict(id='fog-tower',name='白鸥灯塔',description='停航三年的离岸灯塔，夜间仍有灯光。',location_type='building',parent_id='fog-port')],
                world_settings=[dict(id='fog-tide',name='雾港潮汐规则',description='每天两次低潮，仅低潮前后半小时可安全穿过礁门。',setting_type='rule')],
                timeline_notes=[dict(id='fog-time',event='最后一次灯塔航行',time_point='三年前秋分',description='航船失联，航线原件封存。')],
                style_notes=[dict(id='fog-style',category='叙事约定',content='线索必须在场景中出现；不以梦境推翻事实。')]))
            chapters=[('潮声里的信','林澄推开潮汐档案馆的门时，雨水正沿铜门环滴落。桌上躺着一封没有邮戳的信，信封背面画着白鸥灯塔。\n\n姐姐失踪已经三年。她认得那道折痕，却不愿立即相信。林澄用镊子夹起信纸，一枚铜钥匙从夹层落下，敲在桌面上。\n\n“明天低潮，去九十二号仓库。”信上只有这一句。窗外有人敲了两下玻璃，是摆渡人周渡。他没有进门，只把一张被海水浸软的航线图按在窗上。\n\n图上的礁门被红墨划去。林澄握住铜钥匙，第一次发现钥匙柄上也刻着同样的红色记号。'),
                      ('九十二号仓库','低潮比预报早了十分钟。林澄和周渡沿堤岸走到九十二号仓库，铁门下露出一道新鲜的拖痕。\n\n铜钥匙转了半圈便停住。周渡蹲下，从门缝里拽出一根细线，门后的铃铛才没有响。他解释说，这是老水手用来防潮鼠的机关，手指却一直发抖。\n\n仓库中央只有一盏航灯。林澄擦掉玻璃上的盐霜，看见灯罩内壁刻着姐姐的名字。灯下压着三年前的原始航线，终点并非白鸥灯塔，而是灯塔背后的礁洞。\n\n“你早就知道。”她望向周渡。\n\n周渡没有辩解。他把潮汐表摊开，指向下一次低潮：“如果现在去，我们还能赶在涨潮前回来。”')]
            for number,(title,content) in enumerate(chapters,1):
                api('POST',f'/novels/{NOVEL}/chapters',dict(chapter_id=f'{NOVEL}-{number}',number=number,title=title,content=content))
            for number,title in [(3,'礁门后的灯'),(4,'被改写的航线'),(5,'最后的摆渡'),(6,'归港')]:
                api('POST',f'/novels/{NOVEL}/chapters/{number}/ensure',dict(title=title))
            facts=[dict(id='fog-fact-1',subject='林澄',predicate='是',object='主角',entity_type='character',subject_entity_id='fog-lin',source_type='manual'),dict(id='fog-fact-2',subject='周渡',predicate='是',object='人物',entity_type='character',subject_entity_id='fog-zhou',source_type='manual'),dict(id='fog-fact-3',subject='林澄',predicate='合作',object='周渡',entity_type='character',subject_entity_id='fog-lin',object_entity_id='fog-zhou',source_type='manual'),dict(id='fog-fact-4',subject='铜钥匙',predicate='开启',object='九十二号仓库',chapter_id=1,source_type='manual')]
            api('PUT',f'/novels/{NOVEL}/knowledge',dict(premise_lock='林澄与周渡循线调查三年前的航船失踪。',facts=facts,chapters=[dict(chapter_id=1,summary='林澄收到来信和铜钥匙，约定低潮调查仓库。',open_threads='谁送来了信？',sync_status='draft'),dict(chapter_id=2,summary='仓库发现姐姐姓名和原始航线，周渡隐瞒旧事。',open_threads='礁洞里有什么？',sync_status='draft')]))
        api('POST',f'/novels/{NOVEL}/manuscript/props',dict(name='铜钥匙',description='柄上刻有红色礁门记号，可开启九十二号仓库。',holder_character_id='fog-lin',first_chapter=1,aliases=['钥匙']))
        api('POST',f'/novels/{NOVEL}/foreshadow-ledger',dict(entry_id='fog-question-1',chapter=1,character_id='fog-lin',question='停航三年的灯塔为何仍在夜里亮灯？'))
        for number,title in [(1,'来信与仓库'),(2,'礁洞与旧航线'),(3,'真相与归港')]:
            act = api('POST',f'/novels/{NOVEL}/structure/nodes',dict(node_type='act',number=number,title=title,description='两章推进一个调查阶段；每阶段兑现一条线索。'))['node']
            for chapter in api('GET', f'/novels/{NOVEL}/chapters'):
                if (chapter['number'] - 1) // 2 + 1 == number:
                    api('POST',f'/novels/{NOVEL}/structure/nodes',dict(node_type='chapter',number=chapter['number'],title=chapter['title'],parent_id=act['id']))
        api('PUT', f'/novels/{NOVEL}', dict(generation_prefs={'phase_display_mode':False}))
        api('PUT',f'/novels/{NOVEL}/chapters/1/review',dict(status='reviewed',memo='人工测试样稿：请检查铜钥匙与仓库号的一致性。'))
        api('POST',f'/novels/{NOVEL}/snapshots',dict(trigger_type='MANUAL',name='功能测试初始快照',description='自编样稿、设定与线索的起始保存点。'))
        for number in (1,2):
            api('POST',f'/novels/{NOVEL}/chapters/{number}/entity-mentions/reindex')
        (PREVIEW/'seed-manifest.json').write_text(json.dumps(dict(novel_id=NOVEL,source='self-authored; real HTTP APIs; no model invocation',chapter_count=6,completed_chapters=2),ensure_ascii=False,indent=2),encoding='utf-8')
        print('Seed installed through real APIs: 2 written chapters, 4 pending chapters.')


if __name__ == '__main__':
    main()



