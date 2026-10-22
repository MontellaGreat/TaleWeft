from copy import deepcopy

import pytest

from application.core.taxonomy.cn_market_v2 import (
    PROFILE_DIMENSIONS,
    TaxonomyV2ValidationError,
    load_cn_market_v2_bundle,
    resolve_category_profile_for_generation,
    resolve_legacy_taxonomy_id,
    resolve_theme_profile_for_generation,
    validate_cn_market_v2_bundle,
)


def test_builtin_v2_bundle_satisfies_structure_and_profile_contracts():
    bundle = load_cn_market_v2_bundle()

    assert bundle["schema_version"] == 2
    assert {channel["id"] for channel in bundle["channels"]} == {"male", "female", "dual"}
    assert len(bundle["categories"]) >= 20
    assert len(bundle["themes"]) == 104
    assert "theme.reality.period" in {theme["id"] for theme in bundle["themes"]}
    assert len({category["id"] for category in bundle["categories"]}) == len(bundle["categories"])
    assert len({theme["id"] for theme in bundle["themes"]}) == len(bundle["themes"])

    categories = {item["id"]: item for item in bundle["categories"]}
    for theme in bundle["themes"]:
        assert theme["id"].startswith("theme.")
        assert theme["category_id"] in categories
        assert theme["id"] in categories[theme["category_id"]]["theme_ids"]
        assert set(theme["profile"]) == set(PROFILE_DIMENSIONS)
        assert all(theme["profile"][dimension].strip() for dimension in PROFILE_DIMENSIONS)
        assert theme["profile_ref"]
        assert theme["profile_source"]["file"]
        assert set(theme["audience_profile_overlays"]) == set(theme["audience_modes"])


def test_channel_categories_are_explicit_deduplicated_and_limited_to_featured_twelve():
    bundle = load_cn_market_v2_bundle()
    categories = {item["id"]: item for item in bundle["categories"]}
    channels = {item["id"]: item for item in bundle["channels"]}

    for channel_id, channel in channels.items():
        assert len(channel["category_ids"]) == len(set(channel["category_ids"]))
        assert len(channel["featured_category_ids"]) <= 12
        assert set(channel["featured_category_ids"]).issubset(channel["category_ids"])
        assert all(channel_id in categories[item]["audience_modes"] for item in channel["category_ids"])

    assert len(channels["dual"]["category_ids"]) == len(
        set(channels["male"]["category_ids"] + channels["female"]["category_ids"])
    )


def test_legacy_ids_migrate_to_one_canonical_category_and_theme():
    assert resolve_legacy_taxonomy_id("xuanhuan") == "major.xuanhuan"
    assert resolve_legacy_taxonomy_id("xuanhuan.dongfang") == "theme.xuanhuan.dongfang"
    assert resolve_legacy_taxonomy_id("missing.legacy.id") is None


def test_profiles_from_drafts_are_complete_but_not_claimed_as_reviewed():
    bundle = load_cn_market_v2_bundle()
    draft_themes = [item for item in bundle["themes"] if item["status"] == "draft_profile_complete"]

    assert len(draft_themes) == 19
    assert all(item["profile_source"]["type"] == "profile_draft" for item in draft_themes)
    assert all(
        overlay["status"] == "pending_review"
        for item in draft_themes
        for overlay in item["audience_profile_overlays"].values()
    )


def test_platform_mappings_preserve_observation_evidence_and_only_reference_known_ids():
    bundle = load_cn_market_v2_bundle()
    platforms = {item["id"]: item for item in bundle["platforms"]}
    assert {"qidian_mobile", "zongheng_mobile", "zongheng_store", "17k_all", "fanqie", "jjwxc", "qimao"}.issubset(platforms)

    for platform in platforms.values():
        for mapping in platform["mappings"]:
            assert mapping["source_category_label"]
            assert mapping["source_url"].startswith("https://")
            assert mapping["confidence"]
            if mapping["status"] in {"mapped_category", "mapped_theme", "mapped_alias", "mapped_legacy_alias"}:
                assert mapping["observed_at"]
                assert mapping["canonical_category_id"] is not None


