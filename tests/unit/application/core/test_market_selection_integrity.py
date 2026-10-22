from copy import deepcopy
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from application.core.services.novel_service import NovelService
from application.core.taxonomy.cn_market_v2 import PROFILE_DIMENSIONS, TaxonomyV2ValidationError
from application.core.taxonomy.market_selection import (
    normalize_market_preferences, validate_market_preferences_for_generation,
)
from domain.novel.value_objects.generation_preferences import GenerationPreferences
from interfaces.api.v1.core.novels import CreateNovelRequest


def custom_selection(*, canonical=True, source=False):
    category_id = 'major.xuanhuan' if canonical else ''
    theme_id = 'theme.xuanhuan.dongfang' if source else ''
    profile = {dimension: f'作者确认的{dimension}文本' for dimension in PROFILE_DIMENSIONS}
    custom = {
        'scope': 'project', 'audience_mode': 'male', 'profile_review_status': 'user_confirmed',
        'custom_theme': {
            'id': 'custom.theme.my-theme', 'label': '自定义主题', 'profile': profile,
            'status': 'user_confirmed',
            ('parent_canonical_category_id' if canonical else 'parent_custom_category_id'):
                category_id if canonical else 'custom.category.my-category',
        },
    }
    if not canonical:
        custom['custom_category'] = {'id': 'custom.category.my-category', 'label': '自定义大类'}
    if source:
        custom['source_canonical_theme_id'] = theme_id
        custom['custom_theme']['source_canonical_theme_id'] = theme_id
    return {'audience_mode': 'male', 'canonical_genre_id': category_id,
            'canonical_theme_id': theme_id, 'custom_market_profile': custom}


@pytest.mark.parametrize('canonical,source', [(True, False), (True, True), (False, False)])
def test_custom_and_confirmed_builtin_override_can_create_and_generate(canonical, source):
    selection = custom_selection(canonical=canonical, source=source)
    request = CreateNovelRequest(novel_id='book', title='书', author='作者', **selection)
    repo = Mock()
    dto = NovelService(repo, Mock()).create_novel(
        novel_id=request.novel_id, title=request.title, author=request.author,
        target_chapters=10, **selection,
        market_profile_snapshot={'profile': {'world_tone': '伪造'}}, profile_snapshot_hash='spoofed',
    )
    preferences = repo.save.call_args.args[0].generation_prefs
    snapshot = validate_market_preferences_for_generation(preferences)
    assert snapshot['profile_status'] == 'user_confirmed'
    assert snapshot['custom_market_profile'] == selection['custom_market_profile']
    assert dto.profile_snapshot_hash != 'spoofed'
    assert dto.locked_world_preset == selection['custom_market_profile']['custom_theme']['profile']['world_tone']


@pytest.mark.parametrize('dimension', PROFILE_DIMENSIONS)
def test_custom_incomplete_dimensions_are_rejected_at_api_and_service(dimension):
    selection = custom_selection()
    selection['custom_market_profile']['custom_theme']['profile'][dimension] = ' '
    with pytest.raises(ValidationError, match='incomplete'):
        CreateNovelRequest(novel_id='book', title='书', author='作者', **selection)
    with pytest.raises(TaxonomyV2ValidationError, match='incomplete'):
        normalize_market_preferences(selection)


@pytest.mark.parametrize('change,error', [
    ({'canonical_genre_id': 'major.missing'}, 'unknown canonical_genre_id'),
    ({'canonical_theme_id': 'theme.missing'}, 'unknown canonical_theme_id'),
    ({'canonical_genre_id': 'major.xianxia'}, 'parents do not match'),
    ({'audience_mode': 'unspecified'}, 'audience_mode'),
    ({'taxonomy_version': 'stale'}, 'registry version'),
])
def test_builtin_selection_cannot_forge_stable_registry_references(change, error):
    selection = {'audience_mode': 'male', 'canonical_genre_id': 'major.xuanhuan',
                 'canonical_theme_id': 'theme.xuanhuan.dongfang', **change}
    with pytest.raises(TaxonomyV2ValidationError, match=error):
        normalize_market_preferences(selection)


