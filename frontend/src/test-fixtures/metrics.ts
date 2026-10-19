/** Synthetic fixtures for component tests and the local visual review only. */
export const metricsUser = { id: 'metrics-admin', username: 'María', role: 'ADMIN' as const };
export const metricsConnections = [
  { id: 'mega-1', name: 'MEGA · Archivo histórico de formación y documentación técnica de Latinoamérica', provider_type: 'MEGA', status: 'ACTIVE' },
  { id: 'seafile-1', name: 'Seafile · Respaldo de operaciones', provider_type: 'SEAFILE', status: 'ACTIVE' },
  { id: 'mock-1', name: 'Conexión de prueba', provider_type: 'MOCK', status: 'ACTIVE' },
];

export function metricsFixture() {
  return {
    generated_at: '2026-10-18T20:00:00Z',
    filters: { range: '30d', from: '2026-09-19', to: '2026-10-18', timezone: 'America/Santiago' },
    summary: {
      transfers: { total: 18, by_status: { COMPLETED: 9, COMPLETED_WITH_WARNINGS: 2, FAILED: 3, CANCELLED: 1, RUNNING: 3 }, completed_cleanly: 9, completed_with_warnings: 2, failed: 3, cancelled: 1, success_rate: 78.6, additional_attempts: 4 },
      files: { total: 150, copied: 121, copied_bytes: 123456789012, copied_size_unknown: 1, failed: 7, skipped_historical: 2, omitted: 5, omitted_bytes: 1024, pending: 15, renamed: 30 },
    },
    providers: {
      source: [{ provider_type: 'MEGA', transfers: 18, files: { copied: 121, copied_bytes: 123456789012 } }],
      destination: [{ provider_type: 'SEAFILE', transfers: 18, files: { copied: 121, copied_bytes: 123456789012 } }],
      pairs: [{ label: 'MEGA → SEAFILE', transfers: 18, files: { copied: 121, copied_bytes: 123456789012 } }],
    },
    activity: [{ date: '2026-10-16', transfers_created: 4, copied: 79, copied_bytes: 90000000000 }, { date: '2026-10-18', transfers_created: 14, copied: 21, copied_bytes: 20000000000 }],
    performance: {
      stages: [{ stage: 'SOURCE_DOWNLOAD', provider_type: 'MEGA', connection_role: 'SOURCE', samples: 7, successful_samples: 4, failed_samples: 2, incomplete_samples: 1, median_ms: 42000, p95_ms: 90000, bytes_count: 123456789, effective_bytes_per_second: 1234567 }],
      states: [{ state: 'RUNNING', samples: 11, median_ms: 120000, p95_ms: 7200000 }],
    },
    reliability: { errors: [{ error_code: 'SOURCE_ACCESS_RESTRICTED', files: 7 }], size_distribution: [{ bucket: '1_to_100_mib', files: 100 }, { bucket: 'over_1_gib', files: 20 }, { bucket: 'unknown', files: 1 }], extensions: [{ extension: 'rar', files: 100 }, { extension: 'pdf', files: 21 }] },
    scheduling: { waiting: 2, paused: 1, by_wait_reason: [{ reason: 'WAITING_UNTIL', transfers: 2 }] },
    live: { active: 3, by_status: { RUNNING: 3 }, reported_speed_bps: 1234567 },
    coverage: { transfers: 18, with_manifest: 16, manifestless_transfers: 2, copied_files_with_date: 100, copied_files_without_date: 21, stage_samples: 7, stage_samples_incomplete: 1 },
    system: {
      environment: 'production', status: 'READY',
      services: { database: 'ready', redis: 'ready', workers: 'ready', beat: 'ready' },
      queues: [{ name: 'transfers', consumer_ready: true, pending: 2 }, { name: 'control', consumer_ready: true, pending: 0 }, { name: 'evaluations', consumer_ready: true, pending: 0 }, { name: 'notifications', consumer_ready: true, pending: 0 }],
      notifications: { by_status: { SENT: 14, FAILED: 1 } }, evaluations: { by_status: { COMPLETED: 10, PENDING: 2 } },
      connections: [{ provider_type: 'MEGA', status: 'ACTIVE', last_test_status: 'SUCCESS', count: 1 }],
    },
  };
}

export function worstMetricsFixture() {
  const data = metricsFixture();
  data.summary.files.copied = 123456789;
  data.summary.files.copied_bytes = 1125899906842624;
  data.summary.transfers.total = 12345678;
  data.performance.stages[0].median_ms = 1284 * 3600000;
  data.performance.stages[0].effective_bytes_per_second = 0.5;
  data.reliability.errors = [{ error_code: 'SOURCE_AUTHENTICATION_REQUIRED_AFTER_SESSION_EXPIRED', files: 1234567 }];
  data.system.status = 'DEGRADED';
  data.system.services.redis = 'unavailable';
  data.system.queues[2].consumer_ready = false;
  return data;
}

export function emptyMetricsFixture() {
  const data = metricsFixture();
  Object.keys(data.summary.transfers).forEach((key) => { if (key !== 'by_status') Object.assign(data.summary.transfers, { [key]: 0 }); });
  Object.keys(data.summary.files).forEach((key) => Object.assign(data.summary.files, { [key]: 0 }));
  data.summary.transfers.by_status = { COMPLETED: 0, COMPLETED_WITH_WARNINGS: 0, FAILED: 0, CANCELLED: 0, RUNNING: 0 };
  data.providers = { source: [], destination: [], pairs: [] };
  data.activity = [];
  data.performance = { stages: [], states: [] };
  data.reliability = { errors: [], extensions: [], size_distribution: [] };
  data.coverage = { transfers: 0, with_manifest: 0, manifestless_transfers: 0, copied_files_with_date: 0, copied_files_without_date: 0, stage_samples: 0, stage_samples_incomplete: 0 };
  data.scheduling = { waiting: 0, paused: 0, by_wait_reason: [] };
  data.live = { active: 0, by_status: { RUNNING: 0 }, reported_speed_bps: 0 };
  return data;
}
