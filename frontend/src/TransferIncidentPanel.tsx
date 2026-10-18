import {
  Alert,
  Box,
  Button,
  Card,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  LinearProgress,
  Stack,
  Tab,
  Tabs,
  Typography,
} from '@mui/material';
import { useInfiniteQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useMemo, useState } from 'react';
import { apiRequest } from './api';

type IncidentKind = 'failed' | 'omitted';
type TransferIncident = {
  transfer: {
    id: string;
    public_id: string;
    status: string;
    attempt_count: number;
    source_connection: { name: string; provider_type: string } | null;
    destination_connection: { name: string; provider_type: string } | null;
  };
  item: {
    id: string;
    source_path: string;
    destination_path: string;
    requested_destination_path: string;
    status: string;
    size_bytes: number | null;
    error_code: string | null;
    failure_stage: string | null;
    omission_kind: string | null;
    omitted_at: string | null;
    incident_at: string;
  };
  can_skip: boolean;
  skip_unavailable_reason: string | null;
  can_retry_omission: boolean;
};
type IncidentPage = {
  items: TransferIncident[];
  counts: { failed: number; omitted: number };
  next_cursor: string | null;
};

export type TransferIncidentPanelProps = {
  onSkip: (transferId: string, itemId: string, expectedAttempt: number, requestId: string) => Promise<void>;
  onRetry: (transferId: string, itemId: string, expectedAttempt: number, requestId: string) => Promise<void>;
};

function formatBytes(value: number | null): string {
  if (value === null) return 'Tamaño no informado';
  if (value === 0) return '0 bytes';
  const units = ['bytes', 'KB', 'MB', 'GB', 'TB'];
  const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  return `${new Intl.NumberFormat('es-CL', { maximumFractionDigits: index === 0 ? 0 : 1 }).format(value / (1024 ** index))} ${units[index]}`;
}

function useIncidentPage(kind: IncidentKind, enabled: boolean) {
  return useInfiniteQuery({
    queryKey: ['transfer-incidents', kind],
    enabled,
    initialPageParam: null as string | null,
    queryFn: ({ pageParam }) => {
      const params = new URLSearchParams({ kind, limit: '20' });
      if (pageParam) params.set('cursor', pageParam);
      return apiRequest<IncidentPage>(`/api/v1/transfer-incidents?${params}`);
    },
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    refetchInterval: () => document.visibilityState === 'visible' ? 15_000 : false,
    refetchIntervalInBackground: false,
  });
}

