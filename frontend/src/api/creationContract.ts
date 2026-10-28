import { apiClient } from './config'

export interface CreationContract {
  locked_genre: string
  locked_world_preset: string
  locked_story_structure: string
  locked_pacing_control: string
  locked_writing_style: string
  locked_special_requirements: string
  target_chapters: number
  target_words_per_chapter: number
  style_notes: string
  premise_lock: string
}

export interface CreationContractState {
  contract: CreationContract
  version: string
  structured_market: boolean
}

export const creationContractApi = {
  get: (novelId: string) => apiClient.get<CreationContractState>(`/novels/${encodeURIComponent(novelId)}/creation-contract`) as Promise<CreationContractState>,
  save: (novelId: string, request: { version: string; contract: CreationContract; confirm_market_profile: boolean }) =>
    apiClient.put<CreationContractState>(`/novels/${encodeURIComponent(novelId)}/creation-contract`, request) as Promise<CreationContractState>,
}
