"""Shared author-requested paragraph punctuation instructions for model prompts."""

PARAGRAPH_PERIOD_RULE = (
    '【段落句号约束】以实际换行划分自然段，每个自然段最多出现一个中文句号“。”，'
    '需要句号时仅在段末收束，闭引号或闭括号前的段末句号仍计入本段；'
    '段内的动作、说明与因果分句按语义用逗号、分号或必要的连接词衔接，'
    '保留原有事实、限制条件、否定、因果及对白信息；'
    '问号、叹号结尾的段落以及标题、名称无需强补句号，'
    '不得机械替换标点、删掉信息或把同一语义单元拆成碎片段落来凑数。'
)

PARAGRAPH_PERIOD_AUDIT_RULE = (
    PARAGRAPH_PERIOD_RULE + '\n'
    '该约束适用于故事正文、设定资料和文风规范自身的自然段，'
    '按各文本字段分别检查，不把 JSON 结构、字段之间的分隔或界面自动折行当作自然段；'
    '同段出现多个中文句号时，归入 mechanical_structure，必须判为有问题，'
    '逐字引用能覆盖多个句号的原文证据，并提出保留语义的具体衔接建议，不能只给通过结论。'
)
