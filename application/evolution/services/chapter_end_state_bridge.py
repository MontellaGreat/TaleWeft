"""Validate the existing chapter extraction bundle and adapt final facts to actions."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from types import SimpleNamespace
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from domain.evolution.models import EvolutionAction


class _Evidence(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    evidence: str = Field(max_length=1200)
    time_scope: Literal['chapter_end', 'historical']
    certainty: Literal['explicit', 'uncertain']


class _CharacterEnd(_Evidence):
    character_name: str = Field(min_length=1, max_length=120)
    status: Literal['alive', 'dead', 'missing', 'ambiguous', 'severely_injured', 'unknown'] | None = None
    location: str | None = Field(default=None, max_length=200)


class _ItemEnd(_Evidence):
    item_name: str = Field(min_length=1, max_length=120)
    holder_name: str | None = Field(default=None, max_length=120)
    holder_state: Literal['held', 'unowned', 'unknown']
    ownership_basis: Literal['current_possession', 'current_transfer', 'historical_transfer', 'uncertain']
    previous_holder_name: str | None = Field(default=None, max_length=120)
    previous_holder_evidence: str | None = Field(default=None, max_length=1200)


class ChapterEndState(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    characters: list[_CharacterEnd] = Field(default_factory=list, max_length=8)
    items: list[_ItemEnd] = Field(default_factory=list, max_length=8)


# General temporal/modality exclusions, independent of any story or entity name.
# Ambiguous quotes are omitted rather than turned into ownership assertions.
_NONCURRENT = re.compile(r'曾经|曾[把将向给交持拥]|当年|从前|以前|早先|一度|回忆|想起|打算|准备|将要|假如|如果|'
                         r'\b(?:used to|formerly|previously|once gave|would give|plans? to|if)\b', re.I)


def build_chapter_end_actions(novel_id, chapter_number, content, raw, resolver):
    state = ChapterEndState.model_validate(raw)
    actions, ignored, seen = [], [], {}

    def quote(text, names, before=None):
        text = text.strip()
        position = content.rfind(text, 0, before) if before is not None else content.rfind(text)
        if not text or position < 0:
            raise ValueError('chapter-end evidence must be an exact manuscript quote')
        if not all(name and name in text for name in names):
            raise ValueError('chapter-end evidence must name the asserted entities')
        if _NONCURRENT.search(text):
            return None
        return {'type': 'chapter_end_evidence', 'novel_id': novel_id,
                'chapter_number': chapter_number, 'quote': text,
                'start': position, 'end': position + len(text)}

    def resolve(name, kind):
        if not name:
            return None
        # The canonical resolver ranks matches. A tied name/alias is not enough
        # evidence to select one of two distinct aggregate IDs.
        query = name.strip().casefold()
        repo = resolver.character_repo if kind == 'character' else resolver.prop_repo
        matches = set()
        for candidate in repo.list_by_novel(novel_id) if repo else []:
            candidate_id = candidate.id.value
            labels = [candidate_id, candidate.name]
            if kind == 'prop':
                labels.extend(getattr(candidate, 'aliases', []) or [])
            if query in {str(label).strip().casefold() for label in labels}:
                matches.add(candidate_id)
        if len(matches) > 1:
            raise ValueError('ambiguous chapter-end entity name')
        if not matches:
            return None
        result = resolver.resolve(novel_id, name.strip(), allowed_types=[kind])
        return result

    def entity(name, kind):
        result = resolve(name, kind)
        if result:
            return result.id
        ignored.append({'entity_type': kind, 'name': name, 'reason': 'unresolved_entity'})
        return None

    def add(kind, payload, ref):
        key = (kind, payload.get('character_id') or payload.get('item_id'))
        if key in seen and seen[key] != payload:
            raise ValueError('conflicting final state assertions for the same entity')
        if key in seen:
            return
        seen[key] = payload
        digest = hashlib.sha256(json.dumps([novel_id, chapter_number, kind, payload, ref],
            ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()
        actions.append(EvolutionAction(digest, kind, payload, 0.9, [ref]).to_dict())

    for char in state.characters:
        if char.time_scope != 'chapter_end' or char.certainty != 'explicit':
            continue
        if char.status in (None, 'unknown') and not (char.location or '').strip():
            continue
        ref = quote(char.evidence, [char.character_name])
        if ref is None:
            ignored.append({'entity_type': 'character', 'name': char.character_name, 'reason': 'noncurrent_evidence'})
            continue
        char_id = entity(char.character_name, 'character')
        if not char_id:
            continue
        ref = {**ref, 'character_id': char_id, 'character_name': char.character_name}
        if (char.location or '').strip():
            if char.location.strip() not in ref['quote']:
                raise ValueError('character location lacks quoted evidence')
            add('MOVE_CHARACTER', {'character_id': char_id, 'to_location': char.location.strip()}, ref)
        if char.status not in (None, 'unknown'):
            add('SET_CHARACTER_STATUS', {'character_id': char_id, 'status': char.status}, ref)

    for item in state.items:
        if (item.time_scope != 'chapter_end' or item.certainty != 'explicit'
                or item.holder_state == 'unknown' or item.ownership_basis in ('historical_transfer', 'uncertain')):
            continue
        if item.holder_state == 'held' and not (item.holder_name or '').strip():
            raise ValueError('a held item requires a named final holder')
        if item.holder_state == 'unowned' and item.holder_name:
            raise ValueError('an unowned item cannot have a holder')
        ref = quote(item.evidence, [item.item_name] + ([item.holder_name] if item.holder_state == 'held' else []))
        if ref is None:
            ignored.append({'entity_type': 'prop', 'name': item.item_name, 'reason': 'noncurrent_evidence'})
            continue
        holder_id = entity(item.holder_name, 'character') if item.holder_state == 'held' else None
        if item.holder_state == 'held' and not holder_id:
            continue
        matched = resolve(item.item_name, 'prop')
        # A text-named new key item gets only a stable evolution projection ID;
        # this does not create or alter an author's prop aggregate.
        item_id = matched.id if matched else 'item-' + uuid.uuid5(uuid.NAMESPACE_URL,
            f'{novel_id}:item:{item.item_name.strip().casefold()}').hex
        item_name = matched.name if matched else item.item_name.strip()
        ref = {**ref, 'item_id': item_id, 'item_name': item_name,
               'holder_id': holder_id, 'holder_name': item.holder_name}
        payload = {'item_id': item_id, 'item_name': item_name, 'to_id': holder_id}
        if item.previous_holder_name:
            if not item.previous_holder_evidence:
                raise ValueError('previous holder precondition requires its own quote')
            previous_ref = quote(item.previous_holder_evidence, [item.item_name, item.previous_holder_name], before=ref['start'])
            if previous_ref is None or previous_ref['end'] > ref['start']:
                raise ValueError('previous holder evidence must precede final possession')
            previous_id = entity(item.previous_holder_name, 'character')
            if not previous_id:
                continue
            # This is a final-state projection, not every transfer in the
            # chapter. An intermediate owner is not an opening-state guard.
            ref['previous_holder_evidence'] = {**previous_ref, 'character_id': previous_id}
        add('TRANSFER_ITEM', payload, ref)
    return {'actions': actions, 'ignored': ignored}


class _StatusPayload(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    character_id: str = Field(min_length=1)
    status: Literal['alive', 'dead', 'missing', 'ambiguous', 'severely_injured']


class _MovePayload(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    character_id: str = Field(min_length=1)
    to_location: str = Field(min_length=1, max_length=200)


class _TransferPayload(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    item_id: str = Field(min_length=1)
    item_name: str = Field(min_length=1, max_length=120)
    to_id: str | None
    from_id: str | None = None


class _ActionEnvelope(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    action_id: str = Field(min_length=1)
    type: Literal['MOVE_CHARACTER', 'SET_CHARACTER_STATUS', 'TRANSFER_ITEM']
    payload: dict
    confidence: float = Field(ge=0, le=1)
    source_refs: list[dict] = Field(min_length=1, max_length=1)


def validate_chapter_end_actions(novel_id, chapter_number, content, raw_actions):
    """Reject malformed/tampered evidence at the evolution boundary."""
    if not isinstance(raw_actions, list) or len(raw_actions) > 24:
        raise ValueError('invalid chapter-end action list')
    actions = []
    payload_types = {'MOVE_CHARACTER': _MovePayload,
                     'SET_CHARACTER_STATUS': _StatusPayload, 'TRANSFER_ITEM': _TransferPayload}
    for raw in raw_actions:
        entry = _ActionEnvelope.model_validate(raw)
        payload_types[entry.type].model_validate(entry.payload)
        ref = entry.source_refs[0]
        if (ref.get('type') != 'chapter_end_evidence' or ref.get('novel_id') != novel_id
                or ref.get('chapter_number') != chapter_number):
            raise ValueError('chapter-end action belongs to another manuscript')
        def check_quote(value):
            if not isinstance(value, dict):
                raise ValueError('chapter-end action lacks evidence')
            start, end, quote = value.get('start'), value.get('end'), value.get('quote')
            if (type(start) is not int or type(end) is not int or not isinstance(quote, str)
                    or not quote or start < 0 or end <= start or end > len(content)
                    or content[start:end] != quote or _NONCURRENT.search(quote)):
                raise ValueError('chapter-end action lacks exact current evidence')
        check_quote(ref)
        if 'previous_holder_evidence' in ref:
            check_quote(ref['previous_holder_evidence'])
        if entry.type == 'TRANSFER_ITEM':
            if ref.get('item_id') != entry.payload['item_id'] or ref.get('item_name') != entry.payload['item_name']:
                raise ValueError('chapter-end item identity is inconsistent')
            if ref.get('holder_id') != entry.payload['to_id']:
                raise ValueError('chapter-end holder identity is inconsistent')
        else:
            if (ref.get('character_id') != entry.payload['character_id']
                    or not ref.get('character_name') or ref['character_name'] not in ref['quote']):
                raise ValueError('chapter-end character identity is inconsistent')
        digest = hashlib.sha256(json.dumps([novel_id, chapter_number, entry.type, entry.payload, ref],
            ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()
        if entry.action_id != digest:
            raise ValueError('chapter-end action identity does not match its evidence')
        actions.append(EvolutionAction.from_dict(entry.model_dump()))
    return actions


def make_chapter_end_resolver(novel_id, database=None, bible_repository=None):
    """Use the injected project's existing canonical IDs, without creating cast."""
    from application.narrative.entity_resolver import EntityResolver
    from domain.novel.value_objects.novel_id import NovelId

    characters, props = [], []
    bible_characters = []
    if database is not None:
        from infrastructure.persistence.database.unified_character_repository import SqliteUnifiedCharacterRepository
        from infrastructure.persistence.database.unified_prop_repository import SqliteUnifiedPropRepository
        characters = SqliteUnifiedCharacterRepository(database).list_by_novel(novel_id)
        props = SqliteUnifiedPropRepository(database).list_by_novel(novel_id)
    if bible_repository is not None:
        bible = bible_repository.get_by_novel_id(NovelId(novel_id))
        bible_characters = list(getattr(bible, 'characters', []) or [])
        known_ids = {character.id.value for character in characters}
        for character in bible_characters:
            identifier = character.character_id.value
            if identifier not in known_ids:
                characters.append(SimpleNamespace(id=SimpleNamespace(value=identifier), name=character.name))
                known_ids.add(identifier)
    characters = _prefer_unique_bible_character_ids(characters, bible_characters)
    return EntityResolver(
        character_repo=SimpleNamespace(list_by_novel=lambda _: characters),
        prop_repo=SimpleNamespace(list_by_novel=lambda _: props),
    )


