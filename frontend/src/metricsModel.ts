export type Connection = { id: string; name: string; provider_type: string; status: string };
export type MetricsUser = { id: string; username: string; role: 'ADMIN' | 'USER' };
export type DashboardResponse = {
  generated_at: string;
  filters: { range: string; from: string; to: string; timezone: string };
  summary: {
    transfers: {
      total: number;
      by_status: Record<string, number>;
      completed_cleanly: number;
      completed_with_warnings: number;
      failed: number;
      cancelled: number;
      success_rate: number;
      additional_attempts: number;
    };
    files: {
      total: number;
      copied: number;
      copied_bytes: number;
      copied_size_unknown: number;
      failed: number;
      skipped_historical: number;
      omitted: number;
      omitted_bytes: number;
      pending: number;
      renamed: number;
    };
  };
  providers: {
    source: Array<{ provider_type: string; transfers: number; files: { copied: number; copied_bytes: number } }>;
    destination: Array<{ provider_type: string; transfers: number; files: { copied: number; copied_bytes: number } }>;
    pairs: Array<{ label: string; transfers: number; files: { copied: number; copied_bytes: number } }>;
  };
  activity: Array<{ date: string; transfers_created: number; copied: number; copied_bytes: number }>;
  performance: {
    stages: Array<{
      stage: string;
      provider_type: string | null;
      connection_role: string | null;
      samples: number;
      successful_samples: number;
      failed_samples: number;
      incomplete_samples: number;
      median_ms: number | null;
      p95_ms: number | null;
      bytes_count: number;
      effective_bytes_per_second: number | null;
    }>;
    states: Array<{ state: string; samples: number; median_ms: number | null; p95_ms: number | null }>;
  };
  reliability: {
    errors: Array<{ error_code: string; files: number }>;
    size_distribution: Array<{ bucket: string; files: number }>;
    extensions: Array<{ extension: string; files: number }>;
  };
  scheduling: { waiting: number; paused: number; by_wait_reason: Array<{ reason: string; transfers: number }> };
  live: { active: number; by_status: Record<string, number>; reported_speed_bps: number };
  coverage: {
    transfers: number;
    with_manifest: number;
    manifestless_transfers: number;
    copied_files_with_date: number;
    copied_files_without_date: number;
    stage_samples: number;
    stage_samples_incomplete: number;
  };
  system: null | {
    environment: string;
    status: string;
    services: Record<string, string>;
    queues: Array<{ name: string; consumer_ready: boolean; pending: number | null }>;
    notifications: { by_status?: Record<string, number> };
    evaluations: { by_status?: Record<string, number> };
    connections: Array<{ provider_type: string; status: string; last_test_status: string | null; count: number }>;
  };
};

