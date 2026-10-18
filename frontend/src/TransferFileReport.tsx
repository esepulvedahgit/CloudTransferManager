import {
  Alert,
  Box,
  Button,
  Checkbox,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Stack,
  Typography,
} from '@mui/material';
import { useMemo, useRef, useState } from 'react';
import { ApiError } from './api';
import type { TransferManifestItem, TransferManifestPage, TransferRecord } from './transferTypes';

export type TransferFileReportProps = {
  transfer: TransferRecord;
  pages: TransferManifestPage[];
  isLoading: boolean;
  isError: boolean;
  hasNextPage: boolean;
  isFetchingNextPage: boolean;
  onLoadMore: () => void;
  onSkipAndResume: (itemId: string, requestId: string) => Promise<void>;
  onRetryOmitted: (itemIds: string[], requestId: string) => Promise<void>;
  onDownloadOmissions: () => void;
  onConflictRefresh?: () => void;
};

const omissionConfirmation =
  'Este archivo no se copiará. Los archivos ya copiados se conservarán y la transferencia continuará con los restantes';
const terminalStatuses = ['COMPLETED', 'COMPLETED_WITH_WARNINGS', 'FAILED', 'CANCELLED'];

function formatBytes(value: number): string {
  if (value === 0) return '0 bytes';
  const units = ['bytes', 'KB', 'MB', 'GB', 'TB'];
  const unitIndex = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  const amount = value / (1024 ** unitIndex);
  return `${new Intl.NumberFormat('es-CL', { maximumFractionDigits: unitIndex === 0 ? 0 : 1 }).format(amount)} ${units[unitIndex]}`;
}

function errorStatus(error: unknown): number | undefined {
  if (error instanceof ApiError) return error.status;
  if (typeof error === 'object' && error !== null && 'status' in error) {
    const status = (error as { status?: unknown }).status;
    return typeof status === 'number' ? status : undefined;
  }
  return undefined;
}

function displayError(error: unknown): string {
  if (error instanceof Error && error.message && !('status' in error && error.status === undefined)) {
    return error.message;
  }
  return 'No se pudo completar la acción. Puedes volver a intentarlo.';
}

function statusLabel(status: TransferManifestItem['status']): string {
  if (status === 'SKIPPED') return 'Omitido';
  if (status === 'COPIED') return 'Copiado';
  if (status === 'FAILED') return 'Con error';
  if (status === 'DIRECTORY') return 'Carpeta';
  return 'Pendiente';
}

