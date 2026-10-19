import { useId, useMemo, useState, type ReactNode } from 'react';
import {
  Alert, Box, Button, Card, CardContent, Chip, CircularProgress, FormControl,
  FormHelperText, InputLabel, MenuItem, Select, Skeleton, Stack, TextField,
  ToggleButton, ToggleButtonGroup, Typography,
} from '@mui/material';
import { BarChart, LineChart, PieChart } from '@mui/x-charts';
import { useQuery } from '@tanstack/react-query';
import { apiRequest } from './api';
import {
  bytes, count, dailyActivity, dayLabel, defaultCustomDates, defaultFilters, duration,
  filterError, metricsQuery, number, providerLabel, sizeLabel, stageLabel,
  statusPresentation, taskStatusLabel, timeLabel, transferStateLabel, waitReasonLabel,
  type Connection, type DashboardResponse, type MetricsFilters, type MetricsUser,
} from './metricsModel';

function MetricCard({ label, value, detail, tint }: { label: string; value: string; detail: string; tint: string }) {
  return <Card aria-label={label} className="metrics-kpi" elevation={0} sx={{ borderTop: `3px solid ${tint}` }}>
    <CardContent>
      <Typography component="h3" variant="body2" color="text.secondary">{label}</Typography>
      <Typography className="metrics-kpi-value" variant="h4" fontWeight={750}>{value}</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>{detail}</Typography>
    </CardContent>
  </Card>;
}

function Panel({ title, subtitle, action, children }: { title: string; subtitle?: string; action?: ReactNode; children: ReactNode }) {
  const titleId = useId();
  return <Card component="section" aria-labelledby={titleId} className="metrics-panel" elevation={0}>
    <CardContent>
      <Box className="metrics-panel-heading">
        <Box sx={{ minWidth: 0 }}>
          <Typography id={titleId} component="h2" variant="h6" fontWeight={720}>{title}</Typography>
          {subtitle && <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>{subtitle}</Typography>}
        </Box>
        {action}
      </Box>
      {children}
    </CardContent>
  </Card>;
}

function FilterSelect({ label, value, options, onChange, disabled, helper }: {
  label: string; value: string; options: Array<{ value: string; label: string }>;
  onChange: (value: string) => void; disabled?: boolean; helper?: string;
}) {
  const id = useId();
  return <FormControl size="small" className="metrics-filter" disabled={disabled}>
    <InputLabel id={id} shrink>{label}</InputLabel>
    <Select labelId={id} label={label} displayEmpty value={value} onChange={event => onChange(event.target.value)}
      SelectDisplayProps={{ title: options.find(option => option.value === value)?.label }}
      MenuProps={{ slotProps: { paper: { sx: { maxWidth: 'min(560px, calc(100vw - 32px))', '& .MuiMenuItem-root': { whiteSpace: 'normal', overflowWrap: 'anywhere' } } } } }}>
      {options.map(option => <MenuItem key={option.value} value={option.value}>{option.label}</MenuItem>)}
    </Select>
    {helper && <FormHelperText>{helper}</FormHelperText>}
  </FormControl>;
}