def test_validator_rejects_duplicate_ids_parent_mismatch_incomplete_profile_and_missing_source():
    bundle = load_cn_market_v2_bundle()
    duplicate = deepcopy(bundle)
    duplicate["themes"].append(deepcopy(duplicate["themes"][0]))
    with pytest.raises(TaxonomyV2ValidationError, match="duplicate theme id"):
        validate_cn_market_v2_bundle(duplicate)

    parent_mismatch = deepcopy(bundle)
    theme_mapping = next(item for item in parent_mismatch["platforms"][0]["mappings"] if item["canonical_theme_id"])
    theme_mapping["canonical_category_id"] = "major.short"
    with pytest.raises(TaxonomyV2ValidationError, match="category/theme parents do not match"):
        validate_cn_market_v2_bundle(parent_mismatch)

    incomplete = deepcopy(bundle)
    incomplete["themes"][0]["profile"]["pacing_control"] = "  "
    with pytest.raises(TaxonomyV2ValidationError, match="pacing_control is incomplete"):
        validate_cn_market_v2_bundle(incomplete)

    missing_source = deepcopy(bundle)
    missing_source["platforms"][0]["mappings"][0]["source_url"] = ""
    with pytest.raises(TaxonomyV2ValidationError, match="source_url must be non-empty"):
        validate_cn_market_v2_bundle(missing_source)


def test_custom_entries_are_project_scoped_and_cannot_change_builtin_registry():
    bundle = load_cn_market_v2_bundle()
    custom = bundle["custom_entry"]

    assert custom["scope"] == "project"
    assert custom["category"]["enabled"] is True
    assert custom["theme"]["enabled"] is True
    assert custom["theme"]["parent_category_required"] is True
    assert custom["draft_status"] == "custom_profile_draft"
    assert custom["generation_requires_complete_profile"] is True
    assert custom["registry_mutation_allowed"] is False


def test_high_risk_theme_profiles_do_not_inherit_unrelated_genre_defaults():
    bundle = load_cn_market_v2_bundle()
    themes = {theme["id"]: theme["profile"] for theme in bundle["themes"]}

    brain = themes["theme.xuanhuan.naodong"]
    brain_text = "\n".join(brain.values())
    assert "除非作者明确选择修炼体系，否则不得自动补入境界、宗门或战力等级" in brain_text
    assert "境界不能只换名字" not in brain_text
    assert "围绕师门、宗族、宿敌、上位者压迫和同辈竞争" not in brain_text

    school = themes["theme.dushi.school"]
    school_text = "\n".join(school.values())
    assert "校园成长、同龄关系和青春选择" in school["special_requirements"]
    assert "年龄、校园制度、学业安排和竞赛规则" in school["special_requirements"]
    assert not any(term in school_text for term in ("客户", "权贵", "职业流程", "都市全品类赛道"))

    thriller = themes["theme.xuanyi.jingsong"]
    thriller_text = "\n".join(thriller.values())
    assert "不能套用刑侦破案模板" in thriller["special_requirements"]
    assert "不强制套用法医、程序或证据链" in thriller["special_requirements"]
    assert "侦查者、嫌疑人、受害者" not in thriller_text

    historical_fanfic = themes["theme.tongren.lishi_f"]
    fanfic_text = "\n".join(historical_fanfic.values())
    assert "不默认安排亲密配对" in fanfic_text
    assert "不默认假定存在原作主角团或亲密配对" in fanfic_text
    assert "围绕原作主角团、反派、CP关系" not in fanfic_text

    athletics = themes["theme.tiyu.tianjing"]
    athletics_text = "\n".join(athletics.values())
    assert "不默认从校园跳到国家队" in athletics["world_tone"]
    assert "不得把伤病反噬或误导当作默认桥段" in athletics_text
    assert "奥运舞台" not in athletics_text

    retirement = themes["theme.tianyuan_zhong.yanglao"]
    retirement_text = "\n".join(retirement.values())
    assert "不强行制造战斗式反转" in retirement["pacing_control"]
    assert "不以扩张经营或强冲突替代养老主题本身" in retirement["special_requirements"]
    assert "小爽点：" not in retirement_text
    assert "一次破局、反噬、误导或情绪兑现" not in retirement_text


