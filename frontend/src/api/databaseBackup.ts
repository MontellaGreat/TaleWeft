import { apiClient } from './config'

export interface DatabaseBackup {
  backup_id: string
  created_at: string
  directory: string
  scope: 'sqlite_database' | 'sqlite_database_and_vectors'
  status: string
  database: { size: number; sha256: string }
  validation: { integrity_check: string; foreign_key_check: string; counts: Record<string, number> }
}
export interface DatabaseRestoreCopy {
  backup_id: string
  status: string
  restored_database: string
  active_database_changed: boolean
  validation: DatabaseBackup['validation']
}
export interface DatabaseRestoreRequest {
  request_id: string
  status: string
  active_database_changed: boolean
  expected_sha256: string
}
const base = '/system/database-backups'
export const databaseBackupApi = {
  list: () => apiClient.get<DatabaseBackup[]>(base) as Promise<DatabaseBackup[]>,
  create: () => apiClient.post<DatabaseBackup>(base, {}) as Promise<DatabaseBackup>,
  verify: (id: string) => apiClient.post<DatabaseBackup>(`${base}/${encodeURIComponent(id)}/verify`, {}) as Promise<DatabaseBackup>,
  restoreCopy: (id: string) => apiClient.post<DatabaseRestoreCopy>(`${base}/${encodeURIComponent(id)}/restore-copy`, {}) as Promise<DatabaseRestoreCopy>,
  requestRestore: (id: string, expected_sha256: string) => apiClient.post<DatabaseRestoreRequest>(`${base}/${encodeURIComponent(id)}/restore-request`, { expected_sha256 }) as Promise<DatabaseRestoreRequest>,
  restoreStatus: () => apiClient.get<DatabaseRestoreRequest | { status: string }>('/system/database-restore-status') as Promise<DatabaseRestoreRequest | { status: string }>,
  cancelRestore: () => apiClient.delete<{ status: string }>('/system/database-restore-request') as Promise<{ status: string }>,
}
