当前跟踪的道具列表（包含未登场与已损毁道具，不可创建其他道具）：
{props_summary}

完整章节正文：
{chapter_excerpt}

输出格式（JSON 数组）：
[
  {
    "prop_id": "...",
    "event_type": "INTRODUCED|USED|TRANSFERRED|DAMAGED|REPAIRED|UPGRADED|RESOLVED",
    "actor_character": "角色名（可选）",
    "from_holder": "转出方角色名（TRANSFERRED 时填）",
    "to_holder": "转入方角色名（TRANSFERRED 时填）",
    "description": "一句话描述",
    "evidence_quote": "逐字引用能定位该事件的原文短句"
  }
]

无相关事件时输出空数组 []。
严格按照事件在正文出现的先后顺序输出，包括同一道具的每次转交、损坏与修复，不可只输出第一件或最后一件事件。
仅提取正文实际发生的事件；提到道具不代表它已使用、修复或复活。转交双方填写现有角色名或角色ID。
