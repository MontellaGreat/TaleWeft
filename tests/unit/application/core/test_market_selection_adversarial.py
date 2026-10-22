"""Adversarial checks for market preference persistence and downgrade paths."""
from unittest.mock import Mock

import pytest

from application.core.services.novel_service import NovelService
from application.core.taxonomy.cn_market_v2 import TaxonomyV2ValidationError
from application.core.taxonomy.market_selection import normalize_market_preferences
from tests.unit.application.core.test_market_selection_integrity import custom_selection


@pytest.mark.parametrize('invalid', [[], False, 0, ''])
def test_empty_nonobject_custom_profile_cannot_disappear_into_legacy_defaults(invalid):
    with pytest.raises(TaxonomyV2ValidationError, match='object'):
        normalize_market_preferences({'custom_market_profile': invalid})


@pytest.mark.parametrize('invalid', [[], False, 0, ''])
def test_update_cannot_silently_downgrade_custom_only_book_using_invalid_shape(invalid):
    repository = Mock()
    service = NovelService(repository, Mock())
    service.create_novel(novel_id='book', title='书', author='作者', target_chapters=10,
                         **custom_selection(canonical=False))
    novel = repository.save.call_args.args[0]
    repository.get_by_id.return_value = novel
    before = novel.generation_prefs.to_dict()
    with pytest.raises(TaxonomyV2ValidationError, match='object'):
        service.update_novel('book', title='不应保存', generation_prefs={'custom_market_profile': invalid})
    assert novel.title == '书'
    assert novel.generation_prefs.to_dict() == before
    repository.patch.assert_not_called()


def test_create_persists_server_derived_market_path_alongside_snapshot():
    repository = Mock()
    dto = NovelService(repository, Mock()).create_novel(
        novel_id='book', title='书', author='作者', target_chapters=10, **custom_selection())
    preferences = repository.save.call_args.args[0].generation_prefs
    expected = ['male', 'major.xuanhuan', 'custom.theme.my-theme']
    assert preferences.market_category_path == expected
    assert preferences.market_profile_snapshot['market_category_path'] == expected
    assert dto.market_category_path == expected


@pytest.mark.parametrize('field,invalid', [('market_tags', 'tag'), ('market_category_path', 'male')])
def test_direct_create_service_rejects_string_in_place_of_market_list(field, invalid):
    with pytest.raises(TaxonomyV2ValidationError, match='list'):
        NovelService(Mock(), Mock()).create_novel(
            novel_id='book', title='书', author='作者', target_chapters=10,
            **custom_selection(), **{field: invalid})
