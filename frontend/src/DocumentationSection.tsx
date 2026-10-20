import { Alert, Box, Button, Card, CardContent, Chip, Link, Paper, Stack, Typography } from '@mui/material';
import type { ReactNode } from 'react';

const capacityRows = [
  {
    name: 'Rutas elegidas por solicitud',
    value: '100',
    detail: 'Archivos y carpetas seleccionados en el origen. Una carpeta puede contener muchos archivos; también se aplica el límite de inventario correspondiente.',
  },
  {
    name: 'Archivos por transferencia cloud',
    value: '5.000',
    detail: 'Límite predeterminado para el inventario de archivos de una copia cloud. Se ajusta con TRANSFER_MAX_FILES.',
  },
  {
    name: 'Archivos por transferencia SMB',
    value: '100.000',
    detail: 'Límite predeterminado para una copia cuyo origen es un NAS SMB. Se ajusta con SMB_TRANSFER_MAX_FILES.',
  },
  {
    name: 'Tamaño individual de archivo',
    value: 'Sin límite',
    detail: 'TRANSFER_MAX_FILE_BYTES=0 desactiva el tope por archivo. El espacio temporal disponible todavía limita los archivos que requieren staging.',
  },
];

function GuideSection({
  id,
  index,
  eyebrow,
  title,
  children,
}: {
  id: string;
  index: string;
  eyebrow: string;
  title: string;
  children: ReactNode;
}) {
  return (
    <Box component="section" className="documentation-block" id={id} aria-labelledby={`${id}-title`}>
      <Box className="documentation-block-heading">
        <Chip className="documentation-index" size="small" label={index} />
        <Box>
          <Typography className="documentation-eyebrow" variant="overline">{eyebrow}</Typography>
          <Typography id={`${id}-title`} variant="h5" component="h2">{title}</Typography>
        </Box>
      </Box>
      {children}
    </Box>
  );
}

function GuideCard({ title, children }: { title: string; children: ReactNode }) {
  return (
    <Card className="documentation-card" variant="outlined">
      <CardContent>
        <Typography variant="subtitle1" component="h3" fontWeight={750}>{title}</Typography>
        <Typography variant="body2" color="text.secondary" sx={{ mt: 0.75 }}>{children}</Typography>
      </CardContent>
    </Card>
  );
}