function Filters({ draft, onChange, onApply, onReset, dirty, connections, users, admin, usersFailed, retryUsers }: {
  draft: MetricsFilters; onChange: (patch: Partial<MetricsFilters>) => void; onApply: () => void;
  onReset: () => void; dirty: boolean; connections: Connection[]; users: Array<{ id: string; username: string }>;
  admin: boolean; usersFailed: boolean; retryUsers: () => void;
}) {
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const advancedId = useId();
  const error = filterError(draft);
  const advancedCount = [draft.selectedUser, draft.sourceProvider, draft.destinationProvider, draft.sourceConnection, draft.destinationConnection].filter(Boolean).length;
  const cloudOptions = [{ value: '', label: 'Todas' }, ...Object.entries(providerLabel).map(([value, label]) => ({ value, label }))];
  const connectionOptions = (provider: string) => [{ value: '', label: 'Todas' }, ...connections
    .filter(connection => connection.provider_type !== 'MOCK' && (!provider || connection.provider_type === provider))
    .map(connection => ({ value: connection.id, label: connection.name }))];
  return <Card component="form" aria-label="Filtros de métricas" className="metrics-filters-card" elevation={0}
    onSubmit={event => { event.preventDefault(); if (!error) onApply(); }}>
    <CardContent>
      <Box className="metrics-panel-heading">
        <Typography component="h2" variant="subtitle1" fontWeight={700}>Filtrar resultados</Typography>
        <Typography variant="caption" color="text.secondary">Los filtros de fecha se aplican al historial.</Typography>
      </Box>
      <Box className="metrics-filters">
        <FilterSelect label="Período" value={draft.range} options={[
          { value: 'today', label: 'Hoy' }, { value: '7d', label: '7 días' }, { value: '30d', label: '30 días' },
          { value: '90d', label: '90 días' }, { value: 'custom', label: 'Personalizado' },
        ]} onChange={range => onChange({ range, ...(range === 'custom' && !draft.dateFrom && !draft.dateTo ? defaultCustomDates() : {}) })} />
        {draft.range === 'custom' && <>
          <TextField className="metrics-filter" label="Desde" type="date" size="small" value={draft.dateFrom}
            slotProps={{ inputLabel: { shrink: true }, htmlInput: { 'aria-describedby': error ? 'metrics-date-error' : undefined } }}
            onChange={event => onChange({ dateFrom: event.target.value })} error={Boolean(error)} />
          <TextField className="metrics-filter" label="Hasta" type="date" size="small" value={draft.dateTo}
            slotProps={{ inputLabel: { shrink: true }, htmlInput: { 'aria-describedby': error ? 'metrics-date-error' : undefined } }}
            onChange={event => onChange({ dateTo: event.target.value })} error={Boolean(error)} />
        </>}
        <Button className="metrics-advanced-toggle" aria-expanded={advancedOpen} aria-controls={advancedId}
          onClick={() => setAdvancedOpen(value => !value)}>{advancedOpen ? 'Ocultar filtros' : 'Más filtros'}{advancedCount ? ` (${advancedCount})` : ''}</Button>
        <Box id={advancedId} className="metrics-advanced-controls" data-expanded={advancedOpen}>
        {admin && <FilterSelect label="Usuario" value={draft.selectedUser}
          options={[{ value: '', label: 'Todos los usuarios' }, ...users.map(user => ({ value: user.id, label: user.username }))]}
          onChange={selectedUser => onChange({ selectedUser })} disabled={usersFailed} helper={usersFailed ? 'No se pudo cargar la lista.' : undefined} />}
        <FilterSelect label="Nube de origen" value={draft.sourceProvider} options={cloudOptions}
          onChange={sourceProvider => onChange({ sourceProvider, sourceConnection: '' })} />
        <FilterSelect label="Nube de destino" value={draft.destinationProvider} options={cloudOptions}
          onChange={destinationProvider => onChange({ destinationProvider, destinationConnection: '' })} />
        <FilterSelect label="Conexión de origen" value={draft.sourceConnection} options={connectionOptions(draft.sourceProvider)}
          onChange={sourceConnection => onChange({ sourceConnection })} />
        <FilterSelect label="Conexión de destino" value={draft.destinationConnection} options={connectionOptions(draft.destinationProvider)}
          onChange={destinationConnection => onChange({ destinationConnection })} />
        </Box>
      </Box>
      <Box className="metrics-filter-actions">
        <Box>
          {error ? <Typography id="metrics-date-error" color="error" variant="body2" role="alert">{error}</Typography>
            : <Typography variant="body2" color="text.secondary">{dirty ? 'Cambios sin aplicar. Los resultados mantienen los filtros anteriores.' : 'Máximo 366 días. Selecciona los filtros y aplícalos juntos.'}</Typography>}
          {usersFailed && <Button size="small" onClick={retryUsers}>Reintentar usuarios</Button>}
        </Box>
        <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
          <Button onClick={onReset}>Restablecer</Button>
          <Button type="submit" variant="contained" disabled={!dirty || Boolean(error)}>Aplicar filtros</Button>
        </Stack>
      </Box>
    </CardContent>
  </Card>;
}