def _prefer_unique_bible_character_ids(characters, bible_characters):
    """Drop stale duplicate unified rows when Bible proves one canonical ID.

    Chapter-end evidence resolves names, while Bible character IDs are the
    author-owned identity boundary.  A previous generation or sync can leave
    a second ``unified_characters`` row with the same name.  Keeping that row
    would make an otherwise provable name appear ambiguous.  We only filter a
    duplicate when the Bible has exactly one canonical ID for that name.  If
    the Bible itself contains multiple same-name characters, all canonical
    candidates remain so the strict ambiguity check still blocks the claim.
    """
    if not bible_characters:
        return characters

    def norm(value):
        return str(value or '').strip().casefold()

    canonical_by_name = {}
    canonical_ids = set()
    canonical_names_by_id = {}
    for character in bible_characters:
        identifier = getattr(getattr(character, 'character_id', None), 'value', '')
        name = getattr(character, 'name', '') or ''
        if not identifier:
            continue
        identifier = str(identifier)
        key = norm(name)
        canonical_by_name.setdefault(key, set()).add(identifier)
        canonical_ids.add(identifier)
        canonical_names_by_id[identifier] = name

    if not canonical_by_name:
        return characters

    result = []
    seen_ids = set()
    for character in characters:
        identifier = str(getattr(getattr(character, 'id', None), 'value', '') or '')
        name = getattr(character, 'name', '') or ''
        ids_for_name = canonical_by_name.get(norm(name), set())
        if len(ids_for_name) == 1 and identifier not in ids_for_name:
            # A unique Bible ID proves this unified row is stale or duplicate.
            continue
        if identifier in seen_ids:
            continue
        result.append(character)
        seen_ids.add(identifier)

    # The caller normally adds Bible characters first, but keep this helper
    # correct for direct use and for repositories that only expose stale rows.
    for identifier, name in canonical_names_by_id.items():
        if identifier not in seen_ids:
            result.append(SimpleNamespace(id=SimpleNamespace(value=identifier), name=name))
            seen_ids.add(identifier)
    return result
