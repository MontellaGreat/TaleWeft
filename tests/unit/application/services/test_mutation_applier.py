"""Mutation Applier 单元测试"""
import pytest
from unittest.mock import Mock, MagicMock
from application.services.mutation_applier import MutationApplier
from domain.novel.repositories.narrative_event_repository import NarrativeEventRepository


@pytest.fixture
def mock_event_repository():
    """Mock 事件仓储"""
    return Mock(spec=NarrativeEventRepository)


@pytest.fixture
def mutation_applier(mock_event_repository):
    """创建 MutationApplier 实例"""
    return MutationApplier(mock_event_repository)


@pytest.fixture
def sample_event():
    """示例事件"""
    return {
        "event_id": "event_001",
        "novel_id": "novel_001",
        "chapter": 1,
        "event_summary": "原始摘要",
        "tags": ["情感:同情"],
        "entities": []
    }


def test_apply_add_tag(mutation_applier, mock_event_repository, sample_event):
    """测试添加标签"""
    # Arrange
    mock_event_repository.get_event.return_value = sample_event
    mock_event_repository.update_event.return_value = None

    mutations = [{"type": "add_tag", "tag": "性格:冷酷"}]

    # Act
    result = mutation_applier.apply_mutations("novel_001", "event_001", mutations)

    # Assert
    assert result["success"] is True
    assert "情感:同情" in result["updated_event"]["tags"]
    assert "性格:冷酷" in result["updated_event"]["tags"]
    assert len(result["updated_event"]["tags"]) == 2


def test_apply_remove_tag(mutation_applier, mock_event_repository, sample_event):
    """测试移除标签"""
    # Arrange
    sample_event["tags"] = ["情感:同情", "性格:冷酷"]
    mock_event_repository.get_event.return_value = sample_event
    mock_event_repository.update_event.return_value = None

    mutations = [{"type": "remove_tag", "tag": "情感:同情"}]

    # Act
    result = mutation_applier.apply_mutations("novel_001", "event_001", mutations)

    # Assert
    assert result["success"] is True
    assert "情感:同情" not in result["updated_event"]["tags"]
    assert "性格:冷酷" in result["updated_event"]["tags"]
    assert len(result["updated_event"]["tags"]) == 1


def test_apply_replace_summary(mutation_applier, mock_event_repository, sample_event):
    """测试替换摘要"""
    # Arrange
    mock_event_repository.get_event.return_value = sample_event
    mock_event_repository.update_event.return_value = None

    mutations = [{"type": "replace_summary", "new_summary": "新摘要"}]

    # Act
    result = mutation_applier.apply_mutations("novel_001", "event_001", mutations)

    # Assert
    assert result["success"] is True
    assert result["updated_event"]["event_summary"] == "新摘要"


def test_apply_multiple_mutations(mutation_applier, mock_event_repository, sample_event):
    """测试应用多个 mutations"""
    # Arrange
    mock_event_repository.get_event.return_value = sample_event
    mock_event_repository.update_event.return_value = None

    mutations = [
        {"type": "add_tag", "tag": "性格:冷酷"},
        {"type": "replace_summary", "new_summary": "新摘要"},
        {"type": "add_tag", "tag": "动机:复仇"}
    ]

    # Act
    result = mutation_applier.apply_mutations("novel_001", "event_001", mutations)

    # Assert
    assert result["success"] is True
    assert result["updated_event"]["event_summary"] == "新摘要"
    assert "情感:同情" in result["updated_event"]["tags"]
    assert "性格:冷酷" in result["updated_event"]["tags"]
    assert "动机:复仇" in result["updated_event"]["tags"]
    assert len(result["applied_mutations"]) == 3