export default function DocumentationSection() {
  return (
    <Box className="documentation-page" component="article" aria-labelledby="documentation-overview-title">
      <Paper className="documentation-intro" elevation={0}>
        <Box>
          <Typography className="documentation-eyebrow" variant="overline">GUÍA DE USO</Typography>
          <Typography id="documentation-overview-title" variant="h5" component="h2">
            Copia archivos entre tus servicios autorizados
          </Typography>
          <Typography color="text.secondary" sx={{ mt: 0.8, maxWidth: 780 }}>
            Cloud Transfer Manager coordina copias entre servicios de almacenamiento y muestra su avance. Solo ofrece la operación COPY: no mueve, elimina, renombra ni sincroniza archivos, ni permite crear carpetas manualmente.
          </Typography>
        </Box>
        <Box className="documentation-intro-badge">
          <Typography variant="overline">OPERACIÓN</Typography>
          <Typography variant="h6">COPY</Typography>
          <Typography variant="caption" color="text.secondary">Solo copias</Typography>
        </Box>
      </Paper>

      <Box component="nav" className="documentation-index-nav" aria-label="Índice de documentación">
        <Button component="a" href="#documentation-start">Uso básico</Button>
        <Button component="a" href="#documentation-capacity">Capacidad y límites</Button>
        <Button component="a" href="#documentation-background">Cola y paralelismo</Button>
        <Button component="a" href="#documentation-schedule">Programaciones</Button>
        <Button component="a" href="#documentation-progress">Seguimiento</Button>
        <Button component="a" href="#documentation-diagrams">Diagramas</Button>
      </Box>

      <GuideSection id="documentation-start" index="01" eyebrow="PRIMEROS PASOS" title="Cómo preparar una copia">
        <Box className="documentation-step-grid">
          <GuideCard title="1. Configura las conexiones">
            Un administrador agrega y prueba MEGA, Google Drive o Seafile desde <strong>Conexiones</strong>. Google Drive requiere autorizar OAuth. La integración documentada de Seafile corresponde a Community Edition 13.x y usa el adaptador elegido al crear la conexión. Para un NAS, el administrador lo registra en <strong>Servicios privados</strong> y el despliegue debe permitir llegar al host SMB.
          </GuideCard>
          <GuideCard title="2. Elige el origen y los elementos">
            En <strong>Explorador</strong>, selecciona una conexión de origen, navega sus carpetas y marca archivos o carpetas. Puedes seleccionar hasta 100 rutas en una solicitud. Las carpetas conservan su estructura relativa al copiarse.
          </GuideCard>
          <GuideCard title="3. Elige el destino">
            Selecciona una conexión distinta y la carpeta de destino existente. En la interfaz, las conexiones SMB solo se ofrecen como origen; los destinos admitidos son MEGA, Google Drive y Seafile.
          </GuideCard>
          <GuideCard title="4. Elige cómo resolver conflictos">
            <strong>Reemplazar si cambió</strong> es la opción inicial. También puedes elegir <strong>Omitir existentes</strong>. Pulsa <strong>Iniciar transferencia</strong> para comenzar o <strong>Programar transferencia</strong> para elegir una fecha y hora futura.
          </GuideCard>
        </Box>

        <Box className="documentation-two-column">
          <GuideCard title="Roles y administración">
            El administrador crea usuarios, configura conexiones cloud y NAS, administra el correo SMTP y puede consultar las transferencias de toda la instalación. Desde el menú del perfil, cada usuario puede cambiar su contraseña y cerrar sesión.
          </GuideCard>
          <GuideCard title="Acceso a conexiones">
            Los usuarios pueden explorar conexiones globales y las conexiones privadas que tienen asignadas. Una conexión debe estar activa para iniciar una copia. El administrador puede archivarla cuando no tenga trabajos activos; sus credenciales se retiran y el historial de transferencias se conserva.
          </GuideCard>
        </Box>

        <Alert className="documentation-note" severity="info">
          Puedes solicitar una notificación por correo al terminar. El administrador debe configurar SMTP para que el envío funcione. En los servicios cloud también se puede usar Seafile; el adaptador Rclone o Seafile API se define por conexión y no cambia automáticamente ante un error.
        </Alert>
      </GuideSection>

      <GuideSection id="documentation-capacity" index="02" eyebrow="LÍMITES DEL DESPLIEGUE" title="Cuánto puedes transferir">
        <Box className="documentation-table-wrap">
          <Box component="table" className="documentation-limits-table">
            <thead>
              <tr><th scope="col">Límite</th><th scope="col">Valor inicial</th><th scope="col">Qué significa</th></tr>
            </thead>
            <tbody>
              {capacityRows.map((row) => (
                <tr key={row.name}>
                  <th scope="row">{row.name}</th>
                  <td><strong>{row.value}</strong></td>
                  <td>{row.detail}</td>
                </tr>
              ))}
            </tbody>
          </Box>
        </Box>
        <Box className="documentation-two-column">
          <Paper className="documentation-callout" elevation={0}>
            <Typography variant="subtitle1" fontWeight={750}>Espacio temporal</Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mt: 0.6 }}>
              Las copias entre MEGA y otro proveedor usan MEGAcmd y el disco local del worker como puente. Desde MEGA, MEGAcmd descarga el archivo completo al staging y luego se sube al destino; hacia MEGA, primero se descarga desde el origen al staging y luego MEGAcmd lo sube. La copia local se borra al terminar cada archivo, incluso si falla o se cancela; si no se puede limpiar, el trabajo se detiene.
            </Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
              El contenido del archivo no se guarda en RAM ni en el <code>tmpfs</code> pequeño de configuración: usa el espacio libre del filesystem que contiene <code>TRANSFER_STAGING_PATH</code> en el worker. Por defecto es <code>/var/lib/cloud-transfer/staging</code>, dentro del almacenamiento de Docker. MEGAcmd también crea allí un HOME temporal aislado que ocupa espacio adicional y normalmente se retira al cerrar la sesión.
            </Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
              Antes de empezar se comprueba que haya espacio libre para el archivo más grande y que quede una reserva de al menos 1 GiB o un margen del 20% de ese archivo, el que sea mayor. Se vuelve a comprobar antes de procesar cada archivo. En el flujo normal se trabaja uno por uno, así que no hace falta sumar el tamaño de toda la selección; una verificación de una escritura interrumpida puede necesitar temporalmente otra copia local. Los trabajos que comparten el staging esperan su turno. La cuota de MEGA en destino es un límite separado.
            </Typography>
          </Paper>
          <Paper className="documentation-callout" elevation={0}>
            <Typography variant="subtitle1" fontWeight={750}>Capacidad real</Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mt: 0.6 }}>
              El límite configurado no garantiza una velocidad o volumen físico determinado. La red, las cuotas y límites del proveedor, el disco de staging y los recursos del worker también influyen. Si se alcanza el límite de archivos, divide la copia en varias transferencias.
            </Typography>
          </Paper>
        </Box>
        <Alert className="documentation-note" severity="warning">
          Si una copia falla por falta de espacio en staging, abre <strong>Ver informe</strong> desde el historial para consultar el motivo. Libera espacio en el disco que contiene el staging o pide al administrador que lo amplíe y luego pulsa <strong>Reanudar</strong>. Ampliar la cuota de MEGA no corrige la falta de espacio local.
        </Alert>
      </GuideSection>

      <GuideSection id="documentation-background" index="03" eyebrow="EJECUCIÓN" title="Cola, segundo plano y paralelismo">
        <Box className="documentation-two-column">
          <GuideCard title="Cuántas pueden ejecutarse en paralelo">
            El valor inicial es <strong>1 transferencia activa por despliegue</strong>, configurado con <code>TRANSFER_MAX_CONCURRENT</code>. El administrador puede aumentarlo en la configuración del worker. El valor debe coincidir con la concurrencia del worker Celery. Al habilitar varios espacios, solo las copias compatibles corren a la vez: el planificador espera si detecta un destino ocupado, una ejecución exclusiva o el uso simultáneo del staging compartido.
          </GuideCard>
          <GuideCard title="Cuántas quedan en segundo plano">
            No existe un máximo global separado para la cola de trabajos pendientes. La aplicación no limita cuántas solicitudes se guardan por usuario; se deben dimensionar la base de datos, el broker y el worker para la carga esperada. El contador del reloj muestra trabajos activos clasificados como programados, exclusivos, dependientes o en espera, tanto futuros como en ejecución.
          </GuideCard>
        </Box>
        <Alert className="documentation-note" severity="info">
          Un usuario ve sus propias transferencias; el administrador ve las transferencias de todos. El panel del reloj pagina la lista que muestra, mientras que el contador corresponde al total que devuelve el servidor. Las transferencias comunes que no están programadas ni esperando se muestran en el explorador y en el historial.
        </Alert>
        <Box className="documentation-mode-grid">
          <Paper className="documentation-mode-card" elevation={0}>
            <Chip label="AUTO" size="small" color="primary" />
            <Typography variant="subtitle1" fontWeight={750} sx={{ mt: 1 }}>Automática</Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
              El dispatcher inicia cada trabajo cuando hay capacidad y no existen conflictos de conexión o staging. Las copias que no comparten recursos ocupados pueden correr en paralelo si hay más de un espacio configurado.
            </Typography>
          </Paper>
          <Paper className="documentation-mode-card" elevation={0}>
            <Chip label="EXCLUSIVE" size="small" />
            <Typography variant="subtitle1" fontWeight={750} sx={{ mt: 1 }}>Exclusiva</Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
              Espera a que terminen las ejecuciones admitidas. Al llegar al turno, bloquea la admisión de trabajos posteriores hasta completarse. Una exclusiva programada para más adelante no bloquea la cola antes de su hora.
            </Typography>
          </Paper>
        </Box>
      </GuideSection>

      <GuideSection id="documentation-schedule" index="04" eyebrow="PROGRAMAR Y ORDENAR" title="Fechas, dependencias y cancelación">
        <Box className="documentation-two-column">
          <GuideCard title="Programar una vez">
            Indica una fecha futura desde el navegador. La fecha se guarda normalizada a UTC junto con la zona horaria. Las programaciones no son recurrentes. Beat revisa la cola cada 15 segundos y espera también a que haya capacidad disponible.
          </GuideCard>
          <GuideCard title="Encadenar transferencias">
            Una transferencia dependiente comienza cuando su transferencia padre termina como completada o completada con avisos. Si el padre falla o se cancela, la hija queda bloqueada. En <strong>Programaciones</strong> puedes revisar el motivo de espera, reprogramar trabajos pendientes o cancelarlos.
          </GuideCard>
        </Box>
        <Typography className="documentation-inline-note" variant="body2" color="text.secondary">
          Cancelar un trabajo activo solicita detenerlo; el estado puede tardar en actualizarse. Las transferencias SMB fallidas o canceladas ofrecen <strong>Reanudar</strong> desde el historial si siguen disponibles la conexión, sus credenciales y la misma raíz del NAS. El acceso de red al NAS debe mantenerse cuando el trabajo se ejecute; CTM no crea ni administra VPN.
        </Typography>
      </GuideSection>

      <GuideSection id="documentation-progress" index="05" eyebrow="SEGUIMIENTO Y RESULTADOS" title="Progreso, historial y avisos">
        <Box className="documentation-three-column">
          <GuideCard title="Progreso en vivo">
            Durante una copia normal se muestra porcentaje, bytes, velocidad y ETA cuando se pueden calcular. Los eventos llegan por SSE; la interfaz vuelve a consultar el estado cada 15 segundos como respaldo.
          </GuideCard>
          <GuideCard title="Historial reciente">
            El trabajo aparece con su identificador TRF, estado, fecha, duración, ruta y conteos. En transferencias fallidas, <strong>Ver informe</strong> muestra el motivo general y, cuando existe, el detalle por archivo. Los resultados son por usuario; la cuenta administradora puede consultar el conjunto de la instalación.
          </GuideCard>
          <GuideCard title="Migraciones SMB">
            El historial permite abrir el manifiesto por archivo y ver copiados, omitidos y fallidos. El contador de elementos procesados también incluye omitidos, así que no representa solo archivos copiados.
          </GuideCard>
        </Box>
        <Box className="documentation-statuses" aria-label="Estados de transferencia">
          <Typography variant="caption" color="text.secondary">ESTADOS HABITUALES</Typography>
          <Stack direction="row" useFlexGap flexWrap="wrap" spacing={0.75} sx={{ mt: 0.8 }}>
            {['Programada', 'En cola', 'Validando', 'En curso', 'Reintentando', 'Completada', 'Completada con avisos', 'Fallida', 'Cancelada'].map((status) => (
              <Chip key={status} label={status} size="small" variant="outlined" />
            ))}
          </Stack>
          <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
            El sistema permite hasta 5 reintentos adicionales para errores transitorios, sujeto al tipo de error. Una transferencia puede terminar con avisos si algunos elementos se omitieron o no se pudieron copiar.
          </Typography>
        </Box>
      </GuideSection>

      <GuideSection id="documentation-diagrams" index="06" eyebrow="VISTA TÉCNICA" title="Cómo se conectan los componentes">
        <Box className="documentation-diagram-grid">
          <Paper component="figure" className="documentation-diagram-card" elevation={0}>
            <Box className="documentation-diagram-copy">
              <Typography variant="subtitle1" component="h3" fontWeight={750}>Arquitectura</Typography>
              <Typography variant="body2" color="text.secondary">
                El navegador llega por Nginx a React y FastAPI. PostgreSQL conserva los datos, Redis coordina tareas y eventos, y los workers ejecutan las copias hacia los proveedores.
              </Typography>
            </Box>
            <Link className="documentation-diagram-image-link" href="/documentation/architecture.svg" target="_blank" rel="noreferrer" aria-label="Abrir diagrama de arquitectura en tamaño completo">
              <Box component="img" className="documentation-diagram-image" src="/documentation/architecture.svg" alt="Diagrama de arquitectura: navegador, Nginx, frontend React, API FastAPI, PostgreSQL, Redis, workers y proveedores de almacenamiento." loading="lazy" />
            </Link>
            <Typography component="figcaption" variant="caption" color="text.secondary">Nginx es el único servicio que publica puertos. Los servicios internos permanecen en la red privada de Docker.</Typography>
          </Paper>
          <Paper component="figure" className="documentation-diagram-card" elevation={0}>
            <Box className="documentation-diagram-copy">
              <Typography variant="subtitle1" component="h3" fontWeight={750}>Secuencia de una copia</Typography>
              <Typography variant="body2" color="text.secondary">
                FastAPI valida y registra la solicitud, el dispatcher la admite, Celery entrega el trabajo al worker y el progreso vuelve al navegador por Redis y SSE.
              </Typography>
            </Box>
            <Link className="documentation-diagram-image-link" href="/documentation/transfer-sequence.svg" target="_blank" rel="noreferrer" aria-label="Abrir diagrama de secuencia en tamaño completo">
              <Box component="img" className="documentation-diagram-image" src="/documentation/transfer-sequence.svg" alt="Diagrama de secuencia de una transferencia COPY: navegador, API, PostgreSQL, Redis, Celery, worker, proveedor y progreso SSE." loading="lazy" />
            </Link>
            <Typography component="figcaption" variant="caption" color="text.secondary">Los eventos de estado se conservan; SSE transmite el avance al cliente conectado.</Typography>
          </Paper>
        </Box>
      </GuideSection>

      <Box className="documentation-footer">
        <Typography variant="caption" color="text.secondary">
          Los valores mostrados corresponden a la configuración inicial de la aplicación. El administrador puede ajustarlos en el despliegue; revisa la documentación de operación para conocer staging, secretos, volúmenes y variables de entorno.
        </Typography>
      </Box>
    </Box>
  );
}
