import { useEffect, useState } from 'react';
import { Alert, Box, Button, Stack, Typography } from '@mui/material';

export type TransferEvaluation = {
  status: 'PENDING' | 'EVALUATING' | 'APPROVED' | 'BLOCKED' | 'ERROR' | 'WAITING_PARENT' | 'NOT_REQUIRED';
  generation: number;
  sequence: number;
  files_found: number | null;
  count_complete: boolean;
  max_files: number | null;
  started_at: string | null;
  updated_at: string | null;
  completed_at: string | null;
  safe_error_code: string | null;
  safe_message: string | null;
};

type Props = {
  evaluation: TransferEvaluation;
  onRetry: () => void;
  retrying: boolean;
};

const numberFormatter = new Intl.NumberFormat('es-CL');

function count(value: number | null): string {
  return numberFormatter.format(value ?? 0);
}

function formatDate(value: string | null): string | null {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return new Intl.DateTimeFormat('es-CL', { dateStyle: 'medium', timeStyle: 'short' }).format(date);
}

function description(evaluation: TransferEvaluation): string {
  const limit = evaluation.max_files === null ? null : count(evaluation.max_files);
  const limitText = limit ? ` · límite ${limit}` : '';
  switch (evaluation.status) {
    case 'PENDING':
      return 'Pendiente de evaluación · cantidad todavía desconocida.';
    case 'EVALUATING':
      return evaluation.files_found === null
        ? `Evaluando la selección · cantidad todavía desconocida${limitText}.`
        : `Evaluando · ${count(evaluation.files_found)} archivos encontrados${limitText}.`;
    case 'APPROVED':
      return evaluation.count_complete
        ? `Programada · ${count(evaluation.files_found)} archivos · conteo completo${limitText}.`
        : `Evaluación aprobada · al menos ${count(evaluation.files_found)} archivos${limitText}.`;
    case 'BLOCKED':
      if (evaluation.files_found === null) {
        return `Bloqueada · no se pudo completar el conteo${limitText}.`;
      }
      return evaluation.count_complete
        ? `Bloqueada · ${count(evaluation.files_found)} archivos · conteo completo${limitText}.`
        : `Bloqueada · al menos ${count(evaluation.files_found)} archivos${limitText}.`;
    case 'ERROR':
      return 'No se pudo evaluar la selección.';
    case 'WAITING_PARENT':
      return 'Pendiente de evaluar tras la tarea padre.';
    case 'NOT_REQUIRED':
      return 'Sin evaluación anticipada · se validará al ejecutar.';
    default:
      return 'Estado de evaluación no disponible.';
  }
}

export default function ScheduledTransferEvaluation({ evaluation, onRetry, retrying }: Props) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (evaluation.status !== 'EVALUATING' || !evaluation.started_at) return undefined;
    const interval = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(interval);
  }, [evaluation.started_at, evaluation.status]);

  const startedAt = evaluation.started_at ? new Date(evaluation.started_at).getTime() : Number.NaN;
  const isTakingLong = evaluation.status === 'EVALUATING'
    && !Number.isNaN(startedAt)
    && now - startedAt >= 60_000;
  const evaluatedAt = evaluation.status === 'APPROVED' ? formatDate(evaluation.completed_at) : null;
  const safeMessage = evaluation.safe_message;
  const canRetry = evaluation.status === 'BLOCKED' || evaluation.status === 'ERROR';

  return (
    <Box className="scheduled-evaluation" role="group" aria-label="Evaluación de la selección">
      <Typography variant="body2" role="status" aria-live="polite">
        {description(evaluation)}
      </Typography>
      {evaluatedAt && (
        <Typography variant="caption" color="text.secondary">
          Evaluada: {evaluatedAt}
        </Typography>
      )}
      {safeMessage && evaluation.status !== 'APPROVED' && (
        <Typography variant="caption" color={evaluation.status === 'BLOCKED' || evaluation.status === 'ERROR' ? 'error.main' : 'text.secondary'}>
          {safeMessage}
        </Typography>
      )}
      {isTakingLong && (
        <Alert severity="info" className="scheduled-evaluation-delay">
          La revisión continúa; el tiempo depende de las carpetas y del proveedor.
        </Alert>
      )}
      {canRetry && (
        <Stack direction="row" alignItems="center" spacing={1}>
          <Button size="small" onClick={onRetry} disabled={retrying}>
            {retrying ? 'Solicitando…' : 'Volver a evaluar'}
          </Button>
        </Stack>
      )}
    </Box>
  );
}
