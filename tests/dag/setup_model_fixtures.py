"""Offline model responses for exercising durable semantic-review callers."""


def approved_semantic_review(request):
    return {'review_version': request['review_version'], 'passed': True, 'input_hash': request['input_hash'],
            'checks': {key: True for key in ('vague_lyricism', 'expository_voice', 'mechanical_structure',
                                            'emotion_labeling', 'template_closure')},
            'coverage': [{'text_id': row['text_id']} if 'text_id' in row
                         else {'path': row['path'], 'input_hash': row['input_hash']} for row in request['texts']],
            'issues': []}
