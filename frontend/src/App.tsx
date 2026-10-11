import { zodResolver } from '@hookform/resolvers/zod';
import {
  Alert,
  AppBar,
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
  FormControl,
  FormControlLabel,
  InputLabel,
  LinearProgress,
  MenuItem,
  Paper,
  Select,
  Stack,
  Tab,
  Tabs,
  TextField,
  Toolbar,
  Typography,
  createTheme,
  ThemeProvider,
} from '@mui/material';
import { QueryClient, QueryClientProvider, useInfiniteQuery, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useMemo, useState } from 'react';
import { Controller, useForm } from 'react-hook-form';
import { BrowserRouter, Navigate, Route, Routes, useNavigate } from 'react-router-dom';
import { z } from 'zod';
import { apiRequest, ApiError } from './api';
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
  destination_connection_id: string;
  destination_path: string;
  progress_percent: number;
  files_completed: number;
  files_total: number;
  bytes_transferred: number;
  bytes_total: number;
  current_item: string | null;
  average_speed_bps: number | null;
  current_speed_bps: number | null;
  estimated_remaining_seconds?: number | null;
};
const terminalTransferStatuses = ['COMPLETED', 'COMPLETED_WITH_WARNINGS', 'FAILED', 'CANCELLED'];
type LoginForm = { username_or_email: string; password: string };
type ConnectionForm = {
  name: string;
  provider_type: 'MOCK' | 'MEGA' | 'GOOGLE_DRIVE' | 'SEAFILE';
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
};
type PasswordForm = { current_password: string; new_password: string };

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
          <Box className="brand-mark" aria-hidden="true">CT</Box>
          <Typography variant="h6" component="h1" fontWeight={750}>Cloud Transfer Manager</Typography>
        </Stack>
        <Typography variant="overline" className="eyebrow">TRANSFERENCIAS PRIVADAS</Typography>
        <Typography variant="h3" className="login-title">Todo tu cloud, en un mismo lugar.</Typography>
        <Typography className="login-copy">
          Explora tus conexiones de almacenamiento y mueve información con trazabilidad de principio a fin.
        </Typography>
        <Box className="login-visual" aria-hidden="true">
          <Box className="cloud-node">MEGA</Box>
          <Box className="transfer-line"><span /><span /><span /></Box>
          <Box className="cloud-node drive-node">DRIVE</Box>
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
  provider_type: z.enum(['MOCK', 'MEGA', 'GOOGLE_DRIVE', 'SEAFILE']),
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
};

