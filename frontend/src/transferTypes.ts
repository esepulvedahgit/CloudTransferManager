import type { TransferEvaluation } from './ScheduledTransferEvaluation';

export type TransferManifestSummary = {
  copied: number;
  failed: number;
  skipped: number;
  pending: number;
  renamed?: number;
  omitted?: number;
  omitted_bytes?: number;
};

export type TransferRecord = {
  id: string;
  public_id: string;
  status: string;
  source_connection_id?: string;
  destination_connection_id: string;
  source_paths?: string[];
  conflict_policy?: string;
  warning_count?: number;
  destination_path: string;
  progress_percent: number;
  files_completed: number;
  files_total: number;
  bytes_transferred: number;
  bytes_total: number;
  current_item: string | null;
  average_speed_bps: number | null;
  current_speed_bps: number | null;
  attempt_count?: number;
  estimated_remaining_seconds?: number | null;
  created_at?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
  scheduled_at?: string | null;
  schedule_timezone?: string;
  execution_mode?: 'AUTO' | 'EXCLUSIVE';
  depends_on_transfer_id?: string | null;
  accepted_parent_omissions_attempt?: number | null;
  wait_reason?: string | null;
  pause_requested_at?: string | null;
  paused_at?: string | null;
  state_version?: number;
  pause_supported?: boolean;
  pause_unavailable_reason?: string | null;
  can_edit_schedule?: boolean;
  evaluation?: TransferEvaluation;
  manifest_summary?: TransferManifestSummary;
};

export type TransferManifestItem = {
  id: string;
  source_path: string;
  source_object_id?: string | null;
  destination_path: string;
  requested_destination_path: string;
  rename_reason: string | null;
  is_dir: boolean;
  size_bytes: number | null;
  modified_at?: string | null;
  scan_complete?: boolean;
  status: 'DIRECTORY' | 'PENDING' | 'COPIED' | 'SKIPPED' | 'FAILED';
  error_code: string | null;
  verified_by: string | null;
  can_skip?: boolean;
  skip_unavailable_reason?: string | null;
  can_retry_omission?: boolean;
  omission_kind?: string | null;
  omitted_at?: string | null;
};

export type TransferManifestPage = {
  items: TransferManifestItem[];
  next_cursor: string | null;
  counts: {
    total: number;
    pending: number;
    copied: number;
    skipped: number;
    failed: number;
    omitted?: number;
    omitted_bytes?: number;
  };
};
