import { zodResolver } from '@hookform/resolvers/zod';
import {
  Alert,
  AppBar,
  Badge,
  Box,
  Button,
  Card,
  CardContent,
  Checkbox,
  Chip,
  CircularProgress,
  Container,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Divider,
  FormHelperText,
  FormControl,
  FormControlLabel,
  InputLabel,
  IconButton,
  LinearProgress,
  ListItemIcon,
  Menu,
  MenuItem,
  Paper,
  Popover,
  Select,
  Stack,
  SvgIcon,
  Tab,
  Tabs,
  TextField,
  Toolbar,
  Typography,
  createTheme,
  ThemeProvider,
} from '@mui/material';
import {
  type InfiniteData,
  QueryClient,
  QueryClientProvider,
  useInfiniteQuery,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query';
import { useEffect, useMemo, useState } from 'react';
import { Controller, useForm } from 'react-hook-form';
import { BrowserRouter, Navigate, Route, Routes, useNavigate } from 'react-router-dom';
import { z } from 'zod';
import { apiRequest, ApiError } from './api';
import DocumentationSection from './DocumentationSection';
import './styles.css';

type User = { id: string; username: string; email: string; role: 'ADMIN' | 'USER'; must_change_password?: boolean };
type Connection = {
  id: string;
  name: string;
  provider_type: string;
  adapter_type?: 'RCLONE' | 'SEAFILE_API';
  status: string;
  credentials_configured: boolean;
  is_global?: boolean;
  last_tested_at?: string | null;
  last_test_status?: string | null;
};
type ManagedUser = {
  id: string;
  username: string;
  email: string;
  role: 'ADMIN' | 'USER';
  is_active: boolean;
  must_change_password: boolean;
};
type StorageItem = {
  name: string;
  path: string;
  is_dir: boolean;
  size_bytes: number | null;
  modified_at: string | null;
  mime_type: string | null;
  provider_type: string;
};
type ItemPage = { items: StorageItem[]; next_cursor: string | null; total_count: number };
type Transfer = {
  id: string;
  public_id: string;
  status: string;
  source_connection_id?: string;
  destination_connection_id: string;
  source_paths?: string[];
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
  wait_reason?: string | null;
  can_edit_schedule?: boolean;
  manifest_summary?: { copied: number; failed: number; skipped: number; pending: number };
};
type TransferListPage = { items: Transfer[]; total_count: number };
type TransferManifestItem = {
  id: string;
  source_path: string;
  destination_path: string;
  is_dir: boolean;
  size_bytes: number | null;
  modified_at: string | null;
  scan_complete: boolean;
  status: 'DIRECTORY' | 'PENDING' | 'COPIED' | 'SKIPPED' | 'FAILED';
  error_code: string | null;
  verified_by: string | null;
};
type TransferManifestPage = {
  items: TransferManifestItem[];
  next_cursor: string | null;
  counts: { total: number; pending: number; copied: number; skipped: number; failed: number };
};
const terminalTransferStatuses = ['COMPLETED', 'COMPLETED_WITH_WARNINGS', 'FAILED', 'CANCELLED'];
const successfulTransferStatuses = ['COMPLETED', 'COMPLETED_WITH_WARNINGS'];
const backgroundTransferStatuses = ['SCHEDULED', 'QUEUED', 'VALIDATING', 'RUNNING', 'RETRYING', 'CANCELLING'];
const streamingTransferStatuses = ['VALIDATING', 'RUNNING', 'RETRYING', 'CANCELLING'];
const terminalProgressDisplayMs = 3_000;

function isProgrammedBackgroundTransfer(transfer: Transfer): boolean {
  return backgroundTransferStatuses.includes(transfer.status) && Boolean(
    transfer.scheduled_at
    || transfer.wait_reason
    || transfer.execution_mode === 'EXCLUSIVE'
    || transfer.depends_on_transfer_id,
  );
}
type LoginForm = { username_or_email: string; password: string };
type ConnectionForm = {
  name: string;
  provider_type: 'MOCK' | 'MEGA' | 'GOOGLE_DRIVE' | 'SEAFILE' | 'SMB';
  seafile_adapter_type: 'RCLONE' | 'SEAFILE_API';
  is_global: boolean;
  mega_username: string;
  mega_password: string;
  drive_client_id: string;
  drive_client_secret: string;
  drive_team_drive: string;
  drive_root_folder_id: string;
  seafile_url: string;
  seafile_username: string;
  seafile_password: string;
  seafile_library: string;
  seafile_library_password: string;
  smb_host: string;
  smb_port: string;
  smb_share: string;
  smb_base_path: string;
  smb_username: string;
  smb_password: string;
  smb_domain: string;
};
type PasswordForm = { current_password: string; new_password: string };
type SmtpSettings = {
  host: string;
  port: number;
  use_tls: boolean;
  from_address: string;
  credentials_configured: boolean;
};
type SmtpFormValues = SmtpSettings & { username: string; password: string };

const theme = createTheme({
  palette: {
    mode: 'light',
    primary: { main: '#2864dc' },
    secondary: { main: '#11a8a0' },
    background: { default: '#f4f7fb', paper: '#ffffff' },
    text: { primary: '#18283f', secondary: '#68788e' },
  },
  shape: { borderRadius: 12 },
  typography: {
    fontFamily: 'Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
    button: { textTransform: 'none', fontWeight: 650 },
  },
});

const loginSchema = z.object({ username_or_email: z.string().min(1), password: z.string().min(1) });
const smtpFormSchema = z.object({
  host: z.string().trim().min(1, 'Ingresa el servidor SMTP.').max(255),
  port: z.number().int('Ingresa un puerto válido.').min(1, 'El puerto debe ser mayor que 0.').max(65535, 'El puerto no puede superar 65535.'),
  use_tls: z.boolean(),
  from_address: z.string().trim().email('Ingresa un correo remitente válido.'),
  credentials_configured: z.boolean(),
  username: z.string().max(255),
  password: z.string().max(1024),
}).superRefine((values, context) => {
  const hasUsername = Boolean(values.username.trim());
  const hasPassword = Boolean(values.password);
  if (hasUsername !== hasPassword) {
    context.addIssue({
      code: 'custom',
      path: [hasUsername ? 'password' : 'username'],
      message: 'Completa usuario y contraseña juntos.',
    });
  }
});

function InterfaceIcon({ name }: { name: 'folder' | 'file' | 'copy' | 'transfer' | 'refresh' | 'archive' | 'settings' | 'key' | 'logout' | 'clock' }) {
  const paths = {
    folder: <path d="M3 6.5A1.5 1.5 0 0 1 4.5 5h5l2 2h8A1.5 1.5 0 0 1 21 8.5v9a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 17.5z" />,
    file: <path d="M7 3.75h7l4 4v12.5H7a2 2 0 0 1-2-2v-12.5a2 2 0 0 1 2-2Zm7 0v4h4M8.5 12h7M8.5 15h7" />,
    copy: <path d="M8 8V4.75A1.75 1.75 0 0 1 9.75 3h9.5A1.75 1.75 0 0 1 21 4.75v9.5A1.75 1.75 0 0 1 19.25 16H16M4.75 8H14A2 2 0 0 1 16 10v9.25A1.75 1.75 0 0 1 14.25 21h-9.5A1.75 1.75 0 0 1 3 19.25v-9.5A1.75 1.75 0 0 1 4.75 8Z" />,
    transfer: <><path d="M3.5 6.5h5v11h-5zM15.5 6.5h5v11h-5z" /><path d="M9 12h6m-2.5-2.5L15 12l-2.5 2.5" /></>,
    refresh: <path d="M20 7v5h-5M4 17v-5h5m10.25-3A7 7 0 0 0 6.1 6.1L4 8m16 8-2.1 1.9A7 7 0 0 1 4.75 15" />,
    archive: <path d="M4 7h16v13H4zM3 4h18v3H3zm5 7h8" />,
    settings: <><path d="M12 8.5a3.5 3.5 0 1 0 0 7 3.5 3.5 0 0 0 0-7Z" /><path d="m19.4 15 .1.1 1.2.9-1.5 2.6-1.4-.6a8 8 0 0 1-1.8 1l-.2 1.5h-3l-.3-1.5a8 8 0 0 1-1.8-1l-1.4.6-1.5-2.6 1.2-.9a7 7 0 0 1 0-2.1l-1.2-.9 1.5-2.6 1.4.6a8 8 0 0 1 1.8-1l.3-1.5h3l.2 1.5a8 8 0 0 1 1.8 1l1.4-.6 1.5 2.6-1.2.9a7 7 0 0 1 0 2Z" transform="translate(-1 -1) scale(.98)" /></>,
    key: <path d="M14.5 9a5 5 0 1 1-1.47-3.54L21 13v3h-3v3h-3v-3h-3l-2.1-2.1M7.5 9h.01" />,
    logout: <path d="M10 5H5v14h5m4-3 4-4-4-4m4 4H9" />,
    clock: <><circle cx="12" cy="12" r="8.5" /><path d="M12 7v5l3.5 2" /></>,
  };
  return <SvgIcon fontSize="small" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">{paths[name]}</g></SvgIcon>;
}

function CloudNetworkMark() {
  return (
    <svg className="cloud-network-mark" viewBox="0 0 48 48" role="img" aria-label="Cloud Transfer Manager" focusable="false">
      <path
        className="cloud-network-outline"
        d="M14.7 36h18a8.25 8.25 0 0 0 .45-16.49A11.6 11.6 0 0 0 10.8 17.6 8.5 8.5 0 0 0 14.7 36Z"
      />
      <path className="cloud-network-links" d="m15.5 27.5 7-7 9 7-9 5.5-7-5.5Zm7-7v12.5m-7-5.5h16" />
      <circle className="cloud-network-node" cx="15.5" cy="27.5" r="2.7" />
      <circle className="cloud-network-node" cx="22.5" cy="20.5" r="2.7" />
      <circle className="cloud-network-node cloud-network-node-accent" cx="31.5" cy="27.5" r="2.7" />
      <circle className="cloud-network-node" cx="22.5" cy="33" r="2.7" />
    </svg>
  );
}

const providerAssets: Record<string, { label: string; src: string }> = {
  MEGA: { label: 'MEGA', src: '/brand/mega.svg' },
  GOOGLE_DRIVE: { label: 'Google Drive', src: '/brand/google-drive.png' },
  SEAFILE: { label: 'Seafile', src: '/brand/seafile.svg' },
};

function ProviderLogo({ provider, decorative = true }: { provider: string; decorative?: boolean }) {
  const asset = providerAssets[provider];
  if (!asset) return <span className="provider-logo-fallback" aria-hidden="true">☁</span>;
  return (
    <img
      className={`provider-logo${decorative ? ' provider-logo-decorative' : ''}`}
      src={asset.src}
      alt={decorative ? '' : asset.label}
      aria-hidden={decorative || undefined}
    />
  );
}

function LoginPage({ onLogin }: { onLogin: (user: User) => void }) {
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const navigate = useNavigate();
  const { register, handleSubmit, formState: { errors } } = useForm<LoginForm>({
    resolver: zodResolver(loginSchema),
  });

  const submit = async (values: LoginForm) => {
    setError('');
    setBusy(true);
    try {
      await apiRequest<{ csrf_token: string }>('/api/v1/auth/csrf');
      const user = await apiRequest<User>('/api/v1/auth/login', {
        method: 'POST',
        body: JSON.stringify(values),
      });
      onLogin(user);
      navigate('/', { replace: true });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'No fue posible iniciar sesión.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <Box className="login-shell">
      <Box className="login-aside">
        <Stack direction="row" alignItems="center" spacing={1.5}>
          <Box className="brand-mark"><CloudNetworkMark /></Box>
          <Typography variant="h6" component="h1" fontWeight={750}>Cloud Transfer Manager</Typography>
        </Stack>
        <Typography variant="overline" className="eyebrow">TRANSFERENCIAS PRIVADAS</Typography>
        <Typography variant="h3" className="login-title">Todo tu cloud, en un mismo lugar.</Typography>
        <Typography className="login-copy">
          Explora tus conexiones de almacenamiento y mueve información con trazabilidad de principio a fin.
        </Typography>
        <Box className="login-visual">
          <Box className="cloud-node"><ProviderLogo provider="MEGA" decorative={false} /><span>MEGA</span></Box>
          <Box className="transfer-line"><span /><span /><span /></Box>
          <Box className="cloud-node drive-node"><ProviderLogo provider="GOOGLE_DRIVE" decorative={false} /><span>Google Drive</span></Box>
          <Box className="provider-strip" role="group" aria-label="Proveedores disponibles">
            <ProviderLogo provider="SEAFILE" decorative={false} />
            <Typography variant="caption">Seafile</Typography>
          </Box>
          <Box className="visual-caption">Transferencias seguras · Progreso en vivo</Box>
        </Box>
      </Box>
      <Paper elevation={0} className="login-card">
        <Typography variant="overline" color="primary.main">ACCESO INTERNO</Typography>
        <Typography variant="h4" className="login-card-title">Bienvenido</Typography>
        <Typography color="text.secondary" sx={{ mb: 3 }}>Inicia sesión para continuar a tu espacio de trabajo.</Typography>
        <Box component="form" onSubmit={handleSubmit(submit)} noValidate>
          <Stack spacing={2.2}>
            <TextField
              label="Usuario o correo"
              autoComplete="username"
              autoFocus
              error={Boolean(errors.username_or_email)}
              helperText={errors.username_or_email ? 'Ingresa tu usuario o correo.' : ' '}
              {...register('username_or_email')}
            />
            <TextField
              label="Contraseña"
              type="password"
              autoComplete="current-password"
              error={Boolean(errors.password)}
              helperText={errors.password ? 'Ingresa tu contraseña.' : ' '}
              {...register('password')}
            />
            {error && <Alert severity="error">{error}</Alert>}
            <Button type="submit" variant="contained" size="large" disabled={busy}>
              {busy ? <CircularProgress size={22} color="inherit" /> : 'Iniciar sesión'}
            </Button>
            <Typography variant="caption" color="text.secondary" textAlign="center">
              Acceso disponible únicamente dentro de la red privada.
            </Typography>
          </Stack>
        </Box>
      </Paper>
    </Box>
  );
}

const connectionSchema = z.object({
  name: z.string().trim().min(1, 'Ingresa un nombre.'),
  provider_type: z.enum(['MOCK', 'MEGA', 'GOOGLE_DRIVE', 'SEAFILE', 'SMB']),
  seafile_adapter_type: z.enum(['RCLONE', 'SEAFILE_API']),
  is_global: z.boolean(),
  mega_username: z.string(),
  mega_password: z.string(),
  drive_client_id: z.string(),
  drive_client_secret: z.string(),
  drive_team_drive: z.string(),
  drive_root_folder_id: z.string(),
  seafile_url: z.string(),
  seafile_username: z.string(),
  seafile_password: z.string(),
  seafile_library: z.string(),
  seafile_library_password: z.string(),
  smb_host: z.string(),
  smb_port: z.string(),
  smb_share: z.string(),
  smb_base_path: z.string(),
  smb_username: z.string(),
  smb_password: z.string(),
  smb_domain: z.string(),
}).superRefine((values, context) => {
  const required: Array<[keyof ConnectionForm, string]> = values.provider_type === 'MEGA'
    ? [['mega_username', values.mega_username], ['mega_password', values.mega_password]]
    : values.provider_type === 'GOOGLE_DRIVE'
      ? [['drive_client_id', values.drive_client_id], ['drive_client_secret', values.drive_client_secret]]
      : values.provider_type === 'SEAFILE'
        ? [
            ['seafile_url', values.seafile_url],
            ['seafile_username', values.seafile_username],
            ['seafile_password', values.seafile_password],
          ]
        : values.provider_type === 'SMB'
          ? [
              ['smb_host', values.smb_host],
              ['smb_share', values.smb_share],
              ['smb_username', values.smb_username],
              ['smb_password', values.smb_password],
            ]
        : [];
  required.forEach(([field, value]) => {
    if (!value) context.addIssue({ code: 'custom', path: [field], message: 'Este campo es obligatorio.' });
  });
});

const emptyConnectionForm: ConnectionForm = {
  name: '', provider_type: 'MEGA', seafile_adapter_type: 'RCLONE', is_global: true,
  mega_username: '', mega_password: '', drive_client_id: '', drive_client_secret: '',
  drive_team_drive: '', drive_root_folder_id: '', seafile_url: '', seafile_username: '',
  seafile_password: '', seafile_library: '', seafile_library_password: '',
  smb_host: '', smb_port: '445', smb_share: '', smb_base_path: '', smb_username: '',
  smb_password: '', smb_domain: '',
};

function ConnectionDialog({
  open,
  onClose,
  onCreated,
  initialProvider = 'MEGA',
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (connection: Connection) => void;
  initialProvider?: ConnectionForm['provider_type'];
}) {
  const formDefaults = { ...emptyConnectionForm, provider_type: initialProvider };
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const { register, control, handleSubmit, reset, watch, formState: { errors } } = useForm<ConnectionForm>({
    resolver: zodResolver(connectionSchema),
    defaultValues: formDefaults,
  });
  const provider = watch('provider_type');

  useEffect(() => {
    if (open) reset(formDefaults);
  }, [formDefaults.provider_type, open, reset]);

  const close = () => {
    reset(formDefaults);
    setError('');
    onClose();
  };

  const submit = async (values: ConnectionForm) => {
    setBusy(true);
    setError('');
    const credentials = values.provider_type === 'MEGA'
      ? { username: values.mega_username, password: values.mega_password }
      : values.provider_type === 'GOOGLE_DRIVE'
        ? {
            client_id: values.drive_client_id,
            client_secret: values.drive_client_secret,
            ...(values.drive_team_drive ? { team_drive: values.drive_team_drive } : {}),
            ...(values.drive_root_folder_id ? { root_folder_id: values.drive_root_folder_id } : {}),
          }
        : values.provider_type === 'SEAFILE'
          ? {
              url: values.seafile_url,
              username: values.seafile_username,
              password: values.seafile_password,
              ...(values.seafile_library.trim() ? { library: values.seafile_library.trim() } : {}),
              ...(values.seafile_library_password
                ? { library_password: values.seafile_library_password }
                : {}),
            }
          : values.provider_type === 'SMB'
            ? {
                host: values.smb_host,
                port: values.smb_port || '445',
                share: values.smb_share,
                ...(values.smb_base_path.trim() ? { base_path: values.smb_base_path.trim() } : {}),
                username: values.smb_username,
                password: values.smb_password,
                ...(values.smb_domain.trim() ? { domain: values.smb_domain.trim() } : {}),
              }
            : undefined;
    try {
      const connection = await apiRequest<Connection>('/api/v1/connections', {
        method: 'POST',
        body: JSON.stringify({
          name: values.name,
          provider_type: values.provider_type,
          ...(values.provider_type === 'SEAFILE'
            ? { adapter_type: values.seafile_adapter_type }
            : {}),
          is_global: values.is_global,
          ...(credentials ? { credentials } : {}),
        }),
      });
      if (values.provider_type === 'GOOGLE_DRIVE') {
        const authorization = await apiRequest<{ authorization_url: string }>(
          `/api/v1/connections/${connection.id}/google-oauth/start`,
          { method: 'POST' },
        );
        window.location.assign(authorization.authorization_url);
        return;
      }
      reset(formDefaults);
      onCreated(connection);
      onClose();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'No se pudo crear la conexión.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onClose={close} fullWidth maxWidth="sm" aria-labelledby="connection-dialog-title">
      <DialogTitle id="connection-dialog-title">
        Agregar conexión
        <Typography component="span" display="block" variant="body2" color="text.secondary" sx={{ mt: 0.5, fontWeight: 400, letterSpacing: 0 }}>
          Configura el acceso y define quién podrá usar este almacenamiento.
        </Typography>
      </DialogTitle>
      <Box component="form" onSubmit={handleSubmit(submit)} noValidate>
        <DialogContent>
          <Stack spacing={2}>
            <TextField
              label="Nombre de conexión"
              error={Boolean(errors.name)}
              helperText={errors.name?.message ?? ' '}
              {...register('name')}
            />
            <FormControl fullWidth>
              <InputLabel id="provider-label">Proveedor</InputLabel>
              <Controller
                name="provider_type"
                control={control}
                render={({ field }) => (
                  <Select {...field} labelId="provider-label" label="Proveedor">
                    <MenuItem value="MEGA"><Stack direction="row" alignItems="center" spacing={1}><ProviderLogo provider="MEGA" />MEGA</Stack></MenuItem>
                    <MenuItem value="GOOGLE_DRIVE"><Stack direction="row" alignItems="center" spacing={1}><ProviderLogo provider="GOOGLE_DRIVE" />Google Drive</Stack></MenuItem>
                    <MenuItem value="SEAFILE"><Stack direction="row" alignItems="center" spacing={1}><ProviderLogo provider="SEAFILE" />Seafile</Stack></MenuItem>
                    <MenuItem value="SMB">NAS privado (SMB)</MenuItem>
                    <MenuItem value="MOCK"><Stack direction="row" alignItems="center" spacing={1}><ProviderLogo provider="MOCK" />Simulado (pruebas)</Stack></MenuItem>
                  </Select>
                )}
              />
            </FormControl>
            {provider === 'MEGA' && (
              <>
                <TextField label="Usuario de MEGA" autoComplete="username" {...register('mega_username')} error={Boolean(errors.mega_username)} helperText={errors.mega_username?.message ?? ' '} />
                <TextField label="Contraseña de MEGA" type="password" autoComplete="new-password" {...register('mega_password')} error={Boolean(errors.mega_password)} helperText={errors.mega_password?.message ?? ' '} />
              </>
            )}
            {provider === 'GOOGLE_DRIVE' && (
              <>
                <Alert severity="info">Autorizarás acceso a Google Drive. Registra en Google Cloud el callback https://tu-dominio/api/v1/connections/google-oauth/callback. El código se intercambia por tokens en el servidor y los tokens no vuelven a la interfaz.</Alert>
                <TextField label="OAuth Client ID" {...register('drive_client_id')} error={Boolean(errors.drive_client_id)} helperText={errors.drive_client_id?.message ?? ' '} />
                <TextField label="OAuth Client Secret" type="password" autoComplete="new-password" {...register('drive_client_secret')} error={Boolean(errors.drive_client_secret)} helperText={errors.drive_client_secret?.message ?? ' '} />
                <TextField label="Shared Drive ID (opcional)" {...register('drive_team_drive')} />
                <TextField label="Carpeta raíz (opcional)" {...register('drive_root_folder_id')} />
              </>
            )}
            {provider === 'SEAFILE' && (
              <>
                <Alert severity="info">
                  Rclone es el adaptador predeterminado. La API solo se usará si la seleccionas aquí; un error nunca cambia el adaptador automáticamente.
                </Alert>
                <FormControl fullWidth>
                  <InputLabel id="seafile-adapter-label">Adaptador Seafile</InputLabel>
                  <Controller
                    name="seafile_adapter_type"
                    control={control}
                    render={({ field }) => (
                      <Select {...field} labelId="seafile-adapter-label" label="Adaptador Seafile">
                        <MenuItem value="RCLONE">Rclone (predeterminado)</MenuItem>
                        <MenuItem value="SEAFILE_API">API de Seafile (opcional)</MenuItem>
                      </Select>
                    )}
                  />
                </FormControl>
                <TextField
                  label="URL del servidor Seafile"
                  placeholder="https://seafile.example.com"
                  autoComplete="url"
                  error={Boolean(errors.seafile_url)}
                  helperText={errors.seafile_url?.message ?? ' '}
                  {...register('seafile_url')}
                />
                <TextField
                  label="Usuario de Seafile"
                  autoComplete="username"
                  error={Boolean(errors.seafile_username)}
                  helperText={errors.seafile_username?.message ?? ' '}
                  {...register('seafile_username')}
                />
                <TextField
                  label="Contraseña de Seafile"
                  type="password"
                  autoComplete="new-password"
                  error={Boolean(errors.seafile_password)}
                  helperText={errors.seafile_password?.message ?? ' '}
                  {...register('seafile_password')}
                />
                <TextField
                  label="Biblioteca (opcional)"
                  helperText="Déjala vacía para mostrar las bibliotecas disponibles."
                  {...register('seafile_library')}
                />
                <TextField
                  label="Contraseña de biblioteca"
                  type="password"
                  autoComplete="new-password"
                  helperText="Solo se necesita para una biblioteca cifrada."
                  {...register('seafile_library_password')}
                />
              </>
            )}
            {provider === 'SMB' && (
              <>
                <Alert severity="info">CTM debe alcanzar el NAS desde la LAN o una VPN ya configurada. La cuenta debe tener acceso de solo lectura al recurso.</Alert>
                <TextField label="Servidor NAS" placeholder="nas.intranet.local" {...register('smb_host')} error={Boolean(errors.smb_host)} helperText={errors.smb_host?.message ?? ' '} />
                <TextField label="Puerto SMB" inputMode="numeric" {...register('smb_port')} />
                <TextField label="Recurso compartido" placeholder="Datos" {...register('smb_share')} error={Boolean(errors.smb_share)} helperText={errors.smb_share?.message ?? ' '} />
                <TextField label="Carpeta raíz opcional" placeholder="Departamentos" {...register('smb_base_path')} />
                <TextField label="Dominio opcional" {...register('smb_domain')} />
                <TextField label="Usuario SMB" autoComplete="username" {...register('smb_username')} error={Boolean(errors.smb_username)} helperText={errors.smb_username?.message ?? ' '} />
                <TextField label="Contraseña SMB" type="password" autoComplete="new-password" {...register('smb_password')} error={Boolean(errors.smb_password)} helperText={errors.smb_password?.message ?? ' '} />
              </>
            )}
            {provider === 'MOCK' && <Alert severity="info">Conexión local de demostración; no usa credenciales externas.</Alert>}
            <Controller
              name="is_global"
              control={control}
              render={({ field }) => (
                <FormControlLabel
                  control={<Checkbox checked={field.value} onChange={(_event, checked) => field.onChange(checked)} />}
                  label="Disponible para todos los usuarios"
                />
              )}
            />
            {!watch('is_global') && <Typography variant="caption" color="text.secondary">Después de crearla, asígnala a usuarios desde Administración.</Typography>}
            {error && <Alert severity="error">{error}</Alert>}
          </Stack>
        </DialogContent>
        <DialogActions sx={{ px: 3, pb: 2.5 }}>
          <Button onClick={close} disabled={busy}>Cancelar</Button>
          <Button type="submit" variant="contained" disabled={busy}>
            {busy ? <CircularProgress size={20} color="inherit" /> : provider === 'GOOGLE_DRIVE' ? 'Conectar con Google' : 'Guardar conexión'}
          </Button>
        </DialogActions>
      </Box>
    </Dialog>
  );
}

function ChangePasswordDialog({
  open,
  canClose,
  onClose,
  onChanged,
}: {
  open: boolean;
  canClose: boolean;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const { register, handleSubmit, reset, formState: { errors } } = useForm<PasswordForm>({
    resolver: zodResolver(z.object({
      current_password: z.string().min(1, 'Ingresa tu contraseña actual.'),
      new_password: z.string().min(12, 'Usa al menos 12 caracteres.'),
    })),
  });
  const submit = async (values: PasswordForm) => {
    setBusy(true);
    setError('');
    try {
      await apiRequest('/api/v1/auth/password', { method: 'POST', body: JSON.stringify(values) });
      reset();
      onChanged();
      onClose();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'No se pudo actualizar la contraseña.');
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog open={open} onClose={canClose ? onClose : undefined} fullWidth maxWidth="xs" aria-labelledby="password-dialog-title">
      <DialogTitle id="password-dialog-title">
        Cambia tu contraseña
        <Typography component="span" display="block" variant="body2" color="text.secondary" sx={{ mt: 0.5, fontWeight: 400, letterSpacing: 0 }}>
          Elige una clave de al menos 12 caracteres.
        </Typography>
      </DialogTitle>
      <Box component="form" onSubmit={handleSubmit(submit)} noValidate>
        <DialogContent>
          <Stack spacing={2}>
            {!canClose && <Alert severity="warning">Debes cambiar la contraseña temporal para continuar.</Alert>}
            <TextField label="Contraseña actual" type="password" autoComplete="current-password" {...register('current_password')} error={Boolean(errors.current_password)} helperText={errors.current_password?.message ?? ' '} />
            <TextField label="Nueva contraseña" type="password" autoComplete="new-password" {...register('new_password')} error={Boolean(errors.new_password)} helperText={errors.new_password?.message ?? 'Mínimo 12 caracteres.'} />
            {error && <Alert severity="error">{error}</Alert>}
          </Stack>
        </DialogContent>
        <DialogActions sx={{ px: 3, pb: 2.5 }}>
          {canClose && <Button onClick={onClose} disabled={busy}>Cancelar</Button>}
          <Button type="submit" variant="contained" disabled={busy}>
            {busy ? <CircularProgress size={20} color="inherit" /> : 'Actualizar contraseña'}
          </Button>
        </DialogActions>
      </Box>
    </Dialog>
  );
}

function AdminDialog({
  open,
  onClose,
  currentUserId,
}: {
  open: boolean;
  onClose: () => void;
  currentUserId: string;
}) {
  const queryClient = useQueryClient();
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [adminTab, setAdminTab] = useState<'general' | 'smtp'>('general');
  const [smtpSaving, setSmtpSaving] = useState(false);
  const [smtpTesting, setSmtpTesting] = useState(false);
  const [smtpResult, setSmtpResult] = useState<{ severity: 'success' | 'error'; message: string } | null>(null);
  const [selectedUser, setSelectedUser] = useState('');
  const [selectedConnection, setSelectedConnection] = useState('');
  const usersQuery = useQuery({
    queryKey: ['admin-users'],
    queryFn: () => apiRequest<{ items: ManagedUser[]; total_count: number }>('/api/v1/users'),
    enabled: open && adminTab === 'general',
  });
  const connectionsQuery = useQuery({
    queryKey: ['connections'],
    queryFn: () => apiRequest<{ items: Connection[] }>('/api/v1/connections'),
    enabled: open && adminTab === 'general',
  });
  const auditQuery = useQuery({
    queryKey: ['audit-events'],
    queryFn: () => apiRequest<{ items: Array<{ id: string; action: string; result: string; created_at: string }> }>('/api/v1/audit-events?limit=20'),
    enabled: open && adminTab === 'general',
  });
  const smtpQuery = useQuery({
    queryKey: ['admin-smtp-settings'],
    queryFn: () => apiRequest<SmtpSettings>('/api/v1/settings/smtp'),
    enabled: open && adminTab === 'smtp',
  });
  const { register, control, handleSubmit, reset, formState: { errors } } = useForm({
    resolver: zodResolver(z.object({
      username: z.string().min(1, 'Ingresa un usuario.'),
      email: z.string().email('Ingresa un correo válido.'),
      password: z.string().min(12, 'Usa al menos 12 caracteres.'),
      role: z.enum(['ADMIN', 'USER']),
    })),
    defaultValues: { username: '', email: '', password: '', role: 'USER' as const },
  });
  const {
    register: registerSmtp,
    control: smtpControl,
    handleSubmit: handleSmtpSubmit,
    reset: resetSmtp,
    formState: { errors: smtpErrors },
  } = useForm<SmtpFormValues>({
    resolver: zodResolver(smtpFormSchema),
    defaultValues: {
      host: '',
      port: 587,
      use_tls: true,
      from_address: '',
      credentials_configured: false,
      username: '',
      password: '',
    },
  });

  useEffect(() => {
    if (smtpQuery.data) {
      resetSmtp({ ...smtpQuery.data, username: '', password: '' });
    }
  }, [smtpQuery.data, resetSmtp]);

  const createUser = async (values: { username: string; email: string; password: string; role: 'ADMIN' | 'USER' }) => {
    setBusy(true);
    setError('');
    try {
      await apiRequest('/api/v1/users', {
        method: 'POST',
        body: JSON.stringify({ ...values, must_change_password: true }),
      });
      reset();
      await queryClient.invalidateQueries({ queryKey: ['admin-users'] });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'No se pudo crear el usuario.');
    } finally {
      setBusy(false);
    }
  };

  const deactivate = async (userId: string) => {
    setError('');
    try {
      await apiRequest(`/api/v1/users/${userId}/deactivate`, { method: 'POST' });
      await queryClient.invalidateQueries({ queryKey: ['admin-users'] });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'No se pudo desactivar el usuario.');
    }
  };

  const grantConnection = async () => {
    if (!selectedUser || !selectedConnection) return;
    setError('');
    try {
      await apiRequest(`/api/v1/connections/${selectedConnection}/users/${selectedUser}`, { method: 'POST' });
      setSelectedUser('');
      setSelectedConnection('');
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'No se pudo asignar la conexión.');
    }
  };

  const saveSmtpSettings = async (values: SmtpFormValues) => {
    setSmtpSaving(true);
    setSmtpResult(null);
    try {
      await apiRequest<SmtpSettings>('/api/v1/settings/smtp', {
        method: 'PUT',
        body: JSON.stringify({
          host: values.host,
          port: values.port,
          use_tls: values.use_tls,
          from_address: values.from_address,
          username: values.username,
          password: values.password,
        }),
      });
      await queryClient.invalidateQueries({ queryKey: ['admin-smtp-settings'] });
      setSmtpResult({ severity: 'success', message: 'Configuración SMTP guardada.' });
    } catch {
      setSmtpResult({ severity: 'error', message: 'No se pudo guardar la configuración SMTP.' });
    } finally {
      setSmtpSaving(false);
    }
  };

  const testSmtpSettings = async (values: SmtpFormValues) => {
    setSmtpTesting(true);
    setSmtpResult(null);
    try {
      const result = await apiRequest<{ success: boolean; message: string }>(
        '/api/v1/settings/smtp/test',
        {
          method: 'POST',
          body: JSON.stringify({
            host: values.host,
            port: values.port,
            use_tls: values.use_tls,
            from_address: values.from_address,
            username: values.username,
            password: values.password,
          }),
        },
      );
      setSmtpResult({ severity: result.success ? 'success' : 'error', message: result.message });
    } catch {
      setSmtpResult({ severity: 'error', message: 'No se pudo probar la conexión SMTP.' });
    } finally {
      setSmtpTesting(false);
    }
  };

  const users = usersQuery.data?.items ?? [];
  const privateConnections = (connectionsQuery.data?.items ?? []).filter((connection) => !connection.is_global);

  return (
    <Dialog open={open} onClose={onClose} fullWidth maxWidth="md" aria-labelledby="admin-dialog-title">
      <DialogTitle id="admin-dialog-title">
        Administración
        <Typography component="span" display="block" variant="body2" color="text.secondary" sx={{ mt: 0.5, fontWeight: 400, letterSpacing: 0 }}>
          Gestiona usuarios, accesos privados y actividad reciente.
        </Typography>
      </DialogTitle>
      <DialogContent>
        <Tabs
          className="admin-tabs"
          value={adminTab}
          onChange={(_event, value: 'general' | 'smtp') => setAdminTab(value)}
          aria-label="Pestañas de administración"
          variant="scrollable"
          allowScrollButtonsMobile
        >
          <Tab value="general" label="General" />
          <Tab value="smtp" label="Correo SMTP" />
        </Tabs>
        {adminTab === 'general' && <Stack spacing={3}>
          <Box className="admin-section">
            <Typography variant="h6" sx={{ mb: 1.5 }}>Crear usuario</Typography>
            <Box component="form" onSubmit={handleSubmit(createUser)} noValidate>
              <Stack spacing={1.5}>
                <TextField label="Nombre de usuario" {...register('username')} error={Boolean(errors.username)} helperText={errors.username?.message as string ?? ' '} />
                <TextField label="Correo" type="email" {...register('email')} error={Boolean(errors.email)} helperText={errors.email?.message as string ?? ' '} />
                <TextField label="Contraseña inicial" type="password" autoComplete="new-password" {...register('password')} error={Boolean(errors.password)} helperText={errors.password?.message as string ?? 'Se solicitará cambiarla al iniciar sesión.'} />
                <FormControl fullWidth>
                  <InputLabel id="new-user-role-label">Rol</InputLabel>
                  <Controller name="role" control={control} render={({ field }) => (
                    <Select {...field} labelId="new-user-role-label" label="Rol">
                      <MenuItem value="USER">Usuario</MenuItem>
                      <MenuItem value="ADMIN">Administrador</MenuItem>
                    </Select>
                  )} />
                </FormControl>
                <Button type="submit" variant="contained" disabled={busy}>
                  {busy ? <CircularProgress size={20} color="inherit" /> : 'Crear usuario'}
                </Button>
              </Stack>
            </Box>
          </Box>

          <Box className="admin-section">
            <Typography variant="h6" sx={{ mb: 1 }}>Usuarios</Typography>
            <Paper variant="outlined" className="admin-list">
              {usersQuery.isLoading && <LinearProgress />}
              {users.map((managedUser) => (
                <Box className="admin-user-row" key={managedUser.id}>
                  <Box>
                    <Typography fontWeight={650}>{managedUser.username}</Typography>
                    <Typography variant="caption" color="text.secondary">{managedUser.email} · {managedUser.role === 'ADMIN' ? 'Administrador' : 'Usuario'}</Typography>
                  </Box>
                  {managedUser.is_active
                    ? <Button size="small" color="error" disabled={managedUser.id === currentUserId} onClick={() => void deactivate(managedUser.id)}>Desactivar</Button>
                    : <Typography variant="caption" color="text.secondary">Inactivo</Typography>}
                </Box>
              ))}
            </Paper>
          </Box>

          <Box className="admin-section">
            <Typography variant="h6" sx={{ mb: 1 }}>Acceso a conexiones privadas</Typography>
            {privateConnections.length === 0
              ? <Typography variant="body2" color="text.secondary">No hay conexiones privadas pendientes de asignación.</Typography>
              : <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1}>
                  <FormControl fullWidth size="small">
                    <InputLabel id="grant-user-label">Usuario</InputLabel>
                    <Select labelId="grant-user-label" label="Usuario" value={selectedUser} onChange={(event) => setSelectedUser(event.target.value)}>
                      {users.filter((managedUser) => managedUser.is_active).map((managedUser) => <MenuItem key={managedUser.id} value={managedUser.id}>{managedUser.username}</MenuItem>)}
                    </Select>
                  </FormControl>
                  <FormControl fullWidth size="small">
                    <InputLabel id="grant-connection-label">Conexión</InputLabel>
                    <Select labelId="grant-connection-label" label="Conexión" value={selectedConnection} onChange={(event) => setSelectedConnection(event.target.value)}>
                      {privateConnections.map((connection) => (
                        <MenuItem key={connection.id} value={connection.id}>
                          <Stack direction="row" alignItems="center" spacing={1}><ProviderLogo provider={connection.provider_type} />{connection.name}</Stack>
                        </MenuItem>
                      ))}
                    </Select>
                  </FormControl>
                  <Button variant="outlined" disabled={!selectedUser || !selectedConnection} onClick={() => void grantConnection()}>Asignar</Button>
                </Stack>}
          </Box>

          <Box className="admin-section">
            <Typography variant="h6" sx={{ mb: 1 }}>Auditoría reciente</Typography>
            <Paper variant="outlined" className="admin-list">
              {(auditQuery.data?.items ?? []).slice(0, 5).map((event) => (
                <Box className="admin-user-row" key={event.id}>
                  <Typography variant="body2">{event.action.replaceAll('_', ' ')}</Typography>
                  <Box className="audit-event-meta">
                    <Typography variant="caption" color="text.secondary">{event.result}</Typography>
                    <Typography component="time" dateTime={event.created_at} variant="caption" color="text.secondary">
                      {formatRequestedAt(event.created_at) ?? 'Fecha no disponible'}
                    </Typography>
                  </Box>
                </Box>
              ))}
            </Paper>
          </Box>
          {error && <Alert severity="error">{error}</Alert>}
        </Stack>}
        {adminTab === 'smtp' && (
          <Box className="admin-section smtp-settings-section">
            <Stack spacing={2.5}>
              <Stack direction={{ xs: 'column', sm: 'row' }} justifyContent="space-between" alignItems={{ xs: 'flex-start', sm: 'center' }} spacing={1}>
                <Box>
                  <Typography variant="h6">Servidor SMTP</Typography>
                  <Typography variant="body2" color="text.secondary">
                    Configura el correo que envía las notificaciones de transferencia.
                  </Typography>
                </Box>
                {smtpQuery.data && (
                  <Chip
                    size="small"
                    color={smtpQuery.data.credentials_configured ? 'success' : 'default'}
                    label={smtpQuery.data.credentials_configured ? 'Credenciales SMTP configuradas' : 'Sin credenciales'}
                  />
                )}
              </Stack>
              {smtpQuery.isLoading && <LinearProgress aria-label="Cargando configuración SMTP" />}
              {smtpQuery.isError && <Alert severity="error">No se pudo cargar la configuración SMTP.</Alert>}
              {smtpQuery.data && (
                <Box component="form" onSubmit={handleSmtpSubmit(saveSmtpSettings)} noValidate>
                  <Stack spacing={2.5}>
                    <Box className="smtp-settings-grid">
                      <TextField
                        label="Servidor SMTP"
                        {...registerSmtp('host')}
                        error={Boolean(smtpErrors.host)}
                        helperText={smtpErrors.host?.message ?? ' '}
                        autoComplete="url"
                      />
                      <TextField
                        label="Puerto"
                        type="number"
                        inputProps={{ min: 1, max: 65535 }}
                        {...registerSmtp('port', { valueAsNumber: true })}
                        error={Boolean(smtpErrors.port)}
                        helperText={smtpErrors.port?.message ?? ' '}
                      />
                      <TextField
                        label="Usuario SMTP"
                        autoComplete="username"
                        {...registerSmtp('username')}
                        error={Boolean(smtpErrors.username)}
                        helperText={smtpErrors.username?.message ?? ' '}
                      />
                      <TextField
                        label="Contraseña SMTP"
                        type="password"
                        autoComplete="new-password"
                        {...registerSmtp('password')}
                        error={Boolean(smtpErrors.password)}
                        helperText={smtpErrors.password?.message ?? ' '}
                      />
                      <TextField
                        label="Correo remitente"
                        type="email"
                        {...registerSmtp('from_address')}
                        error={Boolean(smtpErrors.from_address)}
                        helperText={smtpErrors.from_address?.message ?? ' '}
                      />
                    </Box>
                    <FormHelperText className="smtp-credentials-hint">
                      Deja usuario y contraseña vacíos para conservar las credenciales guardadas.
                    </FormHelperText>
                    <Box className="smtp-tls-option">
                      <Controller
                        name="use_tls"
                        control={smtpControl}
                        render={({ field }) => (
                          <FormControlLabel
                            control={<Checkbox {...field} checked={field.value} />}
                            label="Usar STARTTLS"
                          />
                        )}
                      />
                      <Typography variant="caption" color="text.secondary">
                        Cifra la conexión entre la aplicación y el servidor de correo.
                      </Typography>
                    </Box>
                    {smtpResult && <Alert severity={smtpResult.severity}>{smtpResult.message}</Alert>}
                    <Stack direction={{ xs: 'column-reverse', sm: 'row' }} justifyContent="flex-end" spacing={1.25}>
                      <Button
                        variant="outlined"
                        disabled={smtpSaving || smtpTesting}
                        onClick={handleSmtpSubmit(testSmtpSettings)}
                      >
                        {smtpTesting ? <><CircularProgress size={16} color="inherit" />&nbsp; Probando conexión</> : 'Probar conexión'}
                      </Button>
                      <Button type="submit" variant="contained" disabled={smtpSaving || smtpTesting}>
                        {smtpSaving ? <><CircularProgress size={16} color="inherit" />&nbsp; Guardando</> : 'Guardar configuración'}
                      </Button>
                    </Stack>
                  </Stack>
                </Box>
              )}
            </Stack>
          </Box>
        )}
      </DialogContent>
      <DialogActions sx={{ px: 3, pb: 2.5 }}><Button onClick={onClose}>Cerrar</Button></DialogActions>
    </Dialog>
  );
}