def test_builtin_draft_cannot_generate_without_confirmed_project_override():
    saved = normalize_market_preferences({'audience_mode': 'male', 'canonical_genre_id': 'major.xuanhuan',
                                          'canonical_theme_id': 'theme.xuanhuan.dongfang'})
    with pytest.raises(TaxonomyV2ValidationError, match='not ready'):
        validate_market_preferences_for_generation(saved)


@pytest.mark.parametrize('field', ['profile_review_status', 'custom_theme.status'])
def test_completeness_does_not_approve_custom_profile(field):
    selection = custom_selection()
    custom = selection['custom_market_profile']
    if field == 'profile_review_status':
        custom[field] = 'draft'
    else:
        custom['custom_theme']['status'] = 'ready'
    saved = normalize_market_preferences(selection)
    with pytest.raises(TaxonomyV2ValidationError, match='confirmation'):
        validate_market_preferences_for_generation(saved)


def test_custom_parent_audience_and_source_must_match_selected_ids():
    selection = custom_selection(source=True)
    for mutate in ('parent', 'audience', 'source'):
        invalid = deepcopy(selection)
        custom = invalid['custom_market_profile']
        if mutate == 'parent':
            custom['custom_theme']['parent_canonical_category_id'] = 'major.xianxia'
        elif mutate == 'audience':
            custom['audience_mode'] = 'female'
        else:
            custom['custom_theme']['source_canonical_theme_id'] = 'theme.xianxia.fanren'
        with pytest.raises(TaxonomyV2ValidationError):
            normalize_market_preferences(invalid)


def test_snapshot_and_final_profile_tampering_are_blocked_at_runtime():
    original = normalize_market_preferences(custom_selection())
    for key, value in [('profile_snapshot_hash', 'spoofed'), ('market_profile_snapshot', {}),
                       ('locked_pacing_control', '替换后的节奏')]:
        tampered = deepcopy(original)
        tampered[key] = value
        with pytest.raises(TaxonomyV2ValidationError):
            validate_market_preferences_for_generation(tampered)


def test_update_preferences_revalidates_selection_and_recomputes_server_snapshot():
    repository = Mock()
    service = NovelService(repository, Mock())
    service.create_novel(novel_id='book', title='书', author='作者', target_chapters=10, **custom_selection())
    novel = repository.save.call_args.args[0]
    repository.get_by_id.return_value = novel
    previous_hash = novel.generation_prefs.profile_snapshot_hash
    service.update_novel('book', generation_prefs={'reader_contract': '每卷兑现核心悬念', 'profile_snapshot_hash': 'spoofed'})
    assert novel.generation_prefs.profile_snapshot_hash not in (previous_hash, 'spoofed')
    with pytest.raises(TaxonomyV2ValidationError, match='confirmation'):
        validate_market_preferences_for_generation(novel.generation_prefs)
    service.update_novel('book', generation_prefs={'custom_market_profile': custom_selection()['custom_market_profile']})
    validate_market_preferences_for_generation(novel.generation_prefs)
    with pytest.raises(TaxonomyV2ValidationError):
        service.update_novel('book', title='不该被修改', generation_prefs={'canonical_genre_id': 'major.missing'})
    assert novel.title == '书'


@pytest.mark.parametrize('field,value', [('market_category_path', ['male', 'wrong', 'path']),
                                         ('market_category_path', 'not-a-list'), ('market_tags', [1])])
def test_selection_cannot_forge_category_path_or_list_types(field, value):
    with pytest.raises(TaxonomyV2ValidationError):
        normalize_market_preferences({**custom_selection(), field: value})


def test_legacy_preferences_remain_compatible():
    snapshot = validate_market_preferences_for_generation(GenerationPreferences(locked_genre='旧版题材'))
    assert snapshot['profile_status'] == 'legacy_unreviewed'