export default function TransferFileReport({
  transfer,
  pages,
  isLoading,
  isError,
  hasNextPage,
  isFetchingNextPage,
  onLoadMore,
  onSkipAndResume,
  onRetryOmitted,
  onDownloadOmissions,
  onConflictRefresh,
}: TransferFileReportProps) {
  const items = useMemo(() => pages.flatMap((page) => page.items), [pages]);
  const explicitOmissions = items.filter((item) => item.omission_kind && item.omitted_at);
  const omittedCount = transfer.manifest_summary?.omitted
    ?? pages[0]?.counts.omitted
    ?? explicitOmissions.length;
  const omittedBytes = transfer.manifest_summary?.omitted_bytes
    ?? pages[0]?.counts.omitted_bytes
    ?? explicitOmissions.reduce((total, item) => total + (item.size_bytes ?? 0), 0);
  const copiedCount = transfer.manifest_summary?.copied
    ?? pages[0]?.counts.copied
    ?? items.filter((item) => item.status === 'COPIED').length;
  const failedCount = transfer.manifest_summary?.failed
    ?? pages[0]?.counts.failed
    ?? items.filter((item) => item.status === 'FAILED').length;
  const isTerminal = terminalStatuses.includes(transfer.status);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [skipCandidate, setSkipCandidate] = useState<TransferManifestItem | null>(null);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const requestIds = useRef(new Map<string, string>());

  const requestIdFor = (key: string) => {
    const existing = requestIds.current.get(key);
    if (existing) return existing;
    const requestId = crypto.randomUUID();
    requestIds.current.set(key, requestId);
    return requestId;
  };

  const runAction = async (
    key: string,
    action: (requestId: string) => Promise<void>,
    onSuccess?: () => void,
  ) => {
    if (busyAction) return;
    setBusyAction(key);
    setActionError(null);
    try {
      await action(requestIdFor(key));
      requestIds.current.delete(key);
      onSuccess?.();
    } catch (error) {
      const status = errorStatus(error);
      setActionError(status === 409
        ? 'La transferencia cambió desde que abriste este informe. Se actualizó el estado; revisa los archivos antes de volver a intentarlo.'
        : displayError(error));
      if (status === 409) {
        requestIds.current.delete(key);
        onConflictRefresh?.();
      }
    } finally {
      setBusyAction(null);
    }
  };

  const retryableItems = items.filter((item) => item.can_retry_omission);
  const selectedRetryableIds = selectedIds.filter((id) => retryableItems.some((item) => item.id === id));
  const selectedItems = retryableItems.filter((item) => selectedRetryableIds.includes(item.id));
  const retryKey = `retry:${transfer.id}:${transfer.attempt_count ?? 1}:${[...selectedRetryableIds].sort().join(',')}`;

  return (
    <Box aria-label="Informe de archivos de transferencia">
      <Stack spacing={1.25}>
        {omittedCount > 0 && (
          <Alert severity="warning" role="status">
            <Typography fontWeight={700}>
              {transfer.status === 'FAILED'
                ? 'Transferencia detenida con archivos omitidos'
                : transfer.status === 'CANCELLED'
                  ? 'Transferencia cancelada con archivos omitidos'
                  : isTerminal
                    ? 'Procesamiento finalizado con advertencias'
                    : 'Archivos omitidos durante la transferencia'}
            </Typography>
            <Typography variant="body2">
              {omittedCount} {omittedCount === 1 ? 'archivo omitido' : 'archivos omitidos'} · {formatBytes(omittedBytes)}.
              {' '}{isTerminal ? 'Los archivos copiados se conservaron.' : 'La transferencia continúa con los archivos restantes.'}
            </Typography>
          </Alert>
        )}
        {actionError && <Alert severity="error" role="alert">{actionError}</Alert>}
        <Stack direction="row" alignItems="center" justifyContent="space-between" spacing={2}>
          <Box>
            <Typography variant="overline" color="text.secondary">ARCHIVOS Y NOMBRES ASIGNADOS</Typography>
            <Typography variant="h6">{transfer.public_id}</Typography>
            <Typography variant="caption" color="text.secondary">
              {copiedCount} copiados · {omittedCount} omitidos · {failedCount} con error
            </Typography>
          </Box>
          {omittedCount > 0 && (
            <Button size="small" onClick={onDownloadOmissions}>
              Descargar CSV de omisiones
            </Button>
          )}
        </Stack>
        {isLoading && <CircularProgress size={22} aria-label="Cargando archivos" />}
        {isError && <Alert severity="error">No se pudo cargar el resultado de la transferencia.</Alert>}
        <Stack spacing={0.75}>
          {items.map((item) => {
            const statusColor = item.status === 'FAILED'
              ? 'error'
              : item.status === 'COPIED'
                ? 'success'
                : item.omission_kind
                  ? 'warning'
                  : 'default';
            return (
              <Box key={item.id} className="history-row" data-testid={`manifest-row-${item.id}`}>
                <Chip size="small" label={statusLabel(item.status)} color={statusColor} />
                <Box className="history-label">
                  <Typography variant="body2" title={item.source_path}>{item.source_path}</Typography>
                  {item.requested_destination_path !== item.destination_path && (
                    <Typography variant="caption" color="text.secondary" title={item.requested_destination_path}>
                      Nombre solicitado: {item.requested_destination_path}
                    </Typography>
                  )}
                  <Typography variant="caption" color="text.secondary" title={item.destination_path}>
                    {item.destination_path}
                  </Typography>
                  {item.omission_kind && (
                    <Typography variant="caption" color="warning.dark" display="block">
                      Motivo: {item.omission_kind} · Omitido: {item.omitted_at ? new Date(item.omitted_at).toLocaleString('es-CL') : 'fecha no disponible'}
                    </Typography>
                  )}
                </Box>
                {item.error_code && <Typography variant="caption" color="error">{item.error_code}</Typography>}
                {item.can_skip && (
                  <Button
                    size="small"
                    disabled={Boolean(busyAction)}
                    onClick={() => setSkipCandidate(item)}
                  >
                    Omitir este archivo y continuar
                  </Button>
                )}
                {item.can_retry_omission && (
                  <Checkbox
                    checked={selectedRetryableIds.includes(item.id)}
                    disabled={Boolean(busyAction) || (!selectedRetryableIds.includes(item.id) && selectedRetryableIds.length >= 100)}
                    inputProps={{ 'aria-label': `Seleccionar ${item.source_path} para reintento` }}
                    onChange={(_event, checked) => setSelectedIds((current) => (
                      checked ? [...current, item.id] : current.filter((id) => id !== item.id)
                    ))}
                  />
                )}
              </Box>
            );
          })}
        </Stack>
        {selectedRetryableIds.length > 0 && (
          <Button
            variant="outlined"
            disabled={Boolean(busyAction) || selectedRetryableIds.length > 100}
            onClick={() => void runAction(
              retryKey,
              (requestId) => onRetryOmitted(selectedRetryableIds, requestId),
              () => setSelectedIds([]),
            )}
          >
            {busyAction === retryKey && <CircularProgress size={16} sx={{ mr: 1 }} />}
            Reintentar seleccionados ({selectedRetryableIds.length})
          </Button>
        )}
        {hasNextPage && (
          <Button size="small" disabled={isFetchingNextPage} onClick={onLoadMore}>
            {isFetchingNextPage ? <CircularProgress size={18} /> : 'Cargar más resultados'}
          </Button>
        )}
      </Stack>

      <Dialog open={Boolean(skipCandidate)} onClose={() => !busyAction && setSkipCandidate(null)}>
        <DialogTitle>Confirmar omisión del archivo</DialogTitle>
        <DialogContent>
          <Typography>{omissionConfirmation}</Typography>
          {skipCandidate && (
            <Box sx={{ mt: 1 }}>
              <Typography fontWeight={700}>{skipCandidate.source_path}</Typography>
              {skipCandidate.error_code && (
                <Typography variant="caption" color="text.secondary">Razón: {skipCandidate.error_code}</Typography>
              )}
            </Box>
          )}
        </DialogContent>
        <DialogActions>
          <Button disabled={Boolean(busyAction)} onClick={() => setSkipCandidate(null)}>Cancelar</Button>
          <Button
            variant="contained"
            disabled={Boolean(busyAction) || !skipCandidate}
            onClick={() => {
              if (!skipCandidate) return;
              const candidate = skipCandidate;
              const key = `skip:${transfer.id}:${transfer.attempt_count ?? 1}:${candidate.id}`;
              void runAction(key, (requestId) => onSkipAndResume(candidate.id, requestId), () => setSkipCandidate(null));
            }}
          >
            {busyAction?.startsWith('skip:') && <CircularProgress size={16} sx={{ mr: 1 }} />}
            Confirmar omisión
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
