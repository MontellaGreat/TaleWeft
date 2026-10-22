"""Self-authored public TaleWeft fixture; no runtime data or model settings."""
from copy import deepcopy


NOVEL_ID = "taleweft-sample-fog-harbor"
NOVEL = {
    "novel_id": NOVEL_ID,
    "title": "雾港来信·功能测试",
    "author": "叙织测试工作室",
    "target_chapters": 6,
    "target_words_per_chapter": 2000,
    "premise": "档案修复师林澄收到一封来自停航灯塔的信。她与摆渡人周渡追查三年前的失踪船，在雾港潮汐档案中发现被人为改写的航线。六章内揭示写信者、铜钥匙与九十二号仓库的关系。",
    "writing_style": "第三人称近距离视角，克制、具体，以行动推动悬疑。",
}

CHAPTERS = [
    ("潮声里的信", "林澄推开潮汐档案馆的门时，雨水正沿铜门环滴落。桌上躺着一封没有邮戳的信，信封背面画着白鸥灯塔。\n\n姐姐失踪已经三年。她认得那道折痕，却不愿立即相信。林澄用镊子夹起信纸，一枚铜钥匙从夹层落下，敲在桌面上。\n\n“明天低潮，去九十二号仓库。”信上只有这一句。窗外有人敲了两下玻璃，是摆渡人周渡。他没有进门，只把一张被海水浸软的航线图按在窗上。\n\n图上的礁门被红墨划去。林澄握住铜钥匙，第一次发现钥匙柄上也刻着同样的红色记号。"),
    ("九十二号仓库", "低潮比预报早了十分钟。林澄和周渡沿堤岸走到九十二号仓库，铁门下露出一道新鲜的拖痕。\n\n铜钥匙转了半圈便停住。周渡蹲下，从门缝里拽出一根细线，门后的铃铛才没有响。他解释说，这是老水手用来防潮鼠的机关，手指却一直发抖。\n\n仓库中央只有一盏航灯。林澄擦掉玻璃上的盐霜，看见灯罩内壁刻着姐姐的名字。灯下压着三年前的原始航线，终点并非白鸥灯塔，而是灯塔背后的礁洞。\n\n“你早就知道。”她望向周渡。\n\n周渡没有辩解。他把潮汐表摊开，指向下一次低潮：“如果现在去，我们还能赶在涨潮前回来。”"),
    ("礁门后的灯", ""), ("被改写的航线", ""), ("最后的摆渡", ""), ("归港", ""),
]

# Namespace all identities so importing into a library cannot replace another
# book's global character/location/knowledge identities.
def identity(suffix):
    return NOVEL_ID + "-" + suffix


BIBLE = {
    "characters": [
        {"id": identity("lin"), "name": "林澄", "description": "二十八岁的档案修复师，寻找失踪的姐姐。", "relationships": [{"target": "周渡", "relation": "合作", "description": "互相信任但各有隐瞒"}], "core_motivation": "查明姐姐失踪的真相", "personality": "耐心、敏锐，不轻易承诺"},
        {"id": identity("zhou"), "name": "周渡", "description": "熟悉暗潮的摆渡人，曾参与最后一次灯塔航行。", "relationships": []},
    ],
    "locations": [
        {"id": identity("port"), "name": "雾港", "description": "受潮汐与常年海雾影响的旧港口。", "location_type": "city"},
        {"id": identity("archive"), "name": "潮汐档案馆", "description": "保存原始航线和航标记录的石砌档案馆。", "location_type": "building", "parent_id": identity("port")},
        {"id": identity("tower"), "name": "白鸥灯塔", "description": "停航三年的离岸灯塔，夜间仍有灯光。", "location_type": "building", "parent_id": identity("port")},
        {"id": identity("warehouse"), "name": "九十二号仓库", "description": "旧堤岸上的仓库，保存失踪航船的原始航线和航灯。", "location_type": "building", "parent_id": identity("port")},
    ],
    "world_settings": [{"id": identity("tide"), "name": "雾港潮汐规则", "description": "每天两次低潮，仅低潮前后半小时可安全穿过礁门。", "setting_type": "rule"}],
    "timeline_notes": [{"id": identity("time"), "event": "最后一次灯塔航行", "time_point": "三年前秋分", "description": "航船失联，航线原件封存。"}],
    "style_notes": [{"id": identity("style"), "category": "叙事约定", "content": "线索必须在场景中出现；不以梦境推翻事实。"}],
}

KNOWLEDGE = {
    "premise_lock": "林澄与周渡循线调查三年前的航船失踪。",
    "facts": [
        {"id": identity("fact-1"), "subject": "林澄", "predicate": "是", "object": "主角", "entity_type": "character", "subject_entity_id": identity("lin"), "source_type": "manual"},
        {"id": identity("fact-2"), "subject": "周渡", "predicate": "是", "object": "人物", "entity_type": "character", "subject_entity_id": identity("zhou"), "source_type": "manual"},
        {"id": identity("fact-3"), "subject": "林澄", "predicate": "合作", "object": "周渡", "entity_type": "character", "subject_entity_id": identity("lin"), "object_entity_id": identity("zhou"), "source_type": "manual"},
        {"id": identity("fact-4"), "subject": "铜钥匙", "predicate": "开启", "object": "九十二号仓库", "chapter_id": 1, "source_type": "manual"},
    ],
    "chapters": [
        {"chapter_id": 1, "summary": "林澄收到来信和铜钥匙，约定低潮调查仓库。", "open_threads": "谁送来了信？", "sync_status": "draft"},
        {"chapter_id": 2, "summary": "仓库发现姐姐姓名和原始航线，周渡隐瞒旧事。", "open_threads": "礁洞里有什么？", "sync_status": "draft"},
    ],
}


def public_payloads():
    """Return independent copies for HTTP requests."""
    return deepcopy(NOVEL), deepcopy(BIBLE), deepcopy(KNOWLEDGE)


def manuscript_document():
    """Use the existing portable manuscript format, whose scope is deliberately limited."""
    from types import SimpleNamespace
    from application.core.services.manuscript_transfer import make_document

    novel = SimpleNamespace(**NOVEL)
    chapters = [SimpleNamespace(number=number, title=title, content=content, outline="", status="draft")
                for number, (title, content) in enumerate(CHAPTERS, 1)]
    return make_document(novel, chapters)