function pathCrumbs(path: string): string[] {
  return path.split('/').filter(Boolean);
}

function formatBytes(size: number | null): string {
  if (size === null) return '—';
  if (size < 1024) return `${size} B`;
  const units = ['KiB', 'MiB', 'GiB', 'TiB'];
  let value = size / 1024;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value.toFixed(value >= 10 ? 1 : 2)} ${units[index]}`;
}

function formatProgressPercent(value: number, fractionDigits = 0): string {
  const bounded = Math.max(0, Math.min(100, value));
  const precision = 10 ** fractionDigits;
  const rounded = Math.round(bounded * precision) / precision;
  const displayValue = bounded < 100 && rounded >= 100 ? 100 - 1 / precision : rounded;
  return displayValue.toFixed(fractionDigits);
}

function formatRequestedAt(value?: string | null): string | null {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return new Intl.DateTimeFormat('es-CL', { dateStyle: 'medium', timeStyle: 'short' }).format(date);
}

function localDateTimeValue(date: Date): string {
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

function transferWaitReasonLabel(reason?: string | null): string | null {
  if (!reason) return null;
  const labels: Record<string, string> = {
    WAITING_UNTIL: 'A la espera de la hora programada',
    WAITING_PARENT: 'A la espera de la transferencia padre',
    BLOCKED_PARENT_FAILED: 'Bloqueada: la transferencia padre no terminó correctamente',
    WAITING_CAPACITY: 'En cola: no hay un espacio de ejecución disponible',
    WAITING_EXCLUSIVE: 'En cola: espera a que terminen las otras transferencias',
    WAITING_CONNECTION: 'En cola: una conexión está ocupada',
    WAITING_STAGING: 'En cola: espera el espacio temporal de transferencia',
    WAITING_CONNECTION_ACTIVE: 'En espera: activa las conexiones de origen y destino',
    WAITING_CONNECTION_ACCESS: 'En espera: restaura el acceso a las conexiones',
    WAITING_USER_ACTIVE: 'En espera: la cuenta solicitante está inactiva',
    BLOCKED_CONNECTION_CHANGED: 'Bloqueada: cambiaron las credenciales. Crea una transferencia nueva para usar la conexión actualizada.',
  };
  return labels[reason] ?? 'En espera de condiciones para iniciar';
}

function localTimeOffsetLabel(value: string, timeZone: string): string {
  const date = new Date(value);
  const part = new Intl.DateTimeFormat('es-CL', {
    timeZone,
    timeZoneName: 'shortOffset',
  }).formatToParts(date).find((item) => item.type === 'timeZoneName');
  return part?.value ?? timeZone;
}

function formatTransferDuration(transfer: Transfer): string {
  const isTerminal = terminalTransferStatuses.includes(transfer.status);
  if (!transfer.started_at) {
    return isTerminal ? 'Duración no disponible' : 'En cola';
  }
  if (isTerminal && !transfer.completed_at) return 'Duración no disponible';
  const start = new Date(transfer.started_at).getTime();
  const end = transfer.completed_at ? new Date(transfer.completed_at).getTime() : Date.now();
  if (Number.isNaN(start) || Number.isNaN(end)) return 'Duración no disponible';
  const seconds = Math.max(0, Math.floor((end - start) / 1000));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainingSeconds = seconds % 60;
  const elapsed = hours > 0
    ? `${hours} h ${minutes} min`
    : minutes > 0
      ? `${minutes} min ${remainingSeconds} s`
      : `${remainingSeconds} s`;
  return transfer.completed_at ? `Duración: ${elapsed}` : `En curso · ${elapsed}`;
}

function transferStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    SCHEDULED: 'Programada',
    QUEUED: 'En cola',
    VALIDATING: 'Validando',
    RUNNING: 'En curso',
    RETRYING: 'Reintentando',
    CANCELLING: 'Cancelando',
    COMPLETED: 'Completada',
    COMPLETED_WITH_WARNINGS: 'Completada con avisos',
    FAILED: 'Fallida',
    CANCELLED: 'Cancelada',
  };
  return labels[status] ?? status.replaceAll('_', ' ').toLowerCase();
}

function providerLabel(provider: string): string {
  if (provider === 'GOOGLE_DRIVE') return 'Google Drive';
  if (provider === 'SEAFILE') return 'Seafile Community Edition';
  if (provider === 'SMB') return 'NAS privado (SMB)';
  if (provider === 'MOCK') return 'Simulada';
  return provider;
}

function connectionAdapterLabel(connection: Connection): string | null {
  if (connection.provider_type !== 'SEAFILE') return null;
  return connection.adapter_type === 'SEAFILE_API'
    ? 'Adaptador: API de Seafile'
    : 'Adaptador: Rclone (predeterminado)';
}

function connectionStatusLabel(status: string): string {
  if (status === 'ACTIVE') return 'Activa';
  if (status === 'ERROR') return 'Requiere atención';
  return 'Pendiente de prueba';
}

function connectionStatusColor(status: string): 'success' | 'error' | 'warning' {
  if (status === 'ACTIVE') return 'success';
  if (status === 'ERROR') return 'error';
  return 'warning';
}

function lastTestLabel(connection: Connection): string {
  if (!connection.last_test_status) return 'Sin prueba registrada';
  const result = connection.last_test_status === 'SUCCESS' ? 'Prueba exitosa' : 'Prueba fallida';
  if (!connection.last_tested_at) return result;
  const date = new Date(connection.last_tested_at);
  return Number.isNaN(date.getTime())
    ? result
    : `${result} · ${new Intl.DateTimeFormat('es-CL', { dateStyle: 'medium', timeStyle: 'short' }).format(date)}`;
}

function ConnectionManagementSection({
  connections,
  loading,
  loadError,
  onAdd,
  onRefresh,
  onArchive,
}: {
  connections: Connection[];
  loading: boolean;
  loadError: boolean;
  onAdd: () => void;
  onRefresh: () => void;
  onArchive: (connection: Connection) => Promise<void>;
}) {
  const [selectedConnection, setSelectedConnection] = useState<Connection | null>(null);

  const requestArchive = (connection: Connection) => {
    setSelectedConnection(connection);
  };

  const closeArchiveDialog = () => {
    setSelectedConnection(null);
  };

  return (
    <Box component="section" className="connection-management" aria-labelledby="connection-management-title">
      <Box className="connection-management-heading">
        <Box>
          <Typography variant="h5" component="h2" id="connection-management-title">Gestiona tus conexiones</Typography>
          <Typography color="text.secondary" sx={{ mt: 0.5 }}>
            Revisa su estado y archiva las que ya no uses. El historial de transferencias se conserva.
          </Typography>
        </Box>
        <Button variant="contained" onClick={onAdd} startIcon={<span aria-hidden="true">＋</span>}>Agregar conexión</Button>
      </Box>

      <Paper className="connection-inventory" elevation={0} role="list" aria-label="Inventario de conexiones">
        <Box className="connection-inventory-head" aria-hidden="true">
          <span>Conexión</span>
          <span>Estado</span>
          <span>Acceso</span>
          <span>Última prueba</span>
          <span>Acción</span>
        </Box>
        {loading && <LinearProgress aria-label="Cargando conexiones" />}
        {loadError && (
          <Alert
            severity="error"
            sx={{ m: 2 }}
            action={<Button color="inherit" size="small" onClick={onRefresh}>Reintentar</Button>}
          >
            No se pudieron cargar las conexiones.
          </Alert>
        )}
        {!loading && !loadError && connections.length === 0 && (
          <Box className="connection-empty-state">
            <Typography variant="h6">Aún no hay conexiones</Typography>
            <Typography color="text.secondary" sx={{ mb: 2 }}>Agrega un proveedor para empezar a explorar y transferir archivos.</Typography>
            <Button variant="outlined" onClick={onAdd}>Agregar primera conexión</Button>
          </Box>
        )}
        {connections.map((connection) => (
          <Box className="connection-row" component="article" role="listitem" key={connection.id}>
            <Box className="connection-name-field">
              <span className="connection-field-label">Conexión</span>
              <Box className="connection-identity">
                <ProviderLogo provider={connection.provider_type} />
                <Box className="connection-name-field">
                  <Typography fontWeight={700}>{connection.name}</Typography>
                  <Typography variant="caption" color="text.secondary">{providerLabel(connection.provider_type)}</Typography>
                </Box>
              </Box>
              {connectionAdapterLabel(connection) && (
                <Typography variant="caption" color="text.secondary">
                  {connectionAdapterLabel(connection)}
                </Typography>
              )}
            </Box>
            <Box className="connection-row-field">
              <span className="connection-field-label">Estado</span>
              <Chip size="small" color={connectionStatusColor(connection.status)} label={connectionStatusLabel(connection.status)} />
            </Box>
            <Box className="connection-row-field">
              <span className="connection-field-label">Acceso</span>
              <Typography variant="body2">{connection.is_global ? 'Global' : 'Privada'}</Typography>
            </Box>
            <Box className="connection-row-field connection-test-field">
              <span className="connection-field-label">Última prueba</span>
              <Typography variant="body2">{lastTestLabel(connection)}</Typography>
            </Box>
            <Box className="connection-action-field">
              <Button
                color="error"
                variant="text"
                startIcon={<InterfaceIcon name="archive" />}
                aria-label={`Archivar ${connection.name}`}
                onClick={() => requestArchive(connection)}
              >
                Archivar
              </Button>
            </Box>
          </Box>
        ))}
      </Paper>

      <ConnectionArchiveDialog
        connection={selectedConnection}
        onClose={closeArchiveDialog}
        onArchive={onArchive}
      />
    </Box>
  );
}

function ConnectionArchiveDialog({
  connection,
  onClose,
  onArchive,
}: {
  connection: Connection | null;
  onClose: () => void;
  onArchive: (connection: Connection) => Promise<void>;
}) {
  const [archiveError, setArchiveError] = useState('');
  const [archiving, setArchiving] = useState(false);

  const closeArchiveDialog = () => {
    if (archiving) return;
    setArchiveError('');
    onClose();
  };

  const confirmArchive = async () => {
    if (!connection || archiving) return;
    setArchiving(true);
    setArchiveError('');
    try {
      await onArchive(connection);
      onClose();
    } catch (caught) {
      setArchiveError(caught instanceof Error ? caught.message : 'No se pudo archivar la conexión.');
    } finally {
      setArchiving(false);
    }
  };

  return (
    <Dialog
      open={Boolean(connection)}
      onClose={closeArchiveDialog}
      fullWidth
      maxWidth="xs"
      aria-labelledby="archive-connection-title"
    >
      <DialogTitle id="archive-connection-title">Archivar conexión</DialogTitle>
      <DialogContent>
        <Stack spacing={2}>
          <Typography>
            ¿Quieres archivar <strong>{connection?.name}</strong>? Dejará de aparecer en el explorador y se quitarán sus credenciales guardadas.
          </Typography>
          <Typography variant="body2" color="text.secondary">
            <span className="dialog-provider"><ProviderLogo provider={connection?.provider_type ?? ''} />Proveedor: {connection ? providerLabel(connection.provider_type) : ''}</span>
          </Typography>
          <Typography color="text.secondary" variant="body2">
            El historial de transferencias se conservará. No se puede archivar una conexión mientras tenga una transferencia activa.
          </Typography>
          {archiveError && <Alert severity="error">{archiveError}</Alert>}
        </Stack>
      </DialogContent>
      <DialogActions sx={{ px: 3, pb: 2.5 }}>
        <Button onClick={closeArchiveDialog} disabled={archiving}>Cancelar</Button>
        <Button color="error" variant="contained" aria-label="Confirmar archivado" onClick={() => void confirmArchive()} disabled={archiving}>
          {archiving ? <CircularProgress size={20} color="inherit" /> : 'Confirmar archivado'}
        </Button>
      </DialogActions>
    </Dialog>
  );
}

function PrivateServicesSection({
  connections,
  loading,
  isAdmin,
  onAdd,
  onExplore,
  onArchive,
}: {
  connections: Connection[];
  loading: boolean;
  isAdmin: boolean;
  onAdd: () => void;
  onExplore: (connection: Connection) => void;
  onArchive: (connection: Connection) => Promise<void>;
}) {
  const [selectedConnection, setSelectedConnection] = useState<Connection | null>(null);

  return (
    <Box component="section" aria-labelledby="private-services-title">
      <Box className="connection-management-heading">
        <Box>
          <Typography variant="h5" component="h2" id="private-services-title">Servicios privados</Typography>
          <Typography color="text.secondary" sx={{ mt: 0.5 }}>
            NAS accesibles desde la red de CTM o desde una VPN ya configurada.
          </Typography>
        </Box>
        {isAdmin && <Button variant="contained" onClick={onAdd}>Agregar NAS</Button>}
      </Box>
      {loading && <LinearProgress aria-label="Cargando servicios privados" />}
      {!loading && connections.length === 0 && (
        <Paper className="connection-empty-state" elevation={0}>
          <Typography variant="h6">Aún no hay NAS configurados</Typography>
          <Typography color="text.secondary">
            Un administrador puede conectar un recurso compartido SMB accesible desde CTM.
          </Typography>
        </Paper>
      )}
      <Stack spacing={1.5}>
        {connections.map((connection) => (
          <Paper key={connection.id} className="connection-inventory" elevation={0}>
            <Box className="connection-row private-service-row">
              <Box className="connection-name-field">
                <Typography fontWeight={700}>{connection.name}</Typography>
                <Typography variant="caption" color="text.secondary">NAS · SMB</Typography>
              </Box>
              <Chip size="small" color={connectionStatusColor(connection.status)} label={connectionStatusLabel(connection.status)} />
              <Stack className="private-service-actions" direction="row" spacing={1}>
                <Button
                  variant="outlined"
                  disabled={connection.status !== 'ACTIVE'}
                  onClick={() => onExplore(connection)}
                  aria-label={`Explorar ${connection.name}`}
                >
                  Explorar NAS
                </Button>
                {isAdmin && (
                  <Button
                    color="error"
                    variant="text"
                    startIcon={<InterfaceIcon name="archive" />}
                    aria-label={`Archivar ${connection.name}`}
                    onClick={() => setSelectedConnection(connection)}
                  >
                    Archivar
                  </Button>
                )}
              </Stack>
            </Box>
          </Paper>
        ))}
      </Stack>
      <ConnectionArchiveDialog
        connection={selectedConnection}
        onClose={() => setSelectedConnection(null)}
        onArchive={onArchive}
      />
    </Box>
  );
}

function ExplorerPanel({
  title,
  side,
  connections,
  connectionId,
  onConnectionChange,
  path,
  onPathChange,
  selectedPaths,
  onToggle,
  isAdmin,
  onTestConnection,
}: {
  title: string;
  side: 'source' | 'destination';
  connections: Connection[];
  connectionId: string;
  onConnectionChange: (id: string) => void;
  path: string;
  onPathChange: (path: string) => void;
  selectedPaths?: string[];
  onToggle?: (item: StorageItem) => void;
  isAdmin: boolean;
  onTestConnection: (connectionId: string, otp?: string) => Promise<void>;
}) {
  const [search, setSearch] = useState('');
  const [testing, setTesting] = useState(false);
  const [otp, setOtp] = useState('');
  const active = connections.find((connection) => connection.id === connectionId);
  const query = useInfiniteQuery({
    queryKey: ['items', connectionId, path, search],
    enabled: Boolean(connectionId) && active?.status === 'ACTIVE',
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) => {
      const params = new URLSearchParams({ path, page_size: '50', sort: 'name', direction: 'asc' });
      if (search.trim()) params.set('search', search.trim());
      if (pageParam) params.set('cursor', pageParam);
      return apiRequest<ItemPage>(`/api/v1/connections/${connectionId}/items?${params}`);
    },
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
  });
  const items = query.data?.pages.flatMap((page) => page.items) ?? [];
  const crumbs = pathCrumbs(path);

  const testConnection = async () => {
    if (!active) return;
    const submittedOtp = otp.trim();
    setOtp('');
    setTesting(true);
    try {
      await onTestConnection(active.id, submittedOtp || undefined);
    } finally {
      setTesting(false);
    }
  };

  return (
    <Card className="explorer-card" variant="outlined">
      <CardContent className="explorer-content">
        <Stack direction="row" alignItems="center" justifyContent="space-between" spacing={1}>
          <Box>
            <Typography variant="overline" color="text.secondary">{side === 'source' ? '01 · ORIGEN' : '02 · DESTINO'}</Typography>
            <Typography variant="h6">{title}</Typography>
          </Box>
          {side === 'source' && <Typography className="selection-count">{selectedPaths?.length ?? 0} seleccionados</Typography>}
        </Stack>
        <FormControl fullWidth size="small" sx={{ mt: 2 }}>
          <InputLabel id={`${side}-connection-label`}>Conexión</InputLabel>
          <Select
            labelId={`${side}-connection-label`}
            label="Conexión"
            value={connectionId}
            onChange={(event) => {
              setOtp('');
              onConnectionChange(event.target.value);
            }}
          >
            {connections.map((connection) => (
              <MenuItem key={connection.id} value={connection.id}>
                <Stack direction="row" alignItems="center" spacing={1}><ProviderLogo provider={connection.provider_type} />{connection.name}</Stack>
              </MenuItem>
            ))}
          </Select>
        </FormControl>
        {active && (
          <>
            <Stack direction="row" alignItems="center" justifyContent="space-between" spacing={1}>
              <Typography className="explorer-connection-status" variant="caption" color="text.secondary">
                <ProviderLogo provider={active.provider_type} />
                {providerLabel(active.provider_type)} · {active.status === 'ACTIVE' ? 'activa' : active.status === 'ERROR' ? 'requiere atención' : 'pendiente de prueba'}
              </Typography>
              {isAdmin && (
                <Button size="small" onClick={() => void testConnection()} disabled={testing}>
                  {testing ? <CircularProgress size={16} /> : 'Probar conexión'}
                </Button>
              )}
            </Stack>
            {isAdmin && active.provider_type === 'SEAFILE' && (
              <TextField
                size="small"
                fullWidth
                label={`Código 2FA de Seafile (${side === 'source' ? 'origen' : 'destino'})`}
                type="password"
                autoComplete="one-time-code"
                value={otp}
                onChange={(event) => setOtp(event.target.value)}
                helperText="Opcional. Se envía solo al probar y se borra al enviar."
              />
            )}
            {active.status !== 'ACTIVE' && (
              <Alert severity={active.status === 'ERROR' ? 'error' : 'warning'}>
                Prueba esta conexión antes de explorar o transferir archivos.
              </Alert>
            )}
            {active.status === 'ACTIVE' && (
              <>
            <Stack direction="row" alignItems="center" spacing={0.5} className="breadcrumbs">
              <Button size="small" onClick={() => onPathChange('/')}>Mi unidad</Button>
              {crumbs.map((crumb, index) => {
                const crumbPath = `/${crumbs.slice(0, index + 1).join('/')}`;
                return <Button key={crumbPath} size="small" onClick={() => onPathChange(crumbPath)}>{crumb}</Button>;
              })}
            </Stack>
            <TextField
              size="small"
              fullWidth
              placeholder="Buscar en esta carpeta"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              inputProps={{ 'aria-label': `Buscar ${title.toLowerCase()}` }}
            />
            <Paper variant="outlined" className="file-list">
              {path !== '/' && (
                <Button className="parent-row" fullWidth onClick={() => onPathChange(`/${crumbs.slice(0, -1).join('/')}` || '/')}>↑ Subir un nivel</Button>
              )}
              {query.isLoading && <Box sx={{ p: 2 }}><LinearProgress /></Box>}
              {query.isError && <Alert severity="error" sx={{ m: 1 }}>No se pudo cargar esta carpeta.</Alert>}
              {items.map((item) => (
                <Box className="file-row" key={item.path}>
                  {side === 'source' ? (
                    <Checkbox
                      size="small"
                      checked={selectedPaths?.includes(item.path) ?? false}
                      onChange={() => onToggle?.(item)}
                      inputProps={{ 'aria-label': `Seleccionar ${item.name}` }}
                    />
                  ) : <Box className="file-row-spacer" />}
                  <Button
                    className="file-name"
                    onClick={() => item.is_dir && onPathChange(item.path)}
                    disabled={!item.is_dir}
                    startIcon={<InterfaceIcon name={item.is_dir ? 'folder' : 'file'} />}
                  >
                    <span>{item.name}</span>
                  </Button>
                  <Typography variant="caption" color="text.secondary">{item.is_dir ? 'Carpeta' : formatBytes(item.size_bytes)}</Typography>
                </Box>
              ))}
              {!query.isLoading && items.length === 0 && (
                <Typography color="text.secondary" variant="body2" sx={{ p: 2 }}>Esta carpeta está vacía.</Typography>
              )}
              {query.hasNextPage && (
                <Button fullWidth onClick={() => void query.fetchNextPage()} disabled={query.isFetchingNextPage}>
                  {query.isFetchingNextPage ? <CircularProgress size={18} /> : 'Cargar más'}
                </Button>
              )}
            </Paper>
            <Typography variant="caption" color="text.secondary">
              {side === 'source' ? 'Selecciona archivos o carpetas para copiar.' : `Destino actual: ${path}`}
            </Typography>
              </>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}

function TransferWorkspace({
  user,
  onLogout,
  onUserUpdated,
}: {
  user: User;
  onLogout: () => void;
  onUserUpdated: (patch: Partial<User>) => void;
}) {
  const queryClient = useQueryClient();
  const [workspaceSection, setWorkspaceSection] = useState<'explorer' | 'connections' | 'private' | 'scheduled' | 'documentation'>('explorer');
  const connectionsQuery = useQuery({
    queryKey: ['connections'],
    queryFn: () => apiRequest<{ items: Connection[] }>('/api/v1/connections'),
  });
  const transfersQuery = useQuery({
    queryKey: ['transfers'],
    queryFn: () => apiRequest<TransferListPage>('/api/v1/transfers?exclude_background=true&offset=0&page_size=50'),
    refetchInterval: 15_000,
    refetchIntervalInBackground: false,
  });
  const backgroundTransfersQuery = useQuery({
    queryKey: ['background-transfers'],
    queryFn: () => apiRequest<TransferListPage>('/api/v1/transfers?background_only=true&offset=0&page_size=200'),
    refetchInterval: 15_000,
    refetchIntervalInBackground: false,
  });
  const scheduledBackgroundTransfersQuery = useInfiniteQuery({
    queryKey: ['programmed-background-transfers'],
    enabled: workspaceSection === 'scheduled',
    initialPageParam: 0,
    queryFn: ({ pageParam }) => apiRequest<TransferListPage>(
      `/api/v1/transfers?background_only=true&offset=${pageParam}&page_size=200`,
    ),
    getNextPageParam: (lastPage, pages) => {
      const loadedCount = pages.reduce((count, page) => count + page.items.length, 0);
      return loadedCount < lastPage.total_count ? loadedCount : undefined;
    },
    refetchInterval: workspaceSection === 'scheduled' ? 15_000 : false,
    refetchIntervalInBackground: false,
  });
  const pendingTransfersQuery = useInfiniteQuery({
    queryKey: ['pending-transfers'],
    initialPageParam: 0,
    queryFn: ({ pageParam }) => apiRequest<{ items: Transfer[]; total_count: number }>(
      `/api/v1/transfers?pending_only=true&offset=${pageParam}&page_size=200`,
    ),
    getNextPageParam: (lastPage, pages) => {
      const loadedCount = pages.reduce((count, page) => count + page.items.length, 0);
      return loadedCount < lastPage.total_count ? loadedCount : undefined;
    },
    refetchInterval: 15_000,
    refetchIntervalInBackground: false,
  });
  const [reportTransferId, setReportTransferId] = useState<string | null>(null);
  const [backgroundPopoverAnchor, setBackgroundPopoverAnchor] = useState<HTMLButtonElement | null>(null);
  const [accountMenuAnchor, setAccountMenuAnchor] = useState<HTMLButtonElement | null>(null);
  const transferManifestQuery = useInfiniteQuery({
    queryKey: ['transfer-manifest', reportTransferId],
    enabled: Boolean(reportTransferId),
    initialPageParam: null as string | null,
    queryFn: ({ pageParam }) => {
      const params = new URLSearchParams({ limit: '100' });
      if (pageParam) params.set('after_id', pageParam);
      return apiRequest<TransferManifestPage>(
        `/api/v1/transfers/${reportTransferId}/items?${params}`,
      );
    },
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
  });
  const connections = connectionsQuery.data?.items ?? [];
  const destinationConnections = useMemo(
    () => connections.filter((connection) => connection.provider_type !== 'SMB'),
    [connections],
  );
  const connectionById = new Map(connections.map((connection) => [connection.id, connection]));
  const [sourceId, setSourceId] = useState('');
  const [destinationId, setDestinationId] = useState('');
  const [sourcePath, setSourcePath] = useState('/');
  const [destinationPath, setDestinationPath] = useState('/');
  const [selectedPaths, setSelectedPaths] = useState<string[]>([]);
  const sourceConnection = connectionById.get(sourceId);
  const destinationConnection = connectionById.get(destinationId);
  const canTransfer = Boolean(
    sourceId
    && destinationId
    && sourceId !== destinationId
    && selectedPaths.length > 0
    && selectedPaths.length <= 100,
  );
  const [conflictPolicy, setConflictPolicy] = useState<'SKIP' | 'OVERWRITE_IF_DIFFERENT'>('OVERWRITE_IF_DIFFERENT');
  const [sendEmailNotification, setSendEmailNotification] = useState(false);
  const [mobileStep, setMobileStep] = useState(0);
  const [activeTransfer, setActiveTransfer] = useState<Transfer | null>(null);
  const [scheduleDialogOpen, setScheduleDialogOpen] = useState(false);
  const [editingScheduleId, setEditingScheduleId] = useState<string | null>(null);
  const [scheduleDateTime, setScheduleDateTime] = useState(() => localDateTimeValue(new Date(Date.now() + 60 * 60_000)));
  const [scheduleMode, setScheduleMode] = useState<'AUTO' | 'EXCLUSIVE'>('AUTO');
  const [scheduleParentId, setScheduleParentId] = useState('');
  const [scheduleError, setScheduleError] = useState('');
  const [transferFailure, setTransferFailure] = useState<{ transferId: string; message: string } | null>(null);
  const [notice, setNotice] = useState('');
  const [noticeSeverity, setNoticeSeverity] = useState<'error' | 'info' | 'success' | 'warning'>('error');
  const [busy, setBusy] = useState(false);
  const [connectionDialogOpen, setConnectionDialogOpen] = useState(false);
  const [connectionDialogProvider, setConnectionDialogProvider] = useState<ConnectionForm['provider_type']>('MEGA');
  const [adminDialogOpen, setAdminDialogOpen] = useState(false);
  const [passwordDialogOpen, setPasswordDialogOpen] = useState(Boolean(user.must_change_password));
  const latestTransfer = useMemo(() => transfersQuery.data?.items[0], [transfersQuery.data]);
  const backgroundTransfers = backgroundTransfersQuery.data?.items ?? [];
  const allScheduledBackgroundTransfers = workspaceSection === 'scheduled'
    ? scheduledBackgroundTransfersQuery.data?.pages.flatMap((page) => page.items) ?? []
    : [];
  const backgroundTransferCount = backgroundTransfersQuery.data?.total_count ?? backgroundTransfers.length;
  const visibleActiveTransfer = activeTransfer && !isProgrammedBackgroundTransfer(activeTransfer)
    ? activeTransfer
    : null;
  const historyTransfers = (transfersQuery.data?.items ?? [])
    .filter((transfer) => !isProgrammedBackgroundTransfer(transfer))
    .map((transfer) => (
    visibleActiveTransfer?.id === transfer.id ? { ...transfer, ...visibleActiveTransfer } : transfer
  ));
  if (visibleActiveTransfer && !historyTransfers.some((transfer) => transfer.id === visibleActiveTransfer.id)) {
    historyTransfers.unshift(visibleActiveTransfer);
  }
  const liveTransfers = historyTransfers.filter((transfer) => (
    ['VALIDATING', 'RUNNING', 'RETRYING', 'CANCELLING'].includes(transfer.status)
    || visibleActiveTransfer?.id === transfer.id
  ));
  const pendingTransferItems = pendingTransfersQuery.data?.pages.flatMap((page) => page.items) ?? [];
  const programmedTransfers = [...new Map(
    [...pendingTransferItems, ...allScheduledBackgroundTransfers]
      .filter(isProgrammedBackgroundTransfer)
      .map((transfer) => [transfer.id, transfer]),
  ).values()];
  const scheduleParentOptions = [...new Map(
    [...historyTransfers, ...pendingTransferItems, ...backgroundTransfers, ...allScheduledBackgroundTransfers]
      .map((transfer) => [transfer.id, transfer]),
  ).values()];
  const streamTransferSignature = [...new Map(
    [...historyTransfers, ...backgroundTransfers, ...allScheduledBackgroundTransfers]
      .map((transfer) => [transfer.id, transfer]),
  ).values()]
    .filter((transfer) => (
      streamingTransferStatuses.includes(transfer.status)
      || (transfer.status === 'QUEUED' && !isProgrammedBackgroundTransfer(transfer))
    ))
    .map((transfer) => `${transfer.id}:${transfer.status}`)
    .join('|');
  const streamTransferIds = useMemo(
    () => streamTransferSignature
      ? streamTransferSignature.split('|').map((entry) => entry.slice(0, entry.lastIndexOf(':')))
      : [],
    [streamTransferSignature],
  );
  const backgroundTransferIdSignature = [...new Set(
    [...backgroundTransfers, ...allScheduledBackgroundTransfers].map((transfer) => transfer.id),
  )].join('|');
  const backgroundTransferIds = useMemo(
    () => new Set(backgroundTransferIdSignature ? backgroundTransferIdSignature.split('|') : []),
    [backgroundTransferIdSignature],
  );

  useEffect(() => {
    if (!activeTransfer && latestTransfer && !isProgrammedBackgroundTransfer(latestTransfer)
      && ['QUEUED', 'VALIDATING', 'RUNNING', 'RETRYING', 'CANCELLING'].includes(latestTransfer.status)) {
      setActiveTransfer(latestTransfer);
    }
  }, [activeTransfer, latestTransfer]);

  useEffect(() => {
    if (!activeTransfer || activeTransfer.status !== 'FAILED') return;
    const refreshed = transfersQuery.data?.items.find((transfer) => transfer.id === activeTransfer.id);
    if (refreshed && successfulTransferStatuses.includes(refreshed.status)) {
      setActiveTransfer(refreshed);
    }
  }, [activeTransfer?.id, activeTransfer?.status, transfersQuery.data]);

  useEffect(() => {
    if (!activeTransfer || !isProgrammedBackgroundTransfer(activeTransfer)) return;
    const refreshed = transfersQuery.data?.items.find(
      (transfer) => transfer.id === activeTransfer.id && terminalTransferStatuses.includes(transfer.status),
    );
    if (refreshed) {
      setActiveTransfer((current) => current?.id === activeTransfer.id ? null : current);
    }
  }, [activeTransfer?.id, activeTransfer?.status, transfersQuery.data]);

  useEffect(() => {
    if (!transferFailure) return;
    const refreshed = transfersQuery.data?.items.find(
      (transfer) => transfer.id === transferFailure.transferId,
    );
    if (refreshed && successfulTransferStatuses.includes(refreshed.status)) {
      setTransferFailure((current) => (
        current?.transferId === transferFailure.transferId ? null : current
      ));
    }
  }, [transferFailure?.transferId, transfersQuery.data]);

  useEffect(() => {
    if (!activeTransfer || !terminalTransferStatuses.includes(activeTransfer.status)) return;

    const terminalTransfer = activeTransfer;
    queryClient.setQueryData<TransferListPage>(['transfers'], (current) => {
      const items = current?.items ?? [];
      const existingTransfer = items.some((transfer) => transfer.id === terminalTransfer.id);
      return {
        ...(current ?? { items: [], total_count: 0 }),
        items: existingTransfer
          ? items.map((transfer) => (
            transfer.id === terminalTransfer.id
              ? { ...transfer, ...terminalTransfer }
              : transfer
          ))
          : [terminalTransfer, ...items],
        total_count: existingTransfer ? current?.total_count ?? items.length : (current?.total_count ?? items.length) + 1,
      };
    });
    void queryClient.invalidateQueries({ queryKey: ['transfers'] });
    void queryClient.invalidateQueries({ queryKey: ['background-transfers'] });
    void queryClient.invalidateQueries({ queryKey: ['programmed-background-transfers'] });
    void queryClient.invalidateQueries({ queryKey: ['pending-transfers'] });
    void queryClient.invalidateQueries({
      queryKey: ['items', terminalTransfer.destination_connection_id, terminalTransfer.destination_path],
    });

    const timeout = window.setTimeout(() => {
      setActiveTransfer((current) => (
        current?.id === terminalTransfer.id ? null : current
      ));
    }, terminalProgressDisplayMs);

    return () => window.clearTimeout(timeout);
  }, [activeTransfer, queryClient]);

  useEffect(() => {
    const oauthResult = new URLSearchParams(window.location.search).get('google_oauth');
    if (!oauthResult) return;
    setNotice(oauthResult === 'success'
      ? 'Google Drive quedó autorizado. Prueba la conexión para activarla.'
      : 'No se pudo completar la autorización de Google Drive.');
    setNoticeSeverity(oauthResult === 'success' ? 'success' : 'error');
    window.history.replaceState({}, '', window.location.pathname);
    void queryClient.invalidateQueries({ queryKey: ['connections'] });
  }, [queryClient]);

  useEffect(() => {
    const sourceIsAvailable = connections.some((connection) => connection.id === sourceId);
    const destinationIsAvailable = destinationConnections.some((connection) => connection.id === destinationId);
    if (sourceId && !sourceIsAvailable) {
      setSourceId(connections[0]?.id ?? '');
      setSourcePath('/');
      setSelectedPaths([]);
    } else if (!sourceId && connections[0]) {
      setSourceId(connections[0].id);
    }
    if (destinationId && !destinationIsAvailable) {
      const fallback = destinationConnections.find((connection) => connection.id !== sourceId)
        ?? destinationConnections[0];
      setDestinationId(fallback?.id ?? '');
      setDestinationPath('/');
    } else if (!destinationId && destinationConnections.length > 0) {
      const currentSource = sourceId || connections[0]?.id;
      const fallback = destinationConnections.find((connection) => connection.id !== currentSource)
      if (fallback) setDestinationId(fallback.id);
    }
  }, [connections, destinationConnections, destinationId, sourceId]);

  const runConnectionTest = async (connectionId: string, otp?: string) => {
    setNotice('');
    try {
      const result = await apiRequest<{ success: boolean; message: string }>(
        `/api/v1/connections/${connectionId}/test`,
        {
          method: 'POST',
          ...(otp ? { body: JSON.stringify({ otp }) } : {}),
        },
      );
      setNotice(result.success ? result.message : `${result.message}. La conexión sigue desactivada.`);
      setNoticeSeverity(result.success ? 'success' : 'error');
    } catch (caught) {
      setNotice(caught instanceof Error ? caught.message : 'No se pudo probar la conexión.');
      setNoticeSeverity('error');
    } finally {
      await queryClient.invalidateQueries({ queryKey: ['connections'] });
    }
  };

  useEffect(() => {
    const streams = streamTransferIds.map((transferId) => {
      const stream = new EventSource(`/api/v1/transfers/${transferId}/stream`);
      stream.onmessage = (event) => {
        const update = JSON.parse(event.data) as Transfer;
        const isBackground = backgroundTransferIds.has(update.id) || isProgrammedBackgroundTransfer(update);
        if (!isBackground) {
          setActiveTransfer((current) => current?.id === update.id ? update : current);
        }
        const targetQueryKey = isBackground && !terminalTransferStatuses.includes(update.status)
          ? ['background-transfers']
          : ['transfers'];
        queryClient.setQueryData<TransferListPage>(targetQueryKey, (current) => {
          const items = current?.items ?? [];
          const found = items.some((item) => item.id === update.id);
          return {
            ...(current ?? { items: [], total_count: 0 }),
            items: found
              ? items.map((item) => item.id === update.id ? { ...item, ...update } : item)
              : [update, ...items],
            total_count: found ? current?.total_count ?? items.length : (current?.total_count ?? items.length) + 1,
          };
        });
        if (isBackground && !terminalTransferStatuses.includes(update.status)) {
          queryClient.setQueryData<InfiniteData<TransferListPage, number>>(
            ['programmed-background-transfers'],
            (current) => current && ({
              ...current,
              pages: current.pages.map((page) => ({
                ...page,
                items: page.items.map((item) => item.id === update.id ? { ...item, ...update } : item),
              })),
            }),
          );
        }
        if (terminalTransferStatuses.includes(update.status)) {
          stream.close();
          if (isBackground) {
            setActiveTransfer((current) => current?.id === update.id ? null : current);
            queryClient.setQueryData<TransferListPage>(['background-transfers'], (current) => ({
              ...(current ?? { items: [], total_count: 0 }),
              items: (current?.items ?? []).filter((item) => item.id !== update.id),
              total_count: Math.max(0, (current?.total_count ?? 0) - 1),
            }));
            void queryClient.invalidateQueries({ queryKey: ['background-transfers'] });
            void queryClient.invalidateQueries({ queryKey: ['programmed-background-transfers'] });
            void queryClient.invalidateQueries({ queryKey: ['transfers'] });
            void queryClient.invalidateQueries({ queryKey: ['pending-transfers'] });
          }
          if (update.status === 'FAILED') {
            void apiRequest<{ items: Array<{ event_type: string; message: string }> }>(
              `/api/v1/transfers/${update.id}/events`,
            ).then(({ items }) => {
              const failure = [...items].reverse().find((item) => item.event_type === 'FAILURE_DIAGNOSTIC');
              if (failure) setTransferFailure({ transferId: update.id, message: failure.message });
            }).catch(() => undefined);
          }
        }
      };
      return stream;
    });
    return () => streams.forEach((stream) => stream.close());
  }, [streamTransferIds, backgroundTransferIds, queryClient]);

  const toggleItem = (item: StorageItem) => {
    setSelectedPaths((current) => current.includes(item.path)
      ? current.filter((path) => path !== item.path)
      : [...current, item.path]);
  };

  const openScheduleDialog = (transfer?: Transfer) => {
    setEditingScheduleId(transfer?.id ?? null);
    setScheduleDateTime(transfer?.scheduled_at
      ? localDateTimeValue(new Date(transfer.scheduled_at))
      : localDateTimeValue(new Date(Date.now() + 60 * 60_000)));
    setScheduleMode(transfer?.execution_mode ?? 'AUTO');
    setScheduleParentId(transfer?.depends_on_transfer_id ?? '');
    setScheduleError('');
    setScheduleDialogOpen(true);
  };

  const saveScheduledTransfer = async () => {
    const scheduledDate = new Date(scheduleDateTime);
    if (Number.isNaN(scheduledDate.getTime()) || localDateTimeValue(scheduledDate) !== scheduleDateTime) {
      setScheduleError('Esa hora local no existe por el cambio de horario. Elige otra hora.');
      return;
    }
    if (scheduledDate.getTime() <= Date.now()) {
      setScheduleError('Elige una fecha y hora futura.');
      return;
    }
    const scheduleTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
    const scheduling = {
      scheduled_at: scheduledDate.toISOString(),
      schedule_timezone: scheduleTimezone,
      execution_mode: scheduleMode,
      depends_on_transfer_id: scheduleParentId || null,
    };
    setBusy(true);
    setScheduleError('');
    try {
      const transfer = editingScheduleId
        ? await apiRequest<Transfer>(`/api/v1/transfers/${editingScheduleId}`, {
          method: 'PATCH',
          body: JSON.stringify(scheduling),
        })
        : await apiRequest<Transfer>('/api/v1/transfers', {
          method: 'POST',
          body: JSON.stringify({
            source_connection_id: sourceId,
            destination_connection_id: destinationId,
            source_paths: selectedPaths,
            destination_path: destinationPath,
            operation: 'COPY',
            conflict_policy: conflictPolicy,
            send_email_notification: sendEmailNotification,
            ...scheduling,
          }),
        });
      setActiveTransfer((current) => (
        isProgrammedBackgroundTransfer(transfer)
          ? current?.id === transfer.id ? null : current
          : transfer
      ));
      setScheduleDialogOpen(false);
      setSelectedPaths(editingScheduleId ? selectedPaths : []);
      setNotice(editingScheduleId ? 'La transferencia se reprogramó.' : 'La transferencia quedó programada.');
      setNoticeSeverity('success');
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['transfers'] }),
        queryClient.invalidateQueries({ queryKey: ['background-transfers'] }),
        queryClient.invalidateQueries({ queryKey: ['programmed-background-transfers'] }),
        queryClient.invalidateQueries({ queryKey: ['pending-transfers'] }),
      ]);
    } catch (error) {
      setScheduleError(error instanceof Error ? error.message : 'No se pudo programar la transferencia.');
    } finally {
      setBusy(false);
    }
  };

  const startTransfer = async () => {
    setBusy(true);
    setNotice('');
    setTransferFailure(null);
    try {
      const transfer = await apiRequest<Transfer>('/api/v1/transfers', {
        method: 'POST',
        body: JSON.stringify({
          source_connection_id: sourceId,
          destination_connection_id: destinationId,
          source_paths: selectedPaths,
          destination_path: destinationPath,
          operation: 'COPY',
          conflict_policy: conflictPolicy,
          send_email_notification: sendEmailNotification,
        }),
      });
      setActiveTransfer(transfer);
      setSelectedPaths([]);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['transfers'] }),
        queryClient.invalidateQueries({ queryKey: ['background-transfers'] }),
        queryClient.invalidateQueries({ queryKey: ['programmed-background-transfers'] }),
        queryClient.invalidateQueries({ queryKey: ['pending-transfers'] }),
      ]);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : 'No se pudo iniciar la transferencia.');
      setNoticeSeverity('error');
    } finally {
      setBusy(false);
    }
  };

  const cancelTransfer = async (transfer: Transfer) => {
    setBusy(true);
    try {
      const updated = await apiRequest<Transfer>(`/api/v1/transfers/${transfer.id}/cancel`, { method: 'POST' });
      if (activeTransfer?.id === updated.id) setActiveTransfer(updated);
      setNotice(updated.status === 'CANCELLED' ? 'La transferencia pendiente se canceló.' : 'Se solicitó cancelar la transferencia.');
      setNoticeSeverity('info');
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['transfers'] }),
        queryClient.invalidateQueries({ queryKey: ['background-transfers'] }),
        queryClient.invalidateQueries({ queryKey: ['programmed-background-transfers'] }),
        queryClient.invalidateQueries({ queryKey: ['pending-transfers'] }),
      ]);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : 'No se pudo cancelar la transferencia.');
      setNoticeSeverity('error');
    } finally {
      setBusy(false);
    }
  };

  const resumeTransfer = async (transfer: Transfer) => {
    setBusy(true);
    setNotice('');
    try {
      const updated = await apiRequest<Transfer>(
        `/api/v1/transfers/${transfer.id}/resume`,
        { method: 'POST' },
      );
      setActiveTransfer((current) => (
        isProgrammedBackgroundTransfer(updated)
          ? current?.id === updated.id ? null : current
          : updated
      ));
      setNotice('La transferencia se reanudó.');
      setNoticeSeverity('success');
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['transfers'] }),
        queryClient.invalidateQueries({ queryKey: ['background-transfers'] }),
        queryClient.invalidateQueries({ queryKey: ['programmed-background-transfers'] }),
        queryClient.invalidateQueries({ queryKey: ['pending-transfers'] }),
      ]);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : 'No se pudo reanudar la transferencia.');
      setNoticeSeverity('error');
    } finally {
      setBusy(false);
    }
  };

  const archiveConnection = async (connection: Connection) => {
    await apiRequest<void>(`/api/v1/connections/${connection.id}`, { method: 'DELETE' });
    queryClient.setQueryData<{ items: Connection[] }>(['connections'], (current) => current && ({
      ...current,
      items: current.items.filter((item) => item.id !== connection.id),
    }));
    await queryClient.invalidateQueries({ queryKey: ['connections'] });
    if (sourceId === connection.id) {
      setSourceId('');
      setSourcePath('/');
      setSelectedPaths([]);
    }
    if (destinationId === connection.id) {
      setDestinationId('');
      setDestinationPath('/');
    }
    setNotice(`Conexión “${connection.name}” archivada. El historial de transferencias se conservó.`);
    setNoticeSeverity('success');
  };

  const panels = (
    <Box className="explorer-grid">
      <ExplorerPanel
        title="Selecciona los elementos"
        side="source"
        connections={connections}
        connectionId={sourceId}
        onConnectionChange={(id) => { setSourceId(id); setSourcePath('/'); setSelectedPaths([]); }}
        path={sourcePath}
        onPathChange={setSourcePath}
        selectedPaths={selectedPaths}
        onToggle={toggleItem}
        isAdmin={user.role === 'ADMIN'}
        onTestConnection={runConnectionTest}
      />
      <ExplorerPanel
        title="Elige la carpeta de destino"
        side="destination"
        connections={destinationConnections}
        connectionId={destinationId}
        onConnectionChange={(id) => { setDestinationId(id); setDestinationPath('/'); }}
        path={destinationPath}
        onPathChange={setDestinationPath}
        isAdmin={user.role === 'ADMIN'}
        onTestConnection={runConnectionTest}
      />
    </Box>
  );

  return (
    <Box className="app-shell">
      <AppBar position="sticky" elevation={0} className="topbar">
        <Toolbar className="topbar-inner">
          <Stack className="topbar-brand" direction="row" alignItems="center" spacing={1.2}>
            <Box className="small-brand-mark"><CloudNetworkMark /></Box>
            <Box className="topbar-wordmark">
              <Typography variant="subtitle1" fontWeight={750}>Cloud Transfer Manager</Typography>
              <Typography variant="overline">COPIA ENTRE NUBES</Typography>
            </Box>
          </Stack>
          <Tabs
            className="header-navigation"
            value={workspaceSection}
            onChange={(_event, value: 'explorer' | 'connections' | 'private' | 'scheduled' | 'documentation') => setWorkspaceSection(value)}
            aria-label="Secciones principales"
          >
            <Tab value="explorer" label="Explorador" />
            <Tab value="private" label="Servicios privados" />
            <Tab value="scheduled" label="Programaciones" />
            <Tab value="documentation" label="Documentación" />
            {user.role === 'ADMIN' && <Tab value="connections" label="Conexiones" />}
          </Tabs>
          <Stack className="topbar-actions" direction="row" alignItems="center" spacing={1.5}>
            <IconButton
              className="background-transfers-trigger"
              aria-label={`${backgroundTransferCount} transferencias en segundo plano`}
              aria-haspopup="dialog"
              aria-expanded={Boolean(backgroundPopoverAnchor)}
              onClick={(event) => setBackgroundPopoverAnchor(event.currentTarget)}
              size="small"
            >
              <Badge
                badgeContent={backgroundTransferCount}
                color="primary"
                max={99}
                invisible={backgroundTransferCount === 0}
                overlap="circular"
              >
                <InterfaceIcon name="clock" />
              </Badge>
            </IconButton>
            <Button
              id="account-menu-trigger"
              className="account-menu-trigger"
              color="inherit"
              aria-label={`Menú de cuenta de ${user.username}`}
              aria-haspopup="menu"
              aria-expanded={Boolean(accountMenuAnchor)}
              aria-controls={accountMenuAnchor ? 'account-menu' : undefined}
              onClick={(event) => setAccountMenuAnchor(event.currentTarget)}
            >
              <span className="account-avatar">{user.username.slice(0, 1).toUpperCase()}</span>
              <span className="account-menu-name">{user.username}</span>
              <span className="account-menu-chevron" aria-hidden="true" />
            </Button>
          </Stack>
        </Toolbar>
      </AppBar>
      <Menu
        id="account-menu"
        anchorEl={accountMenuAnchor}
        open={Boolean(accountMenuAnchor)}
        onClose={() => setAccountMenuAnchor(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }}
        transformOrigin={{ vertical: 'top', horizontal: 'right' }}
        transitionDuration={{ enter: 180, exit: 120 }}
        slotProps={{
          paper: { className: 'account-menu' },
          list: { 'aria-labelledby': 'account-menu-trigger' },
        }}
      >
        {user.role === 'ADMIN' && (
          <MenuItem className="account-menu-item" onClick={() => { setAccountMenuAnchor(null); setAdminDialogOpen(true); }}>
            <ListItemIcon><InterfaceIcon name="settings" /></ListItemIcon>
            Administración
          </MenuItem>
        )}
        <MenuItem className="account-menu-item" onClick={() => { setAccountMenuAnchor(null); setPasswordDialogOpen(true); }}>
          <ListItemIcon><InterfaceIcon name="key" /></ListItemIcon>
          Cambiar contraseña
        </MenuItem>
        <Divider className="account-menu-divider" />
        <MenuItem className="account-menu-item account-menu-logout" onClick={() => { setAccountMenuAnchor(null); onLogout(); }}>
          <ListItemIcon><InterfaceIcon name="logout" /></ListItemIcon>
          Cerrar sesión
        </MenuItem>
      </Menu>
      <Popover
        open={Boolean(backgroundPopoverAnchor)}
        anchorEl={backgroundPopoverAnchor}
        onClose={() => setBackgroundPopoverAnchor(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }}
        transformOrigin={{ vertical: 'top', horizontal: 'right' }}
        transitionDuration={{ enter: 180, exit: 140 }}
        slotProps={{ paper: { className: 'background-transfers-popover', role: 'dialog', 'aria-label': 'Transferencias en segundo plano' } }}
      >
        <Box className="background-transfers-heading">
          <Box>
            <Typography variant="overline" color="text.secondary">TRABAJO EN SEGUNDO PLANO</Typography>
            <Typography variant="subtitle1" fontWeight={750}>Transferencias activas</Typography>
          </Box>
          <Typography variant="caption" color="text.secondary">
            {backgroundTransferCount} {backgroundTransferCount === 1 ? 'en curso o en espera' : 'en curso o en espera'}
          </Typography>
        </Box>
        {backgroundTransfersQuery.isLoading && <LinearProgress />}
        {backgroundTransfersQuery.isError && (
          <Alert severity="error" sx={{ m: 1.5 }}>No se pudo actualizar el estado de las transferencias.</Alert>
        )}
        <Stack className="background-transfer-items" spacing={0.75}>
          {backgroundTransfers.map((transfer) => {
            const waitLabel = transferWaitReasonLabel(transfer.wait_reason);
            const isActive = streamingTransferStatuses.includes(transfer.status);
            return (
              <Box className="background-transfer-item" key={transfer.id}>
                <Box className="background-transfer-item-heading">
                  <Box className="background-transfer-item-copy">
                    <Typography variant="body2" fontWeight={700} title={transfer.current_item ?? transfer.public_id}>
                      {transfer.current_item ?? transfer.public_id}
                    </Typography>
                    {transfer.current_item && (
                      <Typography variant="caption" color="text.secondary">{transfer.public_id}</Typography>
                    )}
                  </Box>
                  <Chip size="small" className="history-status" data-status={transfer.status} label={transferStatusLabel(transfer.status)} />
                </Box>
                {waitLabel ? (
                  <Typography className="background-transfer-detail" variant="caption" color="text.secondary">
                    {waitLabel}
                  </Typography>
                ) : isActive ? (
                  <>
                    <Box className="background-transfer-progress-meta">
                      <Typography variant="caption" color="text.secondary">
                        {formatBytes(transfer.bytes_transferred)} de {formatBytes(transfer.bytes_total)}
                      </Typography>
                      <Typography variant="caption" color="text.secondary">{formatProgressPercent(transfer.progress_percent)}%</Typography>
                    </Box>
                    <LinearProgress
                      variant={transfer.bytes_total ? 'determinate' : 'indeterminate'}
                      value={transfer.progress_percent}
                    />
                  </>
                ) : (
                  <Typography className="background-transfer-detail" variant="caption" color="text.secondary">
                    {formatRequestedAt(transfer.scheduled_at) ?? 'Pendiente de ejecución'}
                  </Typography>
                )}
              </Box>
            );
          })}
          {!backgroundTransfersQuery.isLoading && backgroundTransfers.length === 0 && (
            <Typography variant="body2" color="text.secondary" sx={{ px: 1.5, py: 2 }}>
              No hay transferencias en segundo plano.
            </Typography>
          )}
        </Stack>
        <Box className="background-transfers-footer">
          <Button
            size="small"
            onClick={() => {
              setBackgroundPopoverAnchor(null);
              setWorkspaceSection('scheduled');
            }}
          >
            Administrar programaciones
          </Button>
        </Box>
      </Popover>
      <Container maxWidth="xl" className="workspace-container">
        <Box className="page-heading">
          <Box>
            <Typography variant="overline" color="primary.main">
              {workspaceSection === 'connections'
                ? 'ADMINISTRACIÓN DE CONEXIONES'
                : workspaceSection === 'private'
                  ? 'ORÍGENES DE RED PRIVADA'
                  : workspaceSection === 'scheduled'
                    ? 'COLA DE TRANSFERENCIAS'
                    : workspaceSection === 'documentation' ? 'GUÍA DE USO' : 'CENTRO DE TRANSFERENCIAS'}
            </Typography>
            <Typography variant="h4" component="h1">
              {workspaceSection === 'connections'
                ? 'Conexiones'
                : workspaceSection === 'private'
                  ? 'Servicios privados'
                  : workspaceSection === 'scheduled'
                    ? 'Programaciones'
                    : workspaceSection === 'documentation' ? 'Documentación' : 'Explorador de archivos'}
            </Typography>
            <Typography color="text.secondary" sx={{ mt: 0.6 }}>
              {workspaceSection === 'connections'
                ? 'Administra las conexiones cloud disponibles para transferir archivos.'
                : workspaceSection === 'private'
                  ? 'Explora carpetas del NAS accesible desde la red de Cloud Transfer Manager.'
                  : workspaceSection === 'scheduled'
                    ? 'Revisa horarios, dependencias y acciones de las transferencias programadas.'
                    : workspaceSection === 'documentation'
                      ? 'Conoce cómo transferir archivos y cómo se administran la cola, la capacidad y el progreso.'
                      : 'Copia archivos entre tus servicios cloud y los NAS conectados.'}
            </Typography>
          </Box>
        </Box>

        {notice && <Alert severity={noticeSeverity} sx={{ mb: 2 }}>{notice}</Alert>}
        {transferFailure && <Alert severity="error" sx={{ mb: 2 }}>{transferFailure.message}</Alert>}
        {workspaceSection === 'connections' && user.role === 'ADMIN' ? (
          <ConnectionManagementSection
            connections={connections.filter((connection) => connection.provider_type !== 'SMB')}
            loading={connectionsQuery.isLoading}
            loadError={connectionsQuery.isError}
            onAdd={() => {
              setConnectionDialogProvider('MEGA');
              setConnectionDialogOpen(true);
            }}
            onRefresh={() => { void connectionsQuery.refetch(); }}
            onArchive={archiveConnection}
          />
        ) : workspaceSection === 'private' ? (
          <PrivateServicesSection
            connections={connections.filter((connection) => connection.provider_type === 'SMB')}
            loading={connectionsQuery.isLoading}
            isAdmin={user.role === 'ADMIN'}
            onAdd={() => {
              setConnectionDialogProvider('SMB');
              setConnectionDialogOpen(true);
            }}
            onExplore={(connection) => {
              setSourceId(connection.id);
              setSourcePath('/');
              setSelectedPaths([]);
              setWorkspaceSection('explorer');
            }}
            onArchive={archiveConnection}
          />
        ) : workspaceSection === 'scheduled' ? (
          <Paper className="scheduled-transfer-list" elevation={0}>
            <Box className="scheduled-list-heading">
              <Box>
                <Typography variant="overline" color="text.secondary">COLA Y TRABAJOS ACTIVOS</Typography>
                <Typography variant="h6">Transferencias programadas</Typography>
              </Box>
              <Button
                size="small"
                onClick={() => {
                  void pendingTransfersQuery.refetch();
                  void backgroundTransfersQuery.refetch();
                  void scheduledBackgroundTransfersQuery.refetch();
                }}
                startIcon={<InterfaceIcon name="refresh" />}
              >
                Actualizar
              </Button>
            </Box>
            {(pendingTransfersQuery.isLoading || backgroundTransfersQuery.isLoading || scheduledBackgroundTransfersQuery.isLoading) && <LinearProgress />}
            {(pendingTransfersQuery.isError || backgroundTransfersQuery.isError || scheduledBackgroundTransfersQuery.isError) && (
              <Alert severity="error" sx={{ mt: 2 }}>No se pudieron cargar las transferencias programadas o en curso.</Alert>
            )}
            {programmedTransfers.map((transfer) => {
              const parent = [...historyTransfers, ...backgroundTransfers, ...allScheduledBackgroundTransfers]
                .find((item) => item.id === transfer.depends_on_transfer_id);
              const source = connectionById.get(transfer.source_connection_id ?? '');
              const destination = connectionById.get(transfer.destination_connection_id);
              const editable = (transfer.can_edit_schedule ?? ['SCHEDULED', 'QUEUED'].includes(transfer.status))
                && transfer.wait_reason !== 'BLOCKED_CONNECTION_CHANGED';
              return (
                <Box className="scheduled-transfer-row" key={transfer.id}>
                  <Box className="scheduled-transfer-main">
                    <Box className="history-title-line">
                      <Typography fontWeight={700}>{transfer.public_id}</Typography>
                      <Chip size="small" className="history-status" data-status={transfer.status} label={transferStatusLabel(transfer.status)} />
                    </Box>
                    <Typography variant="body2" color="text.secondary">
                      {source?.name ?? 'Origen'} → {destination?.name ?? 'Destino'}
                    </Typography>
                    <Box className="scheduled-transfer-meta">
                      <Typography variant="caption" color="text.secondary">
                        <span>Inicio</span>{formatRequestedAt(transfer.scheduled_at) ?? 'Al liberarse la cola'}
                      </Typography>
                      <Typography variant="caption" color="text.secondary">
                        <span>Modo</span>{transfer.execution_mode === 'EXCLUSIVE' ? 'Exclusiva' : 'Automática'}
                      </Typography>
                      {transfer.depends_on_transfer_id && (
                        <Typography variant="caption" color="text.secondary">
                          <span>Depende de</span>{parent?.public_id ?? transfer.depends_on_transfer_id.slice(0, 8)}
                        </Typography>
                      )}
                    </Box>
                    {transferWaitReasonLabel(transfer.wait_reason) && (
                      <Typography className="scheduled-wait-reason" variant="caption" color="text.secondary">
                        {transferWaitReasonLabel(transfer.wait_reason)}
                      </Typography>
                    )}
                  </Box>
                  <Stack className="scheduled-transfer-actions" direction="row" spacing={0.75}>
                    {editable && (
                      <Button size="small" onClick={() => openScheduleDialog(transfer)}>
                        Reprogramar
                      </Button>
                    )}
                    {!terminalTransferStatuses.includes(transfer.status) && transfer.status !== 'CANCELLING' && (
                      <Button size="small" color="error" disabled={busy} onClick={() => void cancelTransfer(transfer)}>
                        Cancelar
                      </Button>
                    )}
                  </Stack>
                </Box>
              );
            })}
            {pendingTransfersQuery.hasNextPage && (
              <Button
                size="small"
                onClick={() => void pendingTransfersQuery.fetchNextPage()}
                disabled={pendingTransfersQuery.isFetchingNextPage}
                sx={{ mt: 1 }}
              >
                {pendingTransfersQuery.isFetchingNextPage ? 'Cargando…' : 'Cargar más pendientes'}
              </Button>
            )}
            {scheduledBackgroundTransfersQuery.hasNextPage && (
              <Button
                size="small"
                onClick={() => void scheduledBackgroundTransfersQuery.fetchNextPage()}
                disabled={scheduledBackgroundTransfersQuery.isFetchingNextPage}
                sx={{ mt: 1, ml: 1 }}
              >
                {scheduledBackgroundTransfersQuery.isFetchingNextPage ? 'Cargando…' : 'Cargar más trabajos activos'}
              </Button>
            )}
            {!pendingTransfersQuery.isLoading && !scheduledBackgroundTransfersQuery.isLoading && programmedTransfers.length === 0 && (
              <Typography color="text.secondary" sx={{ p: 3 }}>
                No hay transferencias programadas, en espera o en ejecución.
              </Typography>
            )}
          </Paper>
        ) : workspaceSection === 'documentation' ? (
          <DocumentationSection />
        ) : (
          <>
            {connectionsQuery.isError && <Alert severity="error" sx={{ mb: 2 }}>No se pudieron cargar las conexiones.</Alert>}
            {connections.length < 2 && (
              <Alert severity="info" sx={{ mb: 2 }}>
                Se necesitan dos conexiones autorizadas para comenzar. Un administrador puede configurar MEGA, Google Drive o un adaptador simulado.
              </Alert>
            )}

            <Paper className="mobile-steps" elevation={0}>
              <Tabs value={mobileStep} onChange={(_event, value: number) => setMobileStep(value)} variant="fullWidth">
                <Tab label="1 · Origen" />
                <Tab label="2 · Destino" />
              </Tabs>
            </Paper>
            <Box className={`mobile-panel mobile-panel-${mobileStep}`}>{panels}</Box>

        <Paper className="transfer-action" elevation={0} role="group" aria-label="Resumen de transferencia">
          <Box className="transfer-action-overview">
            <Box className="transfer-symbol" role="img" aria-label="Transferencia de archivos">
              <InterfaceIcon name="transfer" />
            </Box>
            <Box className="transfer-action-copy">
              <Box className="transfer-action-heading">
                <Typography variant="h6" component="h2">
                  {canTransfer ? 'Listo para transferir' : 'Prepara tu transferencia'}
                </Typography>
                <Chip
                  className="transfer-selection-chip"
                  size="small"
                  label={selectedPaths.length === 1
                    ? '1 elemento'
                    : `${selectedPaths.length} elementos`}
                />
              </Box>
              <Typography variant="body2" color="text.secondary" className="transfer-selection-summary">
                {selectedPaths.length === 0
                  ? 'Selecciona elementos en el origen para preparar la copia.'
                  : canTransfer
                    ? `${selectedPaths.length} ${selectedPaths.length === 1 ? 'elemento listo' : 'elementos listos'} para copiar`
                    : sourceId === destinationId
                      ? 'Elige una conexión distinta como destino.'
                      : 'Selecciona una conexión de destino para habilitar la copia.'}
              </Typography>
              {selectedPaths.length > 100 && (
                <Alert severity="warning" sx={{ mt: 1 }}>
                  Selecciona como máximo 100 rutas por transferencia. Divide la selección en varias copias.
                </Alert>
              )}
              <Box className="transfer-route" aria-label="Ruta de transferencia">
                <Box className="transfer-route-point">
                  <Typography variant="overline">ORIGEN</Typography>
                  <Box className="transfer-route-connection">
                    <ProviderLogo provider={sourceConnection?.provider_type ?? ''} decorative={false} />
                    <Typography variant="body2" title={sourceConnection?.name ?? 'Selecciona un origen'}>
                      {sourceConnection?.name ?? 'Selecciona un origen'}
                    </Typography>
                  </Box>
                </Box>
                <span className="transfer-route-arrow" aria-hidden="true">→</span>
                <Box className="transfer-route-point">
                  <Typography variant="overline">DESTINO</Typography>
                  <Box className="transfer-route-connection">
                    <ProviderLogo provider={destinationConnection?.provider_type ?? ''} decorative={false} />
                    <Typography variant="body2" title={destinationConnection?.name ?? 'Selecciona un destino'}>
                      {destinationConnection?.name ?? 'Selecciona un destino'}
                    </Typography>
                  </Box>
                  <Typography variant="caption" className="transfer-destination-path" title={destinationPath}>
                    <InterfaceIcon name="folder" />
                    {destinationPath === '/' ? 'Carpeta raíz' : destinationPath}
                  </Typography>
                </Box>
              </Box>
            </Box>
          </Box>
          <Stack className="transfer-options" spacing={0.75}>
            <FormControl size="small" fullWidth>
              <InputLabel id="conflict-policy-label">Si ya existe</InputLabel>
              <Select
                labelId="conflict-policy-label"
                label="Si ya existe"
                value={conflictPolicy}
                onChange={(event) => setConflictPolicy(event.target.value as typeof conflictPolicy)}
              >
                <MenuItem value="OVERWRITE_IF_DIFFERENT">Reemplazar si cambió</MenuItem>
                <MenuItem value="SKIP">Omitir existentes</MenuItem>
              </Select>
            </FormControl>
            <FormControlLabel
              control={<Checkbox checked={sendEmailNotification} onChange={(_event, checked) => setSendEmailNotification(checked)} size="small" />}
              label={<Typography variant="caption">Avisarme por correo al terminar</Typography>}
            />
          </Stack>
          <Stack className="transfer-submit-actions" spacing={0.75}>
            <Button
              className="transfer-submit"
              variant="contained"
              size="large"
              startIcon={<InterfaceIcon name="copy" />}
              disabled={!canTransfer || busy}
              onClick={startTransfer}
            >
              {busy ? <CircularProgress size={22} color="inherit" /> : 'Iniciar transferencia'}
            </Button>
            <Button
              className="transfer-schedule-submit"
              variant="outlined"
              size="small"
              startIcon={<InterfaceIcon name="clock" />}
              disabled={!canTransfer || busy}
              onClick={() => openScheduleDialog()}
            >
              Programar transferencia
            </Button>
          </Stack>
        </Paper>

        {liveTransfers.map((transfer) => (
          <Card className="progress-card" variant="outlined" key={transfer.id}>
            <CardContent>
              <Stack direction="row" justifyContent="space-between" alignItems="center" spacing={2}>
                <Box>
                  <Typography variant="overline" color="text.secondary">TRANSFERENCIA ACTIVA · {transfer.public_id}</Typography>
                  <Typography fontWeight={700}>{transfer.current_item ?? transferStatusLabel(transfer.status)}</Typography>
                </Box>
                <Typography fontWeight={700}>{formatProgressPercent(transfer.progress_percent, 1)}%</Typography>
              </Stack>
              {transfer.status === 'RETRYING' && (
                <Typography variant="caption" color="text.secondary" display="block" sx={{ mt: 0.5 }}>
                  Reintento automático en curso; próximo intento {transfer.attempt_count ? transfer.attempt_count + 1 : 2}.
                </Typography>
              )}
              <LinearProgress variant={transfer.bytes_total ? 'determinate' : 'indeterminate'} value={transfer.progress_percent} sx={{ mt: 2, mb: 1 }} />
              <Typography variant="caption" color="text.secondary">
                {formatBytes(transfer.bytes_transferred)} de {formatBytes(transfer.bytes_total)} · {transfer.files_completed}/{transfer.files_total} archivos
              </Typography>
              <Typography variant="caption" color="text.secondary" display="block">
                {transfer.current_speed_bps === null ? 'Velocidad calculándose' : `${formatBytes(transfer.current_speed_bps)}/s`}
                {transfer.estimated_remaining_seconds === null || transfer.estimated_remaining_seconds === undefined
                  ? ''
                  : ` · ETA ${transfer.estimated_remaining_seconds}s`}
              </Typography>
              {!['COMPLETED', 'COMPLETED_WITH_WARNINGS', 'FAILED', 'CANCELLED', 'CANCELLING'].includes(transfer.status) && (
                <Button color="error" size="small" sx={{ mt: 1 }} onClick={() => void cancelTransfer(transfer)} disabled={busy}>Cancelar transferencia</Button>
              )}
            </CardContent>
          </Card>
        ))}

        {reportTransferId && (
          <Card className="progress-card" variant="outlined" aria-label="Resultado de migración SMB">
            <CardContent>
              <Stack direction="row" alignItems="center" justifyContent="space-between" spacing={2}>
                <Box>
                  <Typography variant="overline" color="text.secondary">INVENTARIO Y RESULTADOS</Typography>
                  <Typography variant="h6">
                    {historyTransfers.find((transfer) => transfer.id === reportTransferId)?.public_id}
                  </Typography>
                </Box>
                <Button size="small" onClick={() => setReportTransferId(null)}>Cerrar</Button>
              </Stack>
              {transferManifestQuery.data?.pages[0]?.counts && (
                <Typography variant="caption" color="text.secondary" display="block" sx={{ mt: 1 }}>
                  {transferManifestQuery.data.pages[0].counts.copied} copiados · {transferManifestQuery.data.pages[0].counts.skipped} omitidos · {transferManifestQuery.data.pages[0].counts.failed} con error
                </Typography>
              )}
              {transferManifestQuery.isLoading && <LinearProgress sx={{ mt: 1 }} />}
              {transferManifestQuery.isError && <Alert severity="error" sx={{ mt: 1 }}>No se pudo cargar el resultado de la migración.</Alert>}
              <Stack spacing={0.75} sx={{ mt: 1.5 }}>
                {transferManifestQuery.data?.pages.flatMap((page) => page.items).map((item) => (
                  <Box key={item.id} className="history-row">
                    <Chip size="small" label={item.status === 'DIRECTORY' ? 'Carpeta' : item.status} color={item.status === 'FAILED' ? 'error' : item.status === 'COPIED' ? 'success' : 'default'} />
                    <Box className="history-label">
                      <Typography variant="body2" title={item.source_path}>{item.source_path}</Typography>
                      <Typography variant="caption" color="text.secondary" title={item.destination_path}>{item.destination_path}</Typography>
                    </Box>
                    {item.error_code && <Typography variant="caption" color="error">{item.error_code}</Typography>}
                  </Box>
                ))}
              </Stack>
              {transferManifestQuery.hasNextPage && (
                <Button
                  size="small"
                  sx={{ mt: 1 }}
                  disabled={transferManifestQuery.isFetchingNextPage}
                  onClick={() => void transferManifestQuery.fetchNextPage()}
                >
                  {transferManifestQuery.isFetchingNextPage ? <CircularProgress size={18} /> : 'Cargar más resultados'}
                </Button>
              )}
            </CardContent>
          </Card>
        )}

        <Box className="history-heading">
          <Box>
            <Typography variant="overline" color="text.secondary">ACTIVIDAD</Typography>
            <Typography variant="h6">Historial reciente</Typography>
            <Typography variant="caption" color="text.secondary">Fecha y hora en tu zona local</Typography>
          </Box>
          <Button size="small" onClick={() => void transfersQuery.refetch()} startIcon={<InterfaceIcon name="refresh" />}>Actualizar</Button>
        </Box>
        <Paper className="history-card" elevation={0}>
          {transfersQuery.isLoading && <LinearProgress />}
          {historyTransfers.slice(0, 8).map((transfer) => {
            const sourceConnection = transfer.source_connection_id
              ? connectionById.get(transfer.source_connection_id)
              : undefined;
            const destinationConnection = connectionById.get(transfer.destination_connection_id);
            return (
            <Box className="history-row" key={transfer.id}>
              <Box className="history-status-dot" data-status={transfer.status} />
              <Box className="history-label">
                <Box className="history-title-line">
                  <Typography fontWeight={700}>{transfer.public_id}</Typography>
                  <Chip size="small" className="history-status" data-status={transfer.status} label={transferStatusLabel(transfer.status)} />
                </Box>
                <Box className="history-route" aria-label="Ruta de transferencia">
                  <ProviderLogo provider={sourceConnection?.provider_type ?? ''} />
                  <span>{sourceConnection?.name ?? (transfer.source_connection_id ? 'Conexión no disponible' : 'Origen')}</span>
                  <span className="history-route-arrow" aria-hidden="true">→</span>
                  <ProviderLogo provider={destinationConnection?.provider_type ?? ''} />
                  <span>{destinationConnection?.name ?? 'Conexión no disponible'}</span>
                </Box>
                <Typography variant="caption" color="text.secondary" className="history-counts">
                  {sourceConnection?.provider_type === 'SMB'
                    ? `${transfer.manifest_summary?.copied ?? 0} copiados · ${transfer.manifest_summary?.failed ?? 0} con error · ${transfer.manifest_summary?.skipped ?? 0} omitidos · ${formatBytes(transfer.bytes_transferred)} de ${formatBytes(transfer.bytes_total)}`
                    : `${transfer.files_completed} de ${transfer.files_total} archivos · ${formatBytes(transfer.bytes_transferred)} de ${formatBytes(transfer.bytes_total)}`}
                </Typography>
                <Box className="history-meta">
                  {formatRequestedAt(transfer.created_at) && (
                    <Typography component="time" dateTime={transfer.created_at ?? undefined} variant="caption" color="text.secondary">
                      <span>Solicitada</span>{formatRequestedAt(transfer.created_at)}
                    </Typography>
                  )}
                  <Typography variant="caption" color="text.secondary"><span>Tiempo de transferencia</span>{formatTransferDuration(transfer)}</Typography>
                </Box>
              </Box>
              <Box className="history-progress" aria-label={`Avance ${formatProgressPercent(transfer.progress_percent)} por ciento`}>
                <Typography variant="body2" fontWeight={700}>{formatProgressPercent(transfer.progress_percent)}%</Typography>
                <Box className="history-progress-track"><Box style={{ width: `${Math.max(0, Math.min(100, transfer.progress_percent))}%` }} /></Box>
              </Box>
              {sourceConnection?.provider_type === 'SMB' && (
                <Stack spacing={0.5} alignItems="flex-start">
                  <Button
                    size="small"
                    onClick={() => setReportTransferId(transfer.id)}
                    aria-label={`Ver archivos de ${transfer.public_id}`}
                  >
                    Ver archivos
                  </Button>
                  {['FAILED', 'CANCELLED'].includes(transfer.status) && (
                    <Button
                      size="small"
                      disabled={busy}
                      onClick={() => void resumeTransfer(transfer)}
                      aria-label={`Reanudar transferencia ${transfer.public_id}`}
                    >
                      Reanudar
                    </Button>
                  )}
                </Stack>
              )}
            </Box>
            );
          })}
          {!transfersQuery.isLoading && historyTransfers.length === 0 && (
            <Typography color="text.secondary" sx={{ p: 3 }}>Aún no hay transferencias. Las nuevas aparecerán aquí.</Typography>
          )}
          {latestTransfer && latestTransfer.status === 'RUNNING' && (
            <Button size="small" onClick={() => setActiveTransfer(latestTransfer)}>Ver progreso en vivo</Button>
          )}
        </Paper>
            <Typography className="footer-note" variant="caption">Operaciones de copia protegidas · Actividad asociada a tu usuario</Typography>
          </>
        )}
      </Container>
      <Dialog
        open={scheduleDialogOpen}
        onClose={() => setScheduleDialogOpen(false)}
        fullWidth
        maxWidth="sm"
        aria-labelledby="schedule-transfer-title"
      >
        <DialogTitle id="schedule-transfer-title">
          {editingScheduleId ? 'Reprogramar transferencia' : 'Programar transferencia'}
        </DialogTitle>
        <DialogContent>
          <Stack spacing={2} sx={{ pt: 1 }}>
            <Typography variant="body2" color="text.secondary">
              {editingScheduleId
                ? 'Actualiza cuándo debe ejecutarse y qué trabajos deben terminar antes.'
                : `${selectedPaths.length} ${selectedPaths.length === 1 ? 'elemento' : 'elementos'} · ${sourceConnection?.name ?? 'Origen'} → ${destinationConnection?.name ?? 'Destino'}`}
            </Typography>
            <TextField
              label="Fecha y hora"
              type="datetime-local"
              value={scheduleDateTime}
              onChange={(event) => setScheduleDateTime(event.target.value)}
              fullWidth
              required
              InputLabelProps={{ shrink: true }}
              inputProps={{ 'aria-label': 'Fecha y hora' }}
              helperText={`Zona horaria: ${Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'} · ${scheduleDateTime ? localTimeOffsetLabel(scheduleDateTime, Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC') : ''}`}
            />
            <FormControl fullWidth size="small">
              <InputLabel id="schedule-mode-label">Modo de ejecución</InputLabel>
              <Select
                labelId="schedule-mode-label"
                label="Modo de ejecución"
                value={scheduleMode}
                onChange={(event) => setScheduleMode(event.target.value as 'AUTO' | 'EXCLUSIVE')}
              >
                <MenuItem value="AUTO">Automática · usar capacidad disponible</MenuItem>
                <MenuItem value="EXCLUSIVE">Exclusiva · esperar a que terminen las demás</MenuItem>
              </Select>
              <FormHelperText>
                {scheduleMode === 'EXCLUSIVE'
                  ? 'Al llegar su turno, no se iniciarán otras copias hasta que termine.'
                  : 'Puede ejecutarse en paralelo con trabajos compatibles.'}
              </FormHelperText>
            </FormControl>
            <FormControl fullWidth size="small">
              <InputLabel id="schedule-parent-label">Transferencia padre (opcional)</InputLabel>
              <Select
                labelId="schedule-parent-label"
                label="Transferencia padre (opcional)"
                value={scheduleParentId}
                onChange={(event) => setScheduleParentId(event.target.value)}
              >
                <MenuItem value="">Sin dependencia</MenuItem>
                {scheduleParentOptions
                  .filter((transfer) => transfer.id !== editingScheduleId)
                  .map((transfer) => (
                    <MenuItem key={transfer.id} value={transfer.id}>
                      {transfer.public_id} · {transferStatusLabel(transfer.status)}
                    </MenuItem>
                  ))}
                {scheduleParentId && !scheduleParentOptions.some((transfer) => transfer.id === scheduleParentId) && (
                  <MenuItem value={scheduleParentId}>{scheduleParentId.slice(0, 8)}</MenuItem>
                )}
              </Select>
              <FormHelperText>
                Se habilita después de una finalización correcta, incluso si tuvo avisos.
              </FormHelperText>
            </FormControl>
            {scheduleError && <Alert severity="error">{scheduleError}</Alert>}
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setScheduleDialogOpen(false)} disabled={busy}>Cancelar</Button>
          <Button variant="contained" onClick={() => void saveScheduledTransfer()} disabled={busy}>
            {busy ? <CircularProgress size={20} color="inherit" /> : 'Confirmar programación'}
          </Button>
        </DialogActions>
      </Dialog>
      <ConnectionDialog
        open={connectionDialogOpen}
        initialProvider={connectionDialogProvider}
        onClose={() => setConnectionDialogOpen(false)}
        onCreated={(created) => {
          setNotice(created.status === 'ACTIVE'
            ? 'Conexión simulada lista para usar.'
            : 'Conexión guardada de forma inactiva. Selecciónala y ejecuta “Probar conexión”.');
          setNoticeSeverity(created.status === 'ACTIVE' ? 'success' : 'warning');
          void queryClient.invalidateQueries({ queryKey: ['connections'] });
        }}
      />
      {user.role === 'ADMIN' && (
        <AdminDialog
          open={adminDialogOpen}
          onClose={() => setAdminDialogOpen(false)}
          currentUserId={user.id}
        />
      )}
      <ChangePasswordDialog
        open={passwordDialogOpen}
        canClose={!user.must_change_password}
        onClose={() => setPasswordDialogOpen(false)}
        onChanged={() => onUserUpdated({ must_change_password: false })}
      />
    </Box>
  );
}

function AppRoutes() {
  const [user, setUser] = useState<User | null>(null);
  const [checking, setChecking] = useState(true);
  const navigate = useNavigate();

  useEffect(() => {
    let current = true;
    apiRequest<User>('/api/v1/auth/me')
      .then((authenticated) => { if (current) setUser(authenticated); })
      .catch(() => { if (current) setUser(null); })
      .finally(() => { if (current) setChecking(false); });
    return () => { current = false; };
  }, []);

  const logout = async () => {
    try {
      await apiRequest('/api/v1/auth/logout', { method: 'POST' });
    } finally {
      setUser(null);
      navigate('/login', { replace: true });
    }
  };

  if (checking) {
    return <Box className="loading-shell"><CircularProgress /><Typography color="text.secondary">Verificando sesión…</Typography></Box>;
  }
  return (
    <Routes>
      <Route path="/login" element={user ? <Navigate to="/" replace /> : <LoginPage onLogin={setUser} />} />
      <Route path="/" element={user ? <TransferWorkspace user={user} onUserUpdated={(patch) => setUser({ ...user, ...patch })} onLogout={() => void logout()} /> : <Navigate to="/login" replace />} />
      <Route path="*" element={<Navigate to={user ? '/' : '/login'} replace />} />
    </Routes>
  );
}

export default function App() {
  const [queryClient] = useState(() => new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 5_000 } },
  }));

  return (
    <ThemeProvider theme={theme}>
      <QueryClientProvider client={queryClient}>
        <BrowserRouter><AppRoutes /></BrowserRouter>
      </QueryClientProvider>
    </ThemeProvider>
  );
}
