import { apiClient } from './config'
import type { BibleDTO, CharacterDTO } from './bible'

export interface CharacterMutationResult extends BibleDTO {
  character_mutation?: {
    renamed?: boolean
    changed_chapters?: number
    refreshed_records?: number
    derivation_jobs?: number
  }
}

const endpoint = (novelId: string, characterId: string) =>
  `/bible/novels/${encodeURIComponent(novelId)}/bible/characters/${encodeURIComponent(characterId)}`

export const characterProfilesApi = {
  update: (novelId: string, characterId: string, changes: Partial<Omit<CharacterDTO, 'id'>>, expected: CharacterDTO) =>
    apiClient.patch<CharacterMutationResult>(endpoint(novelId, characterId), { changes, expected_character: expected }),
  delete: (novelId: string, characterId: string, expected: CharacterDTO) =>
    apiClient.delete<CharacterMutationResult>(endpoint(novelId, characterId), { data: { expected_character: expected } }),
}