function ConnectionDialog({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (connection: Connection) => void;
}) {
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const { register, control, handleSubmit, reset, watch, formState: { errors } } = useForm<ConnectionForm>({
    resolver: zodResolver(connectionSchema),
    defaultValues: emptyConnectionForm,
  });
  const provider = watch('provider_type');

  const close = () => {
    reset(emptyConnectionForm);
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
      reset(emptyConnectionForm);
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
      <DialogTitle id="connection-dialog-title">Agregar conexión</DialogTitle>
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
                    <MenuItem value="MEGA">MEGA</MenuItem>
                    <MenuItem value="GOOGLE_DRIVE">Google Drive</MenuItem>
                    <MenuItem value="SEAFILE">Seafile</MenuItem>
                    <MenuItem value="MOCK">Simulado (pruebas)</MenuItem>
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
      <DialogTitle id="password-dialog-title">Cambia tu contraseña</DialogTitle>
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
  const [selectedUser, setSelectedUser] = useState('');
  const [selectedConnection, setSelectedConnection] = useState('');
  const usersQuery = useQuery({
    queryKey: ['admin-users'],
    queryFn: () => apiRequest<{ items: ManagedUser[]; total_count: number }>('/api/v1/users'),
    enabled: open,
  });
  const connectionsQuery = useQuery({
    queryKey: ['connections'],
    queryFn: () => apiRequest<{ items: Connection[] }>('/api/v1/connections'),
    enabled: open,
  });
  const auditQuery = useQuery({
    queryKey: ['audit-events'],
    queryFn: () => apiRequest<{ items: Array<{ id: string; action: string; result: string; created_at: string }> }>('/api/v1/audit-events?limit=20'),
    enabled: open,
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

  const users = usersQuery.data?.items ?? [];
  const privateConnections = (connectionsQuery.data?.items ?? []).filter((connection) => !connection.is_global);

  return (
    <Dialog open={open} onClose={onClose} fullWidth maxWidth="md" aria-labelledby="admin-dialog-title">
      <DialogTitle id="admin-dialog-title">Administración</DialogTitle>
      <DialogContent>
        <Stack spacing={3}>
          <Box>
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

          <Box>
            <Typography variant="h6" sx={{ mb: 1 }}>Usuarios</Typography>
            <Paper variant="outlined" className="admin-list">
              {usersQuery.isLoading && <LinearProgress />}
              {users.map((managedUser) => (
                <Box className="admin-user-row" key={managedUser.id}>
                  <Box>
                    <Typography fontWeight={650}>{managedUser.username}</Typography>
                    <Typography variant="caption" color="text.secondary">{managedUser.email} · {managedUser.role}</Typography>
                  </Box>
                  {managedUser.is_active
                    ? <Button size="small" color="error" disabled={managedUser.id === currentUserId} onClick={() => void deactivate(managedUser.id)}>Desactivar</Button>
                    : <Typography variant="caption" color="text.secondary">Inactivo</Typography>}
                </Box>
              ))}
            </Paper>
          </Box>

          <Box>
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
                      {privateConnections.map((connection) => <MenuItem key={connection.id} value={connection.id}>{connection.name}</MenuItem>)}
                    </Select>
                  </FormControl>
                  <Button variant="outlined" disabled={!selectedUser || !selectedConnection} onClick={() => void grantConnection()}>Asignar</Button>
                </Stack>}
          </Box>

          <Box>
            <Typography variant="h6" sx={{ mb: 1 }}>Auditoría reciente</Typography>
            <Paper variant="outlined" className="admin-list">
              {(auditQuery.data?.items ?? []).slice(0, 5).map((event) => (
                <Box className="admin-user-row" key={event.id}>
                  <Typography variant="body2">{event.action.replaceAll('_', ' ')}</Typography>
                  <Typography variant="caption" color="text.secondary">{event.result}</Typography>
                </Box>
              ))}
            </Paper>
          </Box>
          {error && <Alert severity="error">{error}</Alert>}
        </Stack>
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
  const units = ['KB', 'MB', 'GB', 'TB'];
  let value = size / 1024;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value.toFixed(value >= 10 ? 1 : 2)} ${units[index]}`;
}

function providerLabel(provider: string): string {
  if (provider === 'GOOGLE_DRIVE') return 'Google Drive';
  if (provider === 'SEAFILE') return 'Seafile Community Edition';
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
  return Number.isNaN(date.getTime()) ? result : `${result} · ${date.toLocaleString()}`;
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
  const [archiveError, setArchiveError] = useState('');
  const [archiving, setArchiving] = useState(false);

  const requestArchive = (connection: Connection) => {
    setArchiveError('');
    setSelectedConnection(connection);
  };

  const closeArchiveDialog = () => {
    if (archiving) return;
    setSelectedConnection(null);
    setArchiveError('');
  };

  const confirmArchive = async () => {
    if (!selectedConnection || archiving) return;
    setArchiving(true);
    setArchiveError('');
    try {
      await onArchive(selectedConnection);
      setSelectedConnection(null);
    } catch (caught) {
      setArchiveError(caught instanceof Error ? caught.message : 'No se pudo archivar la conexión.');
    } finally {
      setArchiving(false);
    }
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
        <Button variant="contained" onClick={onAdd}>＋ Agregar conexión</Button>
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
              <Typography fontWeight={700}>{connection.name}</Typography>
              <Typography variant="caption" color="text.secondary">{providerLabel(connection.provider_type)}</Typography>
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
                aria-label={`Archivar ${connection.name}`}
                onClick={() => requestArchive(connection)}
              >
                Archivar
              </Button>
            </Box>
          </Box>
        ))}
      </Paper>

      <Dialog
        open={Boolean(selectedConnection)}
        onClose={closeArchiveDialog}
        fullWidth
        maxWidth="xs"
        aria-labelledby="archive-connection-title"
      >
        <DialogTitle id="archive-connection-title">Archivar conexión</DialogTitle>
        <DialogContent>
          <Stack spacing={2}>
            <Typography>
              ¿Quieres archivar <strong>{selectedConnection?.name}</strong>? Dejará de aparecer en el explorador y se quitarán sus credenciales guardadas.
            </Typography>
            <Typography variant="body2" color="text.secondary">
              Proveedor: {selectedConnection ? providerLabel(selectedConnection.provider_type) : ''}
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
              <MenuItem key={connection.id} value={connection.id}>{connection.name}</MenuItem>
            ))}
          </Select>
        </FormControl>
        {active && (
          <>
            <Stack direction="row" alignItems="center" justifyContent="space-between" spacing={1}>
              <Typography variant="caption" color="text.secondary">
                Estado: {active.status === 'ACTIVE' ? 'activa' : active.status === 'ERROR' ? 'requiere atención' : 'pendiente de prueba'}
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
                    startIcon={<span aria-hidden="true">{item.is_dir ? '▰' : '↗'}</span>}
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
  const connectionsQuery = useQuery({
    queryKey: ['connections'],
    queryFn: () => apiRequest<{ items: Connection[] }>('/api/v1/connections'),
  });
  const transfersQuery = useQuery({
    queryKey: ['transfers'],
    queryFn: () => apiRequest<{ items: Transfer[] }>('/api/v1/transfers'),
    refetchInterval: 3_000,
  });
  const connections = connectionsQuery.data?.items ?? [];
  const [sourceId, setSourceId] = useState('');
  const [destinationId, setDestinationId] = useState('');
  const [sourcePath, setSourcePath] = useState('/');
  const [destinationPath, setDestinationPath] = useState('/');
  const [selectedPaths, setSelectedPaths] = useState<string[]>([]);
  const [conflictPolicy, setConflictPolicy] = useState<'SKIP' | 'OVERWRITE_IF_DIFFERENT'>('OVERWRITE_IF_DIFFERENT');
  const [sendEmailNotification, setSendEmailNotification] = useState(false);
  const [mobileStep, setMobileStep] = useState(0);
  const [workspaceSection, setWorkspaceSection] = useState<'explorer' | 'connections'>('explorer');
  const [activeTransfer, setActiveTransfer] = useState<Transfer | null>(null);
  const [notice, setNotice] = useState('');
  const [noticeSeverity, setNoticeSeverity] = useState<'error' | 'info' | 'success' | 'warning'>('error');
  const [busy, setBusy] = useState(false);
  const [connectionDialogOpen, setConnectionDialogOpen] = useState(false);
  const [adminDialogOpen, setAdminDialogOpen] = useState(false);
  const [passwordDialogOpen, setPasswordDialogOpen] = useState(Boolean(user.must_change_password));
  const latestTransfer = useMemo(() => transfersQuery.data?.items[0], [transfersQuery.data]);

  useEffect(() => {
    if (!activeTransfer && latestTransfer && ['QUEUED', 'VALIDATING', 'RUNNING', 'RETRYING', 'CANCELLING'].includes(latestTransfer.status)) {
      setActiveTransfer(latestTransfer);
    }
  }, [activeTransfer, latestTransfer]);

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
    const destinationIsAvailable = connections.some((connection) => connection.id === destinationId);
    if (sourceId && !sourceIsAvailable) {
      setSourceId(connections[0]?.id ?? '');
      setSourcePath('/');
      setSelectedPaths([]);
    } else if (!sourceId && connections[0]) {
      setSourceId(connections[0].id);
    }
    if (destinationId && !destinationIsAvailable) {
      const fallback = connections.find((connection) => connection.id !== sourceId) ?? connections[0];
      setDestinationId(fallback?.id ?? '');
      setDestinationPath('/');
    } else if (!destinationId && connections.length > 1) {
      const currentSource = sourceId || connections[0].id;
      const fallback = connections.find((connection) => connection.id !== currentSource) ?? connections[1];
      setDestinationId(fallback.id);
    }
  }, [connections, destinationId, sourceId]);

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
    if (!activeTransfer || terminalTransferStatuses.includes(activeTransfer.status)) return;
    const stream = new EventSource(`/api/v1/transfers/${activeTransfer.id}/stream`);
    stream.onmessage = (event) => {
      const update = JSON.parse(event.data) as Transfer;
      setActiveTransfer(update);
      if (terminalTransferStatuses.includes(update.status)) {
        stream.close();
        void queryClient.invalidateQueries({ queryKey: ['transfers'] });
        void queryClient.invalidateQueries({
          queryKey: ['items', update.destination_connection_id, update.destination_path],
        });
      }
    };
    return () => stream.close();
  }, [activeTransfer?.id, activeTransfer?.status, queryClient]);

  const toggleItem = (item: StorageItem) => {
    setSelectedPaths((current) => current.includes(item.path)
      ? current.filter((path) => path !== item.path)
      : [...current, item.path]);
  };

  const startTransfer = async () => {
    setBusy(true);
    setNotice('');
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
      await queryClient.invalidateQueries({ queryKey: ['transfers'] });
    } catch (error) {
      setNotice(error instanceof Error ? error.message : 'No se pudo iniciar la transferencia.');
      setNoticeSeverity('error');
    } finally {
      setBusy(false);
    }
  };

  const cancelTransfer = async () => {
    if (!activeTransfer) return;
    setBusy(true);
    try {
      const updated = await apiRequest<Transfer>(`/api/v1/transfers/${activeTransfer.id}/cancel`, { method: 'POST' });
      setActiveTransfer(updated);
      await queryClient.invalidateQueries({ queryKey: ['transfers'] });
    } catch (error) {
      setNotice(error instanceof Error ? error.message : 'No se pudo cancelar la transferencia.');
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
        connections={connections}
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
            <Box className="small-brand-mark">CT</Box>
            <Typography variant="subtitle1" fontWeight={750}>Cloud Transfer Manager</Typography>
          </Stack>
          <Stack className="topbar-actions" direction="row" alignItems="center" spacing={1.5}>
            <Box className="user-badge"><span>{user.username.slice(0, 1).toUpperCase()}</span><Typography variant="body2">{user.username}</Typography></Box>
            {user.role === 'ADMIN' && <Button color="inherit" size="small" onClick={() => setAdminDialogOpen(true)}>Administración</Button>}
            <Button color="inherit" size="small" onClick={() => setPasswordDialogOpen(true)}>Cambiar contraseña</Button>
            <Button color="inherit" size="small" onClick={onLogout}>Cerrar sesión</Button>
          </Stack>
        </Toolbar>
      </AppBar>
      <Container maxWidth="xl" className="workspace-container">
        <Box className="page-heading">
          <Box>
            <Typography variant="overline" color="primary.main">
              {workspaceSection === 'connections' ? 'ADMINISTRACIÓN DE CONEXIONES' : 'CENTRO DE TRANSFERENCIAS'}
            </Typography>
            <Typography variant="h4" component="h1">
              {workspaceSection === 'connections' ? 'Conexiones' : 'Explorador de archivos'}
            </Typography>
            <Typography color="text.secondary" sx={{ mt: 0.6 }}>
              {workspaceSection === 'connections'
                ? 'Administra los proveedores disponibles para explorar y transferir archivos.'
                : 'Mueve archivos entre tus conexiones cloud con visibilidad en tiempo real.'}
            </Typography>
          </Box>
        </Box>

        {user.role === 'ADMIN' && (
          <Paper className="workspace-tabs" elevation={0}>
            <Tabs
              value={workspaceSection}
              onChange={(_event, value: 'explorer' | 'connections') => setWorkspaceSection(value)}
              aria-label="Secciones principales"
            >
              <Tab value="explorer" label="Explorador" />
              <Tab value="connections" label="Conexiones" />
            </Tabs>
          </Paper>
        )}

        {notice && <Alert severity={noticeSeverity} sx={{ mb: 2 }}>{notice}</Alert>}
        {workspaceSection === 'connections' && user.role === 'ADMIN' ? (
          <ConnectionManagementSection
            connections={connections}
            loading={connectionsQuery.isLoading}
            loadError={connectionsQuery.isError}
            onAdd={() => setConnectionDialogOpen(true)}
            onRefresh={() => { void connectionsQuery.refetch(); }}
            onArchive={archiveConnection}
          />
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

        <Paper className="transfer-action" elevation={0}>
          <Box className="transfer-action-copy">
            <Box className="copy-icon" aria-hidden="true">↗</Box>
            <Box>
              <Typography fontWeight={700}>Listo para copiar</Typography>
              <Typography variant="body2" color="text.secondary">
                {selectedPaths.length} elementos · destino {destinationPath}
              </Typography>
            </Box>
          </Box>
          <Stack className="transfer-options" spacing={0.5}>
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
          <Button
            variant="contained"
            size="large"
            disabled={!sourceId || !destinationId || sourceId === destinationId || selectedPaths.length === 0 || busy}
            onClick={startTransfer}
          >
            {busy ? <CircularProgress size={22} color="inherit" /> : 'Iniciar transferencia'}
          </Button>
        </Paper>

        {activeTransfer && (
          <Card className="progress-card" variant="outlined">
            <CardContent>
              <Stack direction="row" justifyContent="space-between" alignItems="center" spacing={2}>
                <Box>
                  <Typography variant="overline" color="text.secondary">TRANSFERENCIA ACTIVA · {activeTransfer.public_id}</Typography>
                  <Typography fontWeight={700}>{activeTransfer.current_item ?? activeTransfer.status}</Typography>
                </Box>
                <Typography fontWeight={700}>{activeTransfer.progress_percent.toFixed(1)}%</Typography>
              </Stack>
              <LinearProgress variant={activeTransfer.bytes_total ? 'determinate' : 'indeterminate'} value={activeTransfer.progress_percent} sx={{ mt: 2, mb: 1 }} />
              <Typography variant="caption" color="text.secondary">
                {formatBytes(activeTransfer.bytes_transferred)} de {formatBytes(activeTransfer.bytes_total)} · {activeTransfer.files_completed}/{activeTransfer.files_total} archivos
              </Typography>
              <Typography variant="caption" color="text.secondary" display="block">
                {activeTransfer.current_speed_bps === null ? 'Velocidad calculándose' : `${formatBytes(activeTransfer.current_speed_bps)}/s`}
                {activeTransfer.estimated_remaining_seconds === null || activeTransfer.estimated_remaining_seconds === undefined
                  ? ''
                  : ` · ETA ${activeTransfer.estimated_remaining_seconds}s`}
              </Typography>
              {!['COMPLETED', 'COMPLETED_WITH_WARNINGS', 'FAILED', 'CANCELLED', 'CANCELLING'].includes(activeTransfer.status) && (
                <Button color="error" size="small" sx={{ mt: 1 }} onClick={() => void cancelTransfer()} disabled={busy}>Cancelar transferencia</Button>
              )}
            </CardContent>
          </Card>
        )}

        <Box className="history-heading">
          <Box>
            <Typography variant="overline" color="text.secondary">ACTIVIDAD</Typography>
            <Typography variant="h6">Historial reciente</Typography>
          </Box>
          <Button size="small" onClick={() => void transfersQuery.refetch()}>Actualizar</Button>
        </Box>
        <Paper className="history-card" elevation={0}>
          {transfersQuery.isLoading && <LinearProgress />}
          {(transfersQuery.data?.items ?? []).slice(0, 8).map((transfer) => (
            <Box className="history-row" key={transfer.id}>
              <Box className="history-status-dot" data-status={transfer.status} />
              <Box className="history-label">
                <Typography fontWeight={650}>{transfer.public_id}</Typography>
                <Typography variant="caption" color="text.secondary">{transfer.files_completed} archivos · {formatBytes(transfer.bytes_transferred)}</Typography>
              </Box>
              <Typography className="history-status" variant="body2">{transfer.status.replaceAll('_', ' ')}</Typography>
              <Typography variant="body2" color="text.secondary">{transfer.progress_percent.toFixed(0)}%</Typography>
            </Box>
          ))}
          {!transfersQuery.isLoading && (transfersQuery.data?.items.length ?? 0) === 0 && (
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
      <ConnectionDialog
        open={connectionDialogOpen}
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