def test_sports_and_rural_profiles_do_not_use_generic_backlash_misdirection_tail():
    bundle = load_cn_market_v2_bundle()
    reviewed_prefixes = ("theme.tiyu.", "theme.tianyuan_zhong.")
    generic_tails = ("都要在后续转化为一次破局、反噬、误导或情绪兑现",)

    reviewed = [
        theme for theme in bundle["themes"] if theme["id"].startswith(reviewed_prefixes)
    ]
    assert reviewed
    for theme in reviewed:
        profile_text = "\n".join(theme["profile"].values())
        assert not any(fragment in profile_text for fragment in generic_tails), theme["id"]


def test_theme_and_custom_theme_defaults_do_not_restore_global_escalation_templates():
    bundle = load_cn_market_v2_bundle()
    profiles = [(item["id"], item["profile"]) for item in bundle["themes"]]
    profiles += [
        (item["id"], item["default_profile"])
        for item in bundle["categories"] if item.get("default_profile")
    ]
    unsafe_templates = (
        "破局、反噬、误导或情绪兑现",
        "回报立刻可见 → 新代价抬高",
        "压力落到具体场景 → 行动破局 → 收益可见 → 更大问题露头",
        "至少保留一个能长期施压、也能被阶段性反超的对照人物",
        "每一轮都要扩大资源、关系、规则认知或行动代价",
        "男频核心仙侠主赛道",
        "男频核心爽文主赛道",
        "女频言情 / 甜宠赛道（宽泛兼容）",
        "CP关系",
    )
    for entry_id, profile in profiles:
        text = "\n".join(profile.values())
        assert not any(fragment in text for fragment in unsafe_templates), entry_id
        assert "小爽点：" not in text or entry_id == "theme.tianyuan_zhong.yanglao", entry_id


def test_custom_theme_defaults_keep_world_and_relationship_choices_open():
    categories = {item["id"]: item for item in load_cn_market_v2_bundle()["categories"]}
    defaults = {entry_id: item["default_profile"] for entry_id, item in categories.items() if item.get("default_profile")}
    assert len(defaults) == 18
    for entry_id in ("major.reality", "major.infinite", "major.short"):
        assert not categories[entry_id].get("default_profile")

    fantasy_world = defaults["major.qihuan"]["world_tone"]
    assert all(branch in fantasy_world for branch in ("东方", "现代", "蒸汽", "非人文明"))
    assert "不预设西式王权教会" in fantasy_world
    game_world = defaults["major.youxi"]["world_tone"]
    assert all(play in game_world for play in ("经营", "解谜", "游戏异界"))
    assert "不默认" in game_world
    immortal_rules = defaults["major.xianxia"]["special_requirements"]
    assert "频道不预设性别和恋爱" in immortal_rules
    assert "道侣仅在明确关系主线时启用" in immortal_rules
    for entry_id in ("major.tongren", "major.romance"):
        assert "未选" in defaults[entry_id]["special_requirements"] or "未明确恋爱" in defaults[entry_id]["special_requirements"]
        assert "配对" in defaults[entry_id]["special_requirements"]
    assert all("无CP" in defaults["major.romance"][key] for key in ("world_tone", "story_structure", "writing_style"))


