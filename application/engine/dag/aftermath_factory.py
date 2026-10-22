"""Build existing chapter services with one explicit project database/provider."""
from pathlib import Path


def build_scoped_chapter_aftermath(db, llm, *, indexing_service=None):
    from application.engine.services.chapter_aftermath_pipeline import ChapterAftermathPipeline
    from application.world.services.knowledge_service import KnowledgeService
    from application.world.services.bible_service import BibleService
    from application.analyst.services.voice_drift_service import VoiceDriftService
    from application.character.services.character_narrative_kernel import CharacterNarrativeKernel
    from application.checkpoint.services.unified_checkpoint_service import UnifiedCheckpointService
    from application.evolution.services.snapshot_service import EvolutionSnapshotService
    from application.evolution.services.action_extractor import EvolutionActionExtractor
    from infrastructure.persistence.database.sqlite_knowledge_repository import SqliteKnowledgeRepository
    from infrastructure.persistence.database.sqlite_chapter_repository import SqliteChapterRepository
    from infrastructure.persistence.database.sqlite_bible_repository import SqliteBibleRepository
    from infrastructure.persistence.database.sqlite_novel_repository import SqliteNovelRepository
    from infrastructure.persistence.database.sqlite_narrative_debt_repository import SqliteNarrativeDebtRepository
    from infrastructure.persistence.database.sqlite_foreshadowing_repository import SqliteForeshadowingRepository
    from infrastructure.persistence.database.sqlite_voice_fingerprint_repository import SQLiteVoiceFingerprintRepository
    from infrastructure.persistence.database.sqlite_chapter_style_score_repository import SqliteChapterStyleScoreRepository
    from infrastructure.persistence.database.sqlite_storyline_repository import SqliteStorylineRepository
    from infrastructure.persistence.database.sqlite_plot_arc_repository import SqlitePlotArcRepository
    from infrastructure.persistence.database.sqlite_narrative_event_repository import SqliteNarrativeEventRepository
    from infrastructure.persistence.database.sqlite_causal_edge_repository import SqliteCausalEdgeRepository
    from infrastructure.persistence.database.sqlite_character_state_repository import SqliteCharacterStateRepository
    from infrastructure.persistence.database.sqlite_evolution_repository import SqliteEvolutionRepository
    from infrastructure.persistence.database.unified_character_repository import SqliteUnifiedCharacterRepository
    from infrastructure.persistence.database.chapter_element_repository import ChapterElementRepository
    from infrastructure.persistence.database.story_node_repository import StoryNodeRepository
    from infrastructure.persistence.database.triple_repository import TripleRepository
    chapters, bibles = SqliteChapterRepository(db), SqliteBibleRepository(db)
    debts, foreshadows = SqliteNarrativeDebtRepository(db), SqliteForeshadowingRepository(db)
    triples, states = TripleRepository(db), SqliteCharacterStateRepository(db)
    characters = SqliteUnifiedCharacterRepository(db)
    bible_service = BibleService(bibles, novel_repository=SqliteNovelRepository(db), chapter_repository=chapters,
                                 unified_character_repository=characters)
    kernel = CharacterNarrativeKernel(bible_service=bible_service, bible_repository=bibles,
        chapter_element_repository=ChapterElementRepository(db.db_path), story_node_repository=StoryNodeRepository(db),
        triple_repository=triples, character_state_repository=states, debt_repository=debts,
        unified_character_repository=characters)
    if indexing_service is None:
        indexing_service = _indexing(db)
    return ChapterAftermathPipeline(
        knowledge_service=KnowledgeService(SqliteKnowledgeRepository(db)),
        chapter_indexing_service=indexing_service, llm_service=llm,
        voice_drift_service=VoiceDriftService(SqliteChapterStyleScoreRepository(db), SQLiteVoiceFingerprintRepository(db)),
        triple_repository=triples, foreshadowing_repository=foreshadows,
        storyline_repository=SqliteStorylineRepository(db), chapter_repository=chapters,
        plot_arc_repository=SqlitePlotArcRepository(db), narrative_event_repository=SqliteNarrativeEventRepository(db),
        causal_edge_repository=SqliteCausalEdgeRepository(db), character_state_repository=states,
        debt_repository=debts, bible_repository=bibles,
        unified_checkpoint_service=UnifiedCheckpointService(db=db, chapter_repository=chapters, foreshadowing_repo=foreshadows),
        evolution_snapshot_service=EvolutionSnapshotService(SqliteEvolutionRepository(db), EvolutionActionExtractor()),
        character_narrative_kernel=kernel, prop_lifecycle_syncer=_props(db, llm, characters),
    )


def _indexing(db):
    from infrastructure.ai.vector_store_environment import VectorStoreEnvironmentSettings
    if not VectorStoreEnvironmentSettings.from_env().enabled:
        return None
    from application.ai.embedding_config_service import EmbeddingConfigService
    cfg = EmbeddingConfigService(db).get_config()
    if cfg.mode == 'openai':
        if not cfg.api_key or not cfg.model.strip():
            return None
        from infrastructure.ai.openai_embedding_service import OpenAIEmbeddingService
        embedding = OpenAIEmbeddingService(api_key=cfg.api_key, base_url=cfg.base_url or None, model=cfg.model)
    else:
        if not cfg.model_path.strip():
            return None
        from infrastructure.ai.local_embedding_service import LocalEmbeddingService
        embedding = LocalEmbeddingService(model_name=cfg.model_path, use_gpu=cfg.use_gpu)
    from infrastructure.ai.chromadb_vector_store import ChromaDBVectorStore
    from application.analyst.services.chapter_indexing_service import ChapterIndexingService
    # Each project gets its own files, even when novels share an ID across DBs.
    vectors = ChromaDBVectorStore(persist_directory=str(Path(db.db_path).parent / 'chromadb'), database=db)
    return ChapterIndexingService(vectors, embedding)


def _props(db, llm, characters):
    from application.prop.services.lifecycle_syncer import PropLifecycleSyncer
    from application.narrative.entity_resolver import EntityResolver
    from application.prop.extractors.pattern_extractor import PatternExtractor
    from application.prop.extractors.llm_extractor import LlmExtractor
    from application.prop.handlers.narrative_event_handler import NarrativePropEventHandler
    from application.prop.handlers.triple_handler import TriplePropEventHandler
    from infrastructure.persistence.database.unified_prop_repository import SqliteUnifiedPropRepository
    from infrastructure.persistence.database.sqlite_prop_event_repository import SqlitePropEventRepository
    from infrastructure.persistence.database.sqlite_narrative_event_repository import SqliteNarrativeEventRepository
    props = SqliteUnifiedPropRepository(db)
    resolver = EntityResolver(character_repo=characters, prop_repo=props)
    return PropLifecycleSyncer(props, SqlitePropEventRepository(db),
        [PatternExtractor(), LlmExtractor(llm, resolver)],
        [NarrativePropEventHandler(SqliteNarrativeEventRepository(db)), TriplePropEventHandler(db)])