def test_apply_invalid_mutation_type(mutation_applier, mock_event_repository, sample_event):
    """测试无效的 mutation type"""
    # Arrange
    mock_event_repository.get_event.return_value = sample_event
    mock_event_repository.update_event.return_value = None

    mutations = [
        {"type": "add_tag", "tag": "性格:冷酷"},
        {"type": "invalid_type", "data": "some_data"},  # 无效类型
        {"type": "replace_summary", "new_summary": "新摘要"}
    ]

    # Act
    with pytest.raises(ValueError, match='整批变更未应用'):
        mutation_applier.apply_mutations("novel_001", "event_001", mutations)
    mock_event_repository.update_event.assert_not_called()
    assert sample_event['tags'] == ['情感:同情'] and sample_event['event_summary'] == '原始摘要'


def test_add_tag_deduplication(mutation_applier, mock_event_repository, sample_event):
    """测试添加标签时自动去重"""
    # Arrange
    sample_event["tags"] = ["情感:同情"]
    mock_event_repository.get_event.return_value = sample_event
    mock_event_repository.update_event.return_value = None

    mutations = [
        {"type": "add_tag", "tag": "情感:同情"},  # 重复标签
        {"type": "add_tag", "tag": "性格:冷酷"}
    ]

    # Act
    result = mutation_applier.apply_mutations("novel_001", "event_001", mutations)

    # Assert
    assert result["success"] is True
    assert result["updated_event"]["tags"].count("情感:同情") == 1  # 不重复
    assert "性格:冷酷" in result["updated_event"]["tags"]


def test_remove_nonexistent_tag(mutation_applier, mock_event_repository, sample_event):
    """测试移除不存在的标签（幂等性）"""
    # Arrange
    mock_event_repository.get_event.return_value = sample_event
    mock_event_repository.update_event.return_value = None

    mutations = [{"type": "remove_tag", "tag": "不存在的标签"}]

    # Act
    result = mutation_applier.apply_mutations("novel_001", "event_001", mutations)

    # Assert
    assert result["success"] is True
    assert result["updated_event"]["tags"] == ["情感:同情"]  # 保持不变


def test_apply_mutations_with_reason(mutation_applier, mock_event_repository, sample_event):
    """测试带原因的 mutation 应用"""
    # Arrange
    mock_event_repository.get_event.return_value = sample_event
    mock_event_repository.update_event.return_value = None

    mutations = [{"type": "add_tag", "tag": "性格:冷酷"}]
    reason = "修正人设冲突"

    # Act
    result = mutation_applier.apply_mutations("novel_001", "event_001", mutations, reason)

    # Assert
    assert result["success"] is True
    # reason 应该被记录（未来可扩展到 audit_log）


def test_replace_tag_from_real_prompt_is_applied_and_idempotent(mutation_applier, mock_event_repository, sample_event):
    mock_event_repository.get_event.return_value = sample_event
    mutation = {'type': 'replace_tag', 'old': '情感:同情', 'new': '动机:理性'}
    result = mutation_applier.apply_mutations('novel_001', 'event_001', [mutation])
    assert result['updated_event']['tags'] == ['动机:理性'] and result['applied_mutations'] == [mutation]
    mock_event_repository.get_event.return_value = result['updated_event']
    assert mutation_applier.apply_mutations('novel_001', 'event_001', [mutation])['updated_event']['tags'] == ['动机:理性']


def test_missing_replace_target_rolls_back_the_entire_requested_batch(mutation_applier, mock_event_repository, sample_event):
    mock_event_repository.get_event.return_value = sample_event
    with pytest.raises(ValueError, match='待替换标签不存在'):
        mutation_applier.apply_mutations('novel_001', 'event_001', [{'type': 'add_tag', 'tag': 'added'}, {'type': 'replace_tag', 'old': 'missing', 'new': 'new'}])
    mock_event_repository.update_event.assert_not_called()
    assert sample_event['tags'] == ['情感:同情']


@pytest.mark.parametrize('mutation', [{'type': 'add_tag', 'tag': ''}, {'type': 'replace_summary'}, {'type': 'replace_tag', 'old': 'old', 'new': 7}])
def test_invalid_mutation_fields_cannot_be_skipped_as_success(mutation_applier, mock_event_repository, mutation):
    with pytest.raises(ValueError): mutation_applier.apply_mutations('novel_001', 'event_001', [mutation])
    mock_event_repository.update_event.assert_not_called()