export default function MetricsDashboard({ user, connections }: { user: MetricsUser; connections: Connection[] }) {
  const [draft, setDraft] = useState<MetricsFilters>({ ...defaultFilters });
  const [applied, setApplied] = useState<MetricsFilters>({ ...defaultFilters });
  const [autoRefresh, setAutoRefresh] = useState(true);
  const timezone = useMemo(() => Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC', []);
  const scopedFilters = { ...applied, selectedUser: user.role === 'ADMIN' ? applied.selectedUser : '' };
  const queryString = metricsQuery(scopedFilters, timezone);
  const dirty = metricsQuery(draft, timezone) !== metricsQuery(applied, timezone);
  const usersQuery = useQuery({
    queryKey: ['metrics-users', user.id],
    queryFn: ({ signal }) => apiRequest<{ items: Array<{ id: string; username: string }> }>('/api/v1/users', { signal }),
    enabled: user.role === 'ADMIN', staleTime: 60_000,
  });
  const dashboardQuery = useQuery({
    queryKey: ['metrics-dashboard', user.id, user.role, queryString],
    queryFn: ({ signal }) => apiRequest<DashboardResponse>(`/api/v1/metrics/dashboard?${queryString}`, { signal }),
    refetchInterval: autoRefresh ? 30_000 : false,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: autoRefresh,
    retry: 1,
  });
  const data = dashboardQuery.data;
  const stale = Boolean(data && (dashboardQuery.isError || dashboardQuery.fetchStatus === 'paused'));
  const refreshing = dashboardQuery.isFetching;
  const reset = () => { setDraft({ ...defaultFilters }); setApplied({ ...defaultFilters }); };

  return <Stack className="metrics-dashboard" spacing={2.2}>
    <Box component="header" className="metrics-header">
      <Box>
        <Typography variant="overline" color="primary" fontWeight={700}>TRANSFERENCIAS Y OPERACIÓN</Typography>
        <Typography component="h1" variant="h4" fontWeight={750}>Métricas de la plataforma</Typography>
        <Typography color="text.secondary" sx={{ mt: 0.7 }}>Actividad histórica y estado actual de tus transferencias.</Typography>
      </Box>
      <Box className="metrics-header-actions">
        <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
          <Button size="small" variant="outlined" aria-pressed={!autoRefresh} onClick={() => setAutoRefresh(value => !value)}>
            {autoRefresh ? 'Pausar actualización' : 'Activar actualización'}
          </Button>
          <Button aria-label="Actualizar métricas" variant="contained" disabled={refreshing || dashboardQuery.fetchStatus === 'paused'}
            onClick={() => void dashboardQuery.refetch()} startIcon={<Box component="span" className="metrics-refresh-icon">{refreshing ? <CircularProgress size={15} color="inherit" /> : '↻'}</Box>}>
            {refreshing ? 'Actualizando…' : 'Actualizar'}
          </Button>
        </Stack>
        <Typography variant="caption" color="text.secondary" className="metrics-update-status" role="status" aria-live="polite">
          {dashboardQuery.fetchStatus === 'paused' ? 'Sin conexión · actualización pendiente'
            : refreshing ? 'Consultando métricas…'
              : data ? `Última lectura: ${timeLabel(data.generated_at)} · ${autoRefresh ? 'automática cada 30 s con la pestaña visible' : 'actualización automática pausada'}`
                : 'Esperando datos de la plataforma'}
        </Typography>
      </Box>
    </Box>

    <Filters draft={draft} onChange={patch => setDraft(current => ({ ...current, ...patch }))}
      onApply={() => setApplied({ ...draft })} onReset={reset} dirty={dirty} connections={connections}
      users={usersQuery.data?.items ?? []} admin={user.role === 'ADMIN'} usersFailed={usersQuery.isError}
      retryUsers={() => void usersQuery.refetch()} />
    <Box className="metrics-refresh-track" aria-hidden="true"><Box className="metrics-refresh-line" data-active={refreshing} /></Box>

    {data && stale && <Alert severity="warning" action={<Button color="inherit" disabled={refreshing} onClick={() => void dashboardQuery.refetch()}>Reintentar</Button>}>
      No se pudo actualizar. Se conservan los datos de las {timeLabel(data.generated_at)}; la salud y las colas no están verificadas ahora.
    </Alert>}
    {!data && dashboardQuery.isError ? <Alert severity="error" action={<Button color="inherit" onClick={() => void dashboardQuery.refetch()}>Reintentar</Button>}>
      No se pudieron cargar las métricas.
    </Alert> : !data ? <LoadingDashboard /> : <DashboardContent data={data} admin={user.role === 'ADMIN'} stale={stale} paused={!autoRefresh} reset={reset} />}
  </Stack>;
}

function LoadingDashboard() {
  return <Stack aria-label="Cargando métricas" aria-busy="true" spacing={2}>
    <Box className="metrics-kpi-grid">{[0, 1, 2, 3].map(key => <Card className="metrics-kpi" key={key} elevation={0}><CardContent>
      <Skeleton animation={false} width="70%" /><Skeleton animation={false} height={52} width="50%" /><Skeleton animation={false} />
    </CardContent></Card>)}</Box>
    <Box className="metrics-chart-grid">{[0, 1].map(key => <Card className="metrics-panel" key={key} elevation={0}><CardContent>
      <Skeleton animation={false} width="55%" height={36} /><Skeleton animation={false} height={290} variant="rounded" sx={{ mt: 2 }} />
    </CardContent></Card>)}</Box>
  </Stack>;
}

function DashboardContent({ data, admin, stale, paused, reset }: { data: DashboardResponse; admin: boolean; stale: boolean; paused: boolean; reset: () => void }) {
  const files = data.summary.files;
  const transfers = data.summary.transfers;
  const successDenominator = transfers.completed_cleanly + transfers.completed_with_warnings + transfers.failed;
  const rangeLabel = `${dayLabel(data.filters.from, true)} – ${dayLabel(data.filters.to, true)}`;
  return <>
    <Box className="metrics-section-label"><Typography component="h2" variant="subtitle1" fontWeight={700}>Resultados del período</Typography>
      <Typography variant="body2" color="text.secondary">{rangeLabel} · {data.filters.timezone}</Typography></Box>
    {data.coverage.manifestless_transfers > 0 && <Alert severity="info">{count(data.coverage.manifestless_transfers)} transferencias no tienen detalle por archivo. Los indicadores de archivos cubren {count(data.coverage.with_manifest)} de {count(data.coverage.transfers)} transferencias.</Alert>}
    {data.coverage.copied_files_without_date > 0 && <Alert severity="info">{count(data.coverage.copied_files_without_date)} archivos históricos no tienen fecha exacta de copia. Se incluyen en los totales, pero no en la serie diaria.</Alert>}
    {transfers.total === 0 && <Alert severity="info" action={<Button color="inherit" onClick={reset}>Restablecer filtros</Button>}>No hay transferencias creadas en este período con los filtros aplicados. La actividad de archivos puede incluir trabajos anteriores.</Alert>}
    <Box className="metrics-kpi-grid">
      <MetricCard label="Transferencias" value={count(transfers.total)} detail={`${count(transfers.completed_cleanly + transfers.completed_with_warnings)} completadas · ${count(transfers.failed)} fallidas`} tint="#2864dc" />
      <MetricCard label="Archivos copiados" value={count(files.copied)} detail={`${bytes(files.copied_bytes)} con tamaño conocido`} tint="#0f8b82" />
      <MetricCard label="Tasa de éxito" value={successDenominator ? `${number(transfers.success_rate)}%` : '—'} detail={successDenominator ? `${count(transfers.cancelled)} canceladas (excluidas) · ${count(transfers.additional_attempts)} reintentos` : 'Sin transferencias terminadas para calcular la tasa'} tint="#7756c9" />
      <MetricCard label="Transferencias con advertencias" value={count(transfers.completed_with_warnings)} detail={`${count(files.omitted)} archivos omitidos · ${count(files.renamed)} renombrados`} tint="#b97816" />
    </Box>
    <Box className="metrics-chart-grid">
      <ActivityPanel data={data} />
      <ProviderPanel data={data} />
    </Box>
    <Box className="metrics-chart-grid">
      <PerformancePanel data={data} />
      <ReliabilityPanel data={data} />
    </Box>
    <RoutesPanel data={data} />
    <Box className="metrics-section-label"><Typography component="h2" variant="subtitle1" fontWeight={700}>Estado actual</Typography>
      <Typography variant="body2" color="text.secondary">{stale || paused ? 'Última lectura sin verificar' : 'Lectura'}: {timeLabel(data.generated_at)} · independiente del período histórico</Typography></Box>
    <Panel title="Cola y tareas" subtitle="Conserva los filtros de nube, conexión y usuario. La fecha no limita esta lectura.">
      <Box className="metrics-stat-grid">
        <ReliabilityRow label="En espera" value={data.scheduling.waiting} /><ReliabilityRow label="Pausando o pausadas" value={data.scheduling.paused} />
        <ReliabilityRow label={stale || paused ? 'Activas en la última lectura' : 'Activas'} value={data.live.active} />
        <ReliabilityRow label="Velocidad reportada" value={`${bytes(data.live.reported_speed_bps)}/s`} />
      </Box>
      {data.scheduling.by_wait_reason.length > 0 && <Box className="metrics-inline-list"><Typography variant="subtitle2">Motivos de espera</Typography>{data.scheduling.by_wait_reason.map(reason => <Chip key={reason.reason} size="small" label={`${waitReasonLabel(reason.reason)} · ${count(reason.transfers)}`} title={reason.reason} />)}</Box>}
    </Panel>
    {admin && data.system && <SystemPanel system={data.system} generatedAt={data.generated_at} stale={stale} paused={paused} />}
  </>;
}

function ActivityPanel({ data }: { data: DashboardResponse }) {
  const days = useMemo(() => dailyActivity(data), [data]);
  const hasActivity = data.activity.some(day => day.transfers_created > 0 || day.copied > 0);
  return <Panel title="Actividad por día" subtitle="Transferencias según su fecha de creación; archivos según su fecha efectiva de copia. Los días sin actividad aparecen en cero.">
    {hasActivity ? <>
      <LineChart aria-label="Actividad diaria de transferencias y archivos" skipAnimation height={290}
        xAxis={[{ scaleType: 'point', height: 44, data: days.map(day => day.date), valueFormatter: (value: string, context) => dayLabel(value, context.location === 'tooltip'), tickLabelMinGap: 25 }]}
        yAxis={[{ min: 0, tickMinStep: 1 }]}
        series={[
          { data: days.map(day => day.transfers_created), label: 'Transferencias', color: '#2864dc', curve: 'linear', showMark: days.length < 10 },
          { data: days.map(day => day.copied), label: 'Archivos copiados', color: '#0f8b82', curve: 'linear', showMark: days.length < 10 },
        ]} margin={{ left: 8, right: 16, top: 26, bottom: 20 }} />
      <DataDisclosure label="Ver datos de actividad"><MetricsTable label="Actividad diaria"><thead><tr><th scope="col">Fecha</th><th scope="col">Transferencias</th><th scope="col">Archivos copiados</th><th scope="col">Volumen</th></tr></thead><tbody>{days.map(day => <tr key={day.date}><td>{dayLabel(day.date, true)}</td><td>{count(day.transfers_created)}</td><td>{count(day.copied)}</td><td>{bytes(day.copied_bytes)}</td></tr>)}</tbody></MetricsTable></DataDisclosure>
    </> : <EmptyState text="No hay actividad registrada en este período." />}
  </Panel>;
}

function ProviderPanel({ data }: { data: DashboardResponse }) {
  const [unit, setUnit] = useState('files');
  const providers = [...new Set([...data.providers.source.map(item => item.provider_type), ...data.providers.destination.map(item => item.provider_type)])];
  const sources = new Map(data.providers.source.map(item => [item.provider_type, item.files]));
  const destinations = new Map(data.providers.destination.map(item => [item.provider_type, item.files]));
  const format = unit === 'bytes' ? bytes : count;
  const value = (files: { copied: number; copied_bytes: number } | undefined) => (unit === 'bytes' ? files?.copied_bytes : files?.copied) ?? 0;
  return <Panel title={unit === 'bytes' ? 'Volumen por proveedor' : 'Archivos por proveedor'} subtitle="Un archivo figura en su origen y en su destino. Las dos series no se suman.">
    <ToggleButtonGroup className="metrics-unit-control" size="small" exclusive value={unit} aria-label="Unidad del gráfico de proveedores" onChange={(_event, value: string | null) => { if (value) setUnit(value); }}>
      <ToggleButton value="files">Archivos</ToggleButton><ToggleButton value="bytes">Volumen</ToggleButton>
    </ToggleButtonGroup>
    {providers.some(provider => value(sources.get(provider)) > 0 || value(destinations.get(provider)) > 0) ? <>
      <BarChart aria-label="Copias por proveedor de origen y destino" skipAnimation height={Math.max(250, providers.length * 65)} layout="horizontal"
        yAxis={[{ scaleType: 'band', data: providers.map(provider => providerLabel[provider] ?? provider), width: 106 }]}
        xAxis={[{ min: 0, height: 44, ...(unit === 'files' ? { tickMinStep: 1 } : {}), valueFormatter: format }]}
        series={[
          { data: providers.map(provider => value(sources.get(provider))), label: 'Origen', color: '#2864dc', valueFormatter: value => value === null ? '—' : format(value) },
          { data: providers.map(provider => value(destinations.get(provider))), label: 'Destino', color: '#0f8b82', valueFormatter: value => value === null ? '—' : format(value) },
        ]} margin={{ left: 0, right: 16, top: 22, bottom: 20 }} />
      <DataDisclosure label="Ver datos por proveedor"><MetricsTable label="Archivos y volumen por proveedor"><thead><tr><th scope="col">Proveedor</th><th scope="col">Archivos origen</th><th scope="col">Archivos destino</th><th scope="col">Volumen origen</th><th scope="col">Volumen destino</th></tr></thead><tbody>{providers.map(provider => <tr key={provider}><td>{providerLabel[provider] ?? provider}</td><td>{count(sources.get(provider)?.copied ?? 0)}</td><td>{count(destinations.get(provider)?.copied ?? 0)}</td><td>{bytes(sources.get(provider)?.copied_bytes ?? 0)}</td><td>{bytes(destinations.get(provider)?.copied_bytes ?? 0)}</td></tr>)}</tbody></MetricsTable></DataDisclosure>
    </> : <EmptyState text={unit === 'bytes' ? 'No hay volumen registrado con tamaño conocido.' : 'No hay archivos copiados para estos proveedores.'} />}
  </Panel>;
}

function PerformancePanel({ data }: { data: DashboardResponse }) {
  return <Panel title="Rendimiento por etapa" subtitle="La mediana representa el tiempo habitual. P95: el 95% de las muestras tarda ese tiempo o menos. Las mediciones comienzan con nuevas ejecuciones.">
    {data.performance.stages.length ? <MetricsTable label="Rendimiento de etapas"><thead><tr><th scope="col">Etapa</th><th scope="col">Proveedor</th><th scope="col">Correctas / total</th><th scope="col">Mediana</th><th scope="col">P95</th><th scope="col">Velocidad efectiva</th></tr></thead><tbody>{data.performance.stages.map(stage => <tr key={`${stage.stage}-${stage.provider_type}-${stage.connection_role}`}>
      <td>{stageLabel[stage.stage] ?? stage.stage}</td><td>{providerLabel[stage.provider_type ?? ''] ?? stage.provider_type ?? 'Local'}</td>
      <td>{count(stage.successful_samples)} / {count(stage.samples)}<Typography component="span" className="metrics-cell-note">{count(stage.failed_samples)} fallidas · {count(stage.incomplete_samples)} sin cierre</Typography></td>
      <td>{duration(stage.median_ms)}</td><td>{duration(stage.p95_ms)}</td><td>{stage.effective_bytes_per_second !== null ? `${bytes(stage.effective_bytes_per_second)}/s` : '—'}</td>
    </tr>)}</tbody></MetricsTable> : <EmptyState text="Aún no hay muestras detalladas. Aparecerán con nuevas ejecuciones." />}
    {data.performance.states.length > 0 && <DataDisclosure label="Ver tiempos por estado"><MetricsTable label="Tiempos por estado"><thead><tr><th scope="col">Estado</th><th scope="col">Muestras</th><th scope="col">Mediana</th><th scope="col">P95</th></tr></thead><tbody>{data.performance.states.map(state => <tr key={state.state}><td>{transferStateLabel(state.state)}</td><td>{count(state.samples)}</td><td>{duration(state.median_ms)}</td><td>{duration(state.p95_ms)}</td></tr>)}</tbody></MetricsTable></DataDisclosure>}
  </Panel>;
}

function ReliabilityPanel({ data }: { data: DashboardResponse }) {
  const distribution = data.reliability.size_distribution.filter(item => item.files > 0);
  const colors = ['#2864dc', '#0f8b82', '#7756c9', '#b97816', '#c95765', '#6d7b90'];
  return <Panel title="Resultado de los archivos" subtitle="Cada archivo se cuenta una vez por resultado. Los renombrados pueden estar entre los copiados.">
    <Box className="metrics-stat-grid">
      <ReliabilityRow label="Omitidos por decisión" value={data.summary.files.omitted} /><ReliabilityRow label="Fallidos" value={data.summary.files.failed} />
      <ReliabilityRow label="Pendientes" value={data.summary.files.pending} /><ReliabilityRow label="Saltados históricos" value={data.summary.files.skipped_historical} />
      <ReliabilityRow label="Copiados sin tamaño conocido" value={data.summary.files.copied_size_unknown} />
    </Box>
    <Typography component="h3" variant="subtitle2" sx={{ mt: 2 }}>Archivos copiados por tamaño</Typography>
    {distribution.length ? <Box className="metrics-size-grid">
      <PieChart aria-label="Distribución de archivos copiados por tamaño" skipAnimation hideLegend height={190}
        series={[{ innerRadius: 48, paddingAngle: 2, data: distribution.map((item, index) => ({ id: item.bucket, label: sizeLabel[item.bucket] ?? item.bucket, value: item.files, color: colors[index % colors.length] })), valueFormatter: item => `${count(item.value)} archivos` }]} />
      <Box component="ul" className="metrics-size-legend">{distribution.map((item, index) => <Box component="li" key={item.bucket}><Box component="span" aria-hidden="true" className="metrics-legend-dot" sx={{ bgcolor: colors[index % colors.length] }} /><Typography component="span" variant="body2">{sizeLabel[item.bucket] ?? item.bucket}</Typography><Typography component="span" variant="body2" className="metrics-number">{count(item.files)}</Typography></Box>)}</Box>
    </Box> : <EmptyState text="Sin archivos copiados para distribuir por tamaño." />}
    {data.reliability.errors.length > 0 && <Box className="metrics-inline-list"><Typography component="h3" variant="subtitle2">Códigos de error frecuentes</Typography>{data.reliability.errors.map(error => <Chip className="metrics-wrap-chip" key={error.error_code} size="small" color="error" variant="outlined" label={`${error.error_code} · ${count(error.files)}`} />)}</Box>}
    {data.reliability.extensions.length > 0 && <Box className="metrics-inline-list"><Typography component="h3" variant="subtitle2">Extensiones más frecuentes (hasta 6)</Typography>{data.reliability.extensions.map(item => <Chip className="metrics-wrap-chip" key={item.extension} size="small" variant="outlined" label={`${item.extension} · ${count(item.files)}`} />)}</Box>}
  </Panel>;
}

function RoutesPanel({ data }: { data: DashboardResponse }) {
  const pairs = [...data.providers.pairs].sort((a, b) => b.files.copied - a.files.copied || b.transfers - a.transfers);
  const rows = (items: typeof pairs) => items.map(pair => <Box key={pair.label} className="metrics-rank-row"><Box sx={{ minWidth: 0 }}>
    <Typography fontWeight={650}>{pair.label.split(' → ').map(part => providerLabel[part] ?? part).join(' → ')}</Typography>
    <Typography variant="body2" color="text.secondary">{count(pair.transfers)} transferencias · {bytes(pair.files.copied_bytes)}</Typography>
  </Box><Chip size="small" label={`${count(pair.files.copied)} archivos`} /></Box>);
  return <Panel title="Rutas con más archivos copiados" subtitle="Combinaciones entre nube de origen y destino; ordenadas por archivos copiados.">
    {pairs.length ? <><Box className="metrics-rank-list">{rows(pairs.slice(0, 6))}</Box>
      {pairs.length > 6 && <DataDisclosure label={`Ver las ${count(pairs.length - 6)} rutas restantes`}>{rows(pairs.slice(6))}</DataDisclosure>}
    </> : <EmptyState text="No hay rutas registradas con estos filtros." />}
  </Panel>;
}

function SystemPanel({ system, generatedAt, stale, paused }: { system: NonNullable<DashboardResponse['system']>; generatedAt: string; stale: boolean; paused: boolean }) {
  const queueFailure = system.queues.some(queue => !queue.consumer_ready);
  const status = stale ? 'STALE' : paused ? 'REFRESH_PAUSED' : system.status === 'SIMULATED' ? 'SIMULATED' : queueFailure ? 'DEGRADED' : system.status;
  const queueLabels: Record<string, string> = { transfers: 'Transferencias', control: 'Control', evaluations: 'Evaluaciones', notifications: 'Notificaciones' };
  const serviceLabels: Record<string, string> = { database: 'Base de datos', redis: 'Redis', workers: 'Workers de copia y control', beat: 'Programador' };
  return <Panel title="Salud de la plataforma" subtitle={`Última comprobación: ${timeLabel(generatedAt)} · ${system.environment === 'production' ? 'Producción' : 'Desarrollo'}`} action={<StatusChip status={status} />}>
    {system.status === 'SIMULATED' && <Alert severity="info" sx={{ mt: 2 }}>Comprobaciones simuladas fuera de producción.</Alert>}
    <Box className="metrics-system-grid">{Object.entries(system.services).map(([service, value]) => <Box className="metrics-system-service" key={service}>
      <Typography variant="body2">{serviceLabels[service] ?? service}</Typography>
      {stale || paused ? <Chip size="small" label="Sin verificar" title={`Último estado: ${statusPresentation(value).label}`} /> : <StatusChip status={value} />}
    </Box>)}</Box>
    {system.queues.length > 0 && <MetricsTable label="Consumidores y carga de las colas"><thead><tr><th scope="col">Cola</th><th scope="col">Consumidor</th><th scope="col">Pendientes</th></tr></thead><tbody>{system.queues.map(queue => <tr key={queue.name}><td>{queueLabels[queue.name] ?? queue.name}</td><td>{stale || paused ? 'Sin verificar' : <Chip size="small" label={queue.consumer_ready ? 'Disponible' : 'Sin consumidor'} color={queue.consumer_ready ? 'success' : 'error'} variant="outlined" />}</td><td>{queue.pending === null ? '—' : count(queue.pending)}</td></tr>)}</tbody></MetricsTable>}
    <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 1.5 }}>Esta salud es global e independiente de los filtros. Se comprueban la base de datos, Redis, los consumidores y el pulso del programador. El frontend y el proxy requieren monitoreo externo.</Typography>
    {system.connections.length > 0 && <DataDisclosure label="Ver último estado guardado de las conexiones">
      <Typography variant="body2" color="text.secondary">Son resultados de pruebas guardadas; no se vuelve a conectar con las nubes al refrescar este tablero.</Typography>
      <MetricsTable label="Estado guardado de las conexiones"><thead><tr><th scope="col">Proveedor</th><th scope="col">Conexión</th><th scope="col">Última prueba</th><th scope="col">Total</th></tr></thead><tbody>{system.connections.map(connection => <tr key={`${connection.provider_type}-${connection.status}-${connection.last_test_status}`}><td>{providerLabel[connection.provider_type] ?? connection.provider_type}</td><td>{statusPresentation(connection.status).label}</td><td>{connection.last_test_status ? statusPresentation(connection.last_test_status).label : 'Sin prueba'}</td><td>{count(connection.count)}</td></tr>)}</tbody></MetricsTable>
    </DataDisclosure>}
    <Box className="metrics-system-tasks">{(['notifications', 'evaluations'] as const).map(key => <Box className="metrics-inline-list" key={key}>
      <Typography component="h3" variant="subtitle2">{key === 'notifications' ? 'Notificaciones' : 'Evaluaciones'}</Typography>
      {Object.entries(system[key].by_status ?? {}).length ? Object.entries(system[key].by_status ?? {}).map(([status, amount]) => <Chip key={status} size="small" label={`${taskStatusLabel(status)} · ${count(amount)}`} variant="outlined" color={status === 'FAILED' ? 'error' : 'default'} />) : <Typography variant="body2" color="text.secondary">Sin tareas registradas.</Typography>}
    </Box>)}</Box>
  </Panel>;
}

function StatusChip({ status }: { status: string }) {
  const presentation = statusPresentation(status);
  return <Chip size="small" label={presentation.label} color={presentation.color} />;
}
function EmptyState({ text }: { text: string }) {
  return <Box className="metrics-empty"><Typography variant="body2" color="text.secondary">{text}</Typography></Box>;
}
function ReliabilityRow({ label, value }: { label: string; value: number | string }) {
  return <Box className="metrics-reliability-row"><Typography variant="body2" color="text.secondary">{label}</Typography><Typography className="metrics-number" variant="body2" fontWeight={700}>{typeof value === 'number' ? count(value) : value}</Typography></Box>;
}
function MetricsTable({ label, children }: { label: string; children: ReactNode }) {
  return <Box className="metrics-table-wrap" role="region" aria-label={label} tabIndex={0}><table className="metrics-table"><caption className="metrics-sr-only">{label}</caption>{children}</table></Box>;
}
function DataDisclosure({ label, children }: { label: string; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  return <Box component="details" className="metrics-disclosure" onToggle={event => setOpen(event.currentTarget.open)}><summary>{label}</summary>{open && children}</Box>;
}