def test_game_and_reincarnation_worlds_support_noncombat_non_rpg_choices():
    themes = {item["id"]: item["profile"] for item in load_cn_market_v2_bundle()["themes"]}
    otherworld = themes["theme.youxi.yijie"]
    assert all(play in otherworld["world_tone"] for play in ("经营", "解谜", "服务"))
    assert "仅在明确选用战斗玩法时启用" in otherworld["world_tone"]
    assert "真实伤害、死亡、物品和人物自主性" in otherworld["special_requirements"]
    reincarnation = themes["theme.qingxiaoshuo.zhuansheng"]
    assert "技艺" in reincarnation["world_tone"]
    assert "仅在所选世界需要时启用" in reincarnation["world_tone"]
    assert "前世技能不自动复制身体条件" in reincarnation["special_requirements"]
    for entry_id in ("theme.youxi.yijie", "theme.youxi.xitong", "theme.youxi.moniq"):
        assert "围绕战队、对手、教练、俱乐部" not in "\n".join(themes[entry_id].values())


def test_romance_none_choices_apply_to_structure_pacing_and_voice():
    themes = {item["id"]: item["profile"] for item in load_cn_market_v2_bundle()["themes"]}
    for entry_id in ("theme.romance.modern", "theme.romance.ancient", "theme.romance.fantasy", "theme.romance.youth", "theme.short.romance"):
        profile = themes[entry_id]
        for key in ("story_structure", "special_requirements"):
            text = profile[key].replace(" ", "")
            # A final opt-out line alone is insufficient when earlier dimensions force romance.
            assert "无CP" in text, (entry_id, key)
        assert any(branch in profile["story_structure"] for branch in ("个人", "家庭", "友情", "合作", "伙伴", "独立目标")), entry_id
        assert not any(template in profile["pacing_control"] for template in ("甜虐交替", "每 3—5 章", "每800", "必须出现一次不可逆")), entry_id
        assert "无CP" in profile["writing_style"].replace(" ", "") or "否则" in profile["writing_style"], entry_id
    assert "无CP" in themes["theme.romance.fantasy"]["world_tone"].replace(" ", "")
    assert "纯甜" in themes["theme.romance.modern"]["pacing_control"]
    assert "零反转" in themes["theme.short.romance"]["pacing_control"]
    assert "800" not in "\n".join(themes["theme.short.romance"].values())


def test_time_weight_and_period_profiles_keep_domain_specific_boundaries():
    themes = {item["id"]: item["profile"] for item in load_cn_market_v2_bundle()["themes"]}
    time_rules = themes["theme.kehuan.shikong"]["special_requirements"]
    assert all(model in time_rules for model in ("单线", "分支", "闭环"))
    assert all(flow in time_rules for flow in ("信息", "物品", "年龄", "记忆", "重置", "流向"))
    assert "量级是资格条件" in themes["theme.tiyu.gedou"]["special_requirements"]
    assert "不是必须逐级上升" in themes["theme.tiyu.gedou"]["special_requirements"]
    period = themes["theme.reality.period"]
    assert "预设主角性别" in period["world_tone"]
    assert "2—4" not in period["pacing_control"]
    assert "篇幅依总长度" in period["pacing_control"]
    assert "不要求每次" in themes["theme.scifi.near_future"]["pacing_control"]
    assert "稳定适应" in themes["theme.scifi.evolution"]["story_structure"]


def test_semantic_revisions_do_not_promote_pending_profiles_to_generation_ready():
    bundle = load_cn_market_v2_bundle()
    assert all(item["status"] != "ready" for item in bundle["themes"])
    assert all(item.get("default_profile_status") != "ready" for item in bundle["categories"])
    assert all(
        overlay["status"] == "pending_review"
        for item in bundle["themes"] for overlay in item["audience_profile_overlays"].values()
    )
    with pytest.raises(TaxonomyV2ValidationError, match="not ready"):
        resolve_theme_profile_for_generation(bundle, "theme.romance.fantasy", "female")
    with pytest.raises(TaxonomyV2ValidationError, match="not ready"):
        resolve_category_profile_for_generation(bundle, "major.xianxia")