export default function TransferIncidentPanel({ onSkip, onRetry }: TransferIncidentPanelProps) {
  const queryClient = useQueryClient();
  const [kind, setKind] = useState<IncidentKind>('failed');
  const [candidate, setCandidate] = useState<TransferIncident | null>(null);
  const [action, setAction] = useState<'skip' | 'retry' | null>(null);
  const [actionError, setActionError] = useState('');
  const failedQuery = useIncidentPage('failed', kind === 'failed');
  const omittedQuery = useIncidentPage('omitted', kind === 'omitted');
  const query = kind === 'failed' ? failedQuery : omittedQuery;
  const pages = query.data?.pages ?? [];
  const incidents = useMemo(() => {
    const unique = new Map<string, TransferIncident>();
    for (const page of pages) {
      for (const incident of page.items) {
        const key = `${incident.transfer.id}:${incident.item.id}`;
        if (!unique.has(key)) unique.set(key, incident);
      }
    }
    return Array.from(unique.values());
  }, [pages]);
  const counts = pages[0]?.counts ?? (kind === 'failed'
    ? omittedQuery.data?.pages[0]?.counts
    : failedQuery.data?.pages[0]?.counts) ?? { failed: 0, omitted: 0 };

  useEffect(() => {
    if (kind === 'failed' && counts.failed === 0 && counts.omitted > 0) setKind('omitted');
  }, [counts.failed, counts.omitted, kind]);

  const confirmAction = async () => {
    if (!candidate || !action) return;
    setActionError('');
    try {
      const requestId = crypto.randomUUID();
      if (action === 'skip') {
        await onSkip(candidate.transfer.id, candidate.item.id, candidate.transfer.attempt_count, requestId);
      } else {
        await onRetry(candidate.transfer.id, candidate.item.id, candidate.transfer.attempt_count, requestId);
      }
      setCandidate(null);
      setAction(null);
      await queryClient.invalidateQueries({ queryKey: ['transfer-incidents'] });
    } catch (error) {
      setActionError(error instanceof Error ? error.message : 'No se pudo completar la acción.');
    }
  };

  const busy = query.isLoading || query.isFetchingNextPage;
  return (
    <Card className="incident-panel" variant="outlined" data-testid="incident-panel">
      <Box className="incident-panel-heading">
        <Box>
          <Typography variant="overline" color="text.secondary">ATENCIÓN REQUERIDA</Typography>
          <Typography variant="h6">Archivos con incidencias</Typography>
          <Typography variant="caption" color="text.secondary">
            Acceso directo a archivos con error y omisiones explícitas.
          </Typography>
        </Box>
        <Button
          size="small"
          onClick={() => void query.refetch()}
          aria-label="Actualizar incidencias"
        >
          Actualizar
        </Button>
      </Box>
      <Tabs
        value={kind}
        onChange={(_event, value: IncidentKind) => setKind(value)}
        aria-label="Tipos de incidencias de archivos"
      >
        <Tab value="failed" label={`Con error (${counts.failed})`} />
        <Tab value="omitted" label={`Omitidos (${counts.omitted})`} />
      </Tabs>
      {query.isLoading && <LinearProgress />}
      {query.isError && <Alert severity="error" sx={{ m: 1.5 }}>
        {query.error instanceof Error ? query.error.message : 'No se pudieron cargar las incidencias.'}
      </Alert>}
      <Stack className="incident-list" spacing={1}>
        {incidents.map((incident) => {
          const source = incident.transfer.source_connection;
          const destination = incident.transfer.destination_connection;
          return (
            <Box className="incident-row" key={`${kind}:${incident.transfer.id}:${incident.item.id}`}>
              <Box className="incident-row-heading">
                <Box>
                  <Typography variant="body2" fontWeight={750}>{incident.transfer.public_id}</Typography>
                  <Typography variant="caption" color="text.secondary">
                    {source?.name ?? 'Origen no disponible'} → {destination?.name ?? 'Destino no disponible'}
                  </Typography>
                </Box>
                <Stack direction="row" alignItems="center" spacing={0.75}>
                  <Chip size="small" label={incident.item.status === 'FAILED' ? 'Con error' : 'Omitido'} />
                  <Typography variant="caption" color="text.secondary">
                    Intento {incident.transfer.attempt_count}
                  </Typography>
                </Stack>
              </Box>
              <Box className="incident-routes">
                <Typography variant="body2" title={incident.item.source_path}>
                  <strong>Origen:</strong> {incident.item.source_path}
                </Typography>
                <Typography variant="body2" title={incident.item.destination_path}>
                  <strong>Destino:</strong> {incident.item.destination_path}
                </Typography>
                {incident.item.requested_destination_path !== incident.item.destination_path && (
                  <Typography variant="caption" color="text.secondary" title={incident.item.requested_destination_path}>
                    Destino solicitado: {incident.item.requested_destination_path}
                  </Typography>
                )}
              </Box>
              <Box className="incident-row-footer">
                <Typography variant="caption" color="text.secondary">
                  {(kind === 'omitted'
                    ? incident.item.omission_kind
                    : incident.item.error_code ?? incident.item.failure_stage)
                    ?? 'Sin detalle disponible'}
                  {' · '}{formatBytes(incident.item.size_bytes)}
                </Typography>
                <Stack direction="row" spacing={0.5}>
                  {incident.can_skip && (
                    <Button size="small" onClick={() => { setCandidate(incident); setAction('skip'); }}>
                      Omitir y continuar
                    </Button>
                  )}
                  {incident.can_retry_omission && (
                    <Button size="small" onClick={() => { setCandidate(incident); setAction('retry'); }}>
                      Reintentar archivo
                    </Button>
                  )}
                </Stack>
              </Box>
            </Box>
          );
        })}
        {!query.isLoading && incidents.length === 0 && (
          <Typography variant="body2" color="text.secondary" sx={{ p: 2 }}>
            {kind === 'failed' ? 'No hay archivos con error.' : 'No hay archivos omitidos.'}
          </Typography>
        )}
      </Stack>
      {query.hasNextPage && (
        <Button
          size="small"
          disabled={busy}
          onClick={() => void query.fetchNextPage()}
          sx={{ m: 1 }}
        >
          {query.isFetchingNextPage ? <CircularProgress size={16} /> : 'Cargar más incidencias'}
        </Button>
      )}
      <Dialog open={Boolean(candidate && action)} onClose={() => !busy && setCandidate(null)}>
        <DialogTitle>{action === 'skip' ? 'Confirmar omisión del archivo' : 'Confirmar reintento del archivo'}</DialogTitle>
        <DialogContent>
          <Stack spacing={1}>
            <Typography>
              {action === 'skip'
                ? 'El archivo no se copiará. Los demás archivos pendientes continuarán.'
                : 'El archivo omitido volverá a la cola de copia; los archivos ya copiados se conservarán.'}
            </Typography>
            {candidate && (
              <>
                <Typography variant="body2" fontWeight={700}>{candidate.item.source_path}</Typography>
                <Typography variant="caption" color="text.secondary">Destino: {candidate.item.destination_path}</Typography>
              </>
            )}
            {actionError && <Alert severity="error">{actionError}</Alert>}
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button disabled={busy} onClick={() => { setCandidate(null); setAction(null); }}>Cancelar</Button>
          <Button
            variant="contained"
            disabled={!candidate || !action || busy}
            onClick={() => void confirmAction()}
          >
            {busy ? <CircularProgress size={16} /> : action === 'skip' ? 'Confirmar omisión' : 'Confirmar reintento'}
          </Button>
        </DialogActions>
      </Dialog>
    </Card>
  );
}