export const providerLabel: Record<string, string> = {
  MEGA: 'MEGA', GOOGLE_DRIVE: 'Google Drive', SEAFILE: 'Seafile', SMB: 'SMB / NAS', NEXTCLOUD: 'Nextcloud',
};
export const stageLabel: Record<string, string> = {
  SOURCE_DOWNLOAD: 'Descarga del origen', SOURCE_HASH: 'Hash del origen',
  DESTINATION_UPLOAD: 'Carga al destino', DESTINATION_VERIFY: 'Verificación del destino',
  STAGING_CLEANUP: 'Limpieza temporal local',
};
export const sizeLabel: Record<string, string> = {
  zero: 'Vacíos', under_1_mib: '< 1 MiB', '1_to_100_mib': '1–100 MiB',
  '100_mib_to_1_gib': '100 MiB–1 GiB', over_1_gib: '> 1 GiB', unknown: 'Sin tamaño',
};
const decimals = new Intl.NumberFormat('es-CL', { maximumFractionDigits: 1 });
const integers = new Intl.NumberFormat('es-CL', { maximumFractionDigits: 0 });
export function number(value: number): string { return Number.isFinite(value) ? decimals.format(value) : '—'; }
export function count(value: number): string { return Number.isFinite(value) ? integers.format(value) : '—'; }
export function bytes(value: number): string {
  if (!Number.isFinite(value) || value < 0) return '—';
  if (value === 0) return '0 B';
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'];
  const index = Math.max(0, Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1));
  return `${number(value / (1024 ** index))} ${units[index]}`;
}
export function duration(value: number | null): string {
  if (value === null || !Number.isFinite(value) || value < 0) return '—';
  if (value < 1000) return `${number(value)} ms`;
  if (value < 60000) return `${number(value / 1000)} s`;
  if (value < 3600000) return `${number(value / 60000)} min`;
  if (value < 86400000) return `${number(value / 3600000)} h`;
  return `${number(value / 86400000)} días`;
}
export function dayLabel(value: string, year = false): string {
  return new Intl.DateTimeFormat('es-CL', { day: '2-digit', month: '2-digit', ...(year ? { year: 'numeric' } as const : {}), timeZone: 'UTC' }).format(new Date(`${value}T12:00:00Z`));
}
export function timeLabel(value: string): string {
  return new Intl.DateTimeFormat('es-CL', { hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(new Date(value));
}
export function transferStateLabel(value: string): string {
  return ({ SCHEDULED: 'Programada', QUEUED: 'En cola', VALIDATING: 'Validando', RUNNING: 'En ejecución', RETRYING: 'Reintentando', PAUSING: 'Pausando', PAUSED: 'Pausada', CANCELLING: 'Cancelando', COMPLETED: 'Completada', COMPLETED_WITH_WARNINGS: 'Con advertencias', CANCELLED: 'Cancelada', FAILED: 'Fallida' } as Record<string, string>)[value] ?? value;
}
export function statusPresentation(value: string): { label: string; color: 'success' | 'warning' | 'error' | 'default' | 'info' } {
  const statuses: Record<string, { label: string; color: 'success' | 'warning' | 'error' | 'default' | 'info' }> = {
    READY: { label: 'Operativa', color: 'success' }, DEGRADED: { label: 'Con incidencias', color: 'warning' }, STALE: { label: 'Sin actualizar', color: 'warning' }, REFRESH_PAUSED: { label: 'Lectura pausada', color: 'default' },
    ready: { label: 'Disponible', color: 'success' }, unavailable: { label: 'No disponible', color: 'error' }, degraded: { label: 'Degradado', color: 'warning' },
    SIMULATED: { label: 'Simulada', color: 'info' }, simulated: { label: 'Simulado', color: 'info' }, not_configured: { label: 'Sin configurar', color: 'default' },
    ACTIVE: { label: 'Activa', color: 'success' }, ARCHIVED: { label: 'Archivada', color: 'default' }, INACTIVE: { label: 'Inactiva', color: 'default' },
    SUCCESS: { label: 'Correcta', color: 'success' }, FAILED: { label: 'Fallida', color: 'error' }, ERROR: { label: 'Error', color: 'error' },
  };
  return statuses[value] ?? { label: value, color: 'default' };
}
export function taskStatusLabel(value: string): string {
  return ({ PENDING: 'Pendientes', QUEUED: 'En cola', SENT: 'Enviadas', COMPLETED: 'Completadas', READY: 'Preparadas', FAILED: 'Fallidas', ERROR: 'Con error', EVALUATING: 'Evaluando', APPROVED: 'Aprobadas', WAITING_PARENT: 'Esperando transferencia anterior', RUNNING: 'En ejecución', RETRYING: 'Reintentando', SENDING: 'Enviando', CLAIMED: 'Reservadas', CANCELLED: 'Canceladas', BLOCKED: 'Bloqueadas' } as Record<string, string>)[value] ?? value;
}
export function waitReasonLabel(value: string): string {
  return ({
    WAITING_UNTIL: 'Hora programada', WAITING_PARENT: 'Transferencia anterior',
    WAITING_EVALUATION: 'Evaluación pendiente', WAITING_USER_ACTIVE: 'Usuario inactivo',
    WAITING_CONNECTION_ACTIVE: 'Conexión inactiva', WAITING_CONNECTION_ACCESS: 'Permisos de conexión',
    WAITING_CONNECTION: 'Conexión ocupada', WAITING_CAPACITY: 'Capacidad de transferencia',
    WAITING_STAGING: 'Espacio temporal disponible', WAITING_EXCLUSIVE: 'Turno exclusivo',
    BLOCKED_CONNECTION_CHANGED: 'Conexión modificada', BLOCKED_EVALUATION: 'Evaluación bloqueada',
    BLOCKED_PARENT_FAILED: 'Transferencia anterior fallida', BLOCKED_PARENT_OMISSIONS: 'Omisiones de la transferencia anterior',
  } as Record<string, string>)[value] ?? value;
}
export type MetricsFilters = {
  range: string; sourceProvider: string; destinationProvider: string;
  sourceConnection: string; destinationConnection: string; selectedUser: string; dateFrom: string; dateTo: string;
};
export const defaultFilters: MetricsFilters = { range: '30d', sourceProvider: '', destinationProvider: '', sourceConnection: '', destinationConnection: '', selectedUser: '', dateFrom: '', dateTo: '' };
export function defaultCustomDates(): { dateFrom: string; dateTo: string } {
  const to = new Date(); const from = new Date(to); from.setDate(from.getDate() - 29);
  const localDay = (day: Date) => `${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, '0')}-${String(day.getDate()).padStart(2, '0')}`;
  return { dateFrom: localDay(from), dateTo: localDay(to) };
}
export function filterError(filters: MetricsFilters): string | null {
  if (filters.range !== 'custom') return null;
  if (!filters.dateFrom || !filters.dateTo) return 'Selecciona ambas fechas para aplicar el período.';
  const start = Date.parse(`${filters.dateFrom}T00:00:00Z`); const end = Date.parse(`${filters.dateTo}T00:00:00Z`);
  if (!Number.isFinite(start) || !Number.isFinite(end)) return 'Indica fechas válidas.';
  if (start > end) return 'La fecha desde debe ser anterior o igual a la fecha hasta.';
  if ((end - start) / 86400000 >= 366) return 'El período no puede superar los 366 días.';
  return null;
}
export function metricsQuery(filters: MetricsFilters, timezone: string): string {
  const query = new URLSearchParams({ range: filters.range, timezone });
  for (const [field, parameter] of [['sourceProvider', 'source_provider'], ['destinationProvider', 'destination_provider'], ['sourceConnection', 'source_connection_id'], ['destinationConnection', 'destination_connection_id'], ['selectedUser', 'user_id']] as const) {
    if (filters[field]) query.set(parameter, filters[field]);
  }
  if (filters.range === 'custom') { query.set('date_from', filters.dateFrom); query.set('date_to', filters.dateTo); }
  return query.toString();
}
export function dailyActivity(data: DashboardResponse): DashboardResponse['activity'] {
  const lookup = new Map(data.activity.map(day => [day.date, day]));
  const end = Date.parse(`${data.filters.to}T12:00:00Z`);
  const days: DashboardResponse['activity'] = [];
  for (let cursor = Date.parse(`${data.filters.from}T12:00:00Z`); cursor <= end && days.length < 366; cursor += 86400000) {
    const date = new Date(cursor).toISOString().slice(0, 10);
    days.push(lookup.get(date) ?? { date, transfers_created: 0, copied: 0, copied_bytes: 0 });
  }
  return days;
}
