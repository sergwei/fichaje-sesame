# fichaje-sesame

Ficha automáticamente en **Sesame** (versión antigua, `panel.sesametime.com`) según tu horario,
saltándose festivos y vacaciones, y te avisa por **Telegram**. Pensado para correr en Docker en un
servidor que esté siempre encendido (una Raspberry, un mini PC, un NAS…).

> ⚠️ **Úsalo con responsabilidad.** En España el registro de jornada es obligatorio y debe reflejar
> las horas que trabajas de verdad. Esta herramienta no sustituye a ese registro: te ahorra pulsar el
> botón los días que trabajas en tu horario habitual. Revisa la política de tu empresa y las condiciones
> de uso de Sesame antes de usarla. Proyecto personal, sin relación con Sesame HR.

## Qué hace

- Ficha **entrada y salida** a las horas de `config.yml`, con unos minutos de variación al azar.
  Las pausas (p. ej. la comida) se fichan como salida y nueva entrada.
- **No ficha** si el día es libre según cualquiera de estas fuentes:
  - los festivos que marca Sesame en "Mis fichajes";
  - los festivos oficiales (librería [`holidays`](https://pypi.org/project/holidays/), nacionales + comunidad autónoma);
  - eventos de día completo de un calendario de Google (vacaciones);
  - fechas sueltas puestas a mano.
- Antes de pulsar comprueba si ya estás dentro o fuera, para no fichar dos veces si lo haces a mano.
  Después de pulsar comprueba en Sesame que se ha registrado.
- Por Telegram: aviso opcional antes de fichar con botones (❌ No fichar / ⏰ +15 min / 🚫 Nada más hoy),
  mensaje silencioso tras fichar y aviso si algo falla. Comandos: `/hoy`, `/estado`, `/nofichar`, `/reanudar`.
- **Modo prueba** (por defecto): hace todo, pero en vez de fichar te dice lo que habría hecho.

## Requisitos

- Docker con Docker Compose.
- Una cuenta de Sesame con **usuario y contraseña**. Con inicio de sesión de Google o Microsoft (SSO) no funciona.
- Que tu empresa use el **Sesame antiguo**. Para comprobarlo:
  ```bash
  curl -s -X POST https://login.sesametime.com/private/login-finder/v1/pre-login \
    -H 'Content-Type: application/json' -d '{"email":"tu@empresa.com"}'
  ```
  Si responde `"sesame1":true`, es el tuyo. Si responde `"sesame2":true`, esta herramienta no sirve tal cual:
  el Sesame nuevo tiene API y SDK oficial ([`sesamehr`](https://pypi.org/project/sesamehr/)).
- Un bot de Telegram (gratis, se crea con [@BotFather](https://t.me/BotFather)).

## Instalación

```bash
git clone https://github.com/sergwei/fichaje-sesame.git
cd fichaje-sesame

cp .env.example .env && chmod 600 .env     # credenciales: edítalo
cp config.example.yml config.yml           # horario y días libres: edítalo
mkdir -p estado && sudo chown 1000:1000 estado

docker compose up -d --build
docker logs -f fichaje
```

Al arrancar te llega por Telegram "🤖 Fichaje arrancado (MODO PRUEBA)" con el plan del día.
Déjalo un día o dos en modo prueba. Cuando los mensajes cuadren con tu horario, pon `MODO=real` en
`.env` y ejecuta `docker compose up -d`.

### Telegram

1. Habla con [@BotFather](https://t.me/BotFather), usa `/newbot` y copia el token en `TELEGRAM_TOKEN`.
2. Escribe cualquier cosa a tu bot y abre `https://api.telegram.org/bot<TOKEN>/getUpdates`.
   El número de `"chat":{"id": ...}` va en `TELEGRAM_CHAT`.
3. El bot solo obedece a ese chat: los mensajes de cualquier otra persona se ignoran.

### Vacaciones desde Google Calendar (opcional)

El servidor lee un calendario por su **"Dirección secreta en formato iCal"**, sin iniciar sesión en Google.
La encontrarás en Google Calendar, en la configuración del calendario → *Integrar el calendario* → termina en `/basic.ics`.
Ponla en `GOOGLE_ICAL_URL` y activa `google_calendar` en `config.yml`.

- **Si apuntas tus vacaciones en un calendario tuyo:** usa su dirección secreta y listo.
- **Si tus vacaciones están en un calendario compartido del equipo** (y no tienes permiso para ver su
  dirección secreta), usa [`apps-script.example.gs`](apps-script.example.gs). Es un pequeño script que se
  ejecuta en tu cuenta de Google y cada hora copia tus eventos (filtrando por palabras, p. ej. "vacaciones"
  + tu nombre) a un calendario tuyo llamado "Fichaje". Las instrucciones están en el propio archivo.
  En ese calendario también puedes añadir a mano cualquier día que no quieras fichar.

> No hagas público ningún calendario para esto. La dirección *pública* solo funciona con calendarios
> públicos; la *secreta* basta y se puede regenerar si se filtra.

## Configuración

Todo está comentado en [`config.example.yml`](config.example.yml). `config.yml` se vuelve a leer cada día
y antes de cada fichaje, así que los cambios no necesitan reiniciar.

| Archivo | Qué contiene | ¿Se sube a git? |
|---|---|---|
| `.env` | credenciales de Sesame, Telegram, URL del calendario, `MODO` | ❌ nunca |
| `config.yml` | tu horario y tus días libres | ❌ |
| `apps-script.gs` | tu copia del script con el ID de tu calendario | ❌ |
| `estado/` | lo que ya se ha fichado hoy (para no repetir tras un reinicio) | ❌ |
| `*.example.*` | plantillas sin datos personales | ✅ |

## Seguridad y privacidad

- El contenedor no abre puertos: solo sale a internet (Sesame, Google Calendar y Telegram).
  Corre sin root, con el sistema de archivos de solo lectura, sin capacidades y con límites de memoria y procesos.
- La contraseña de Sesame solo está en `.env`. Protégelo con `chmod 600`.
- Los registros (`docker logs`) no muestran ni el token de Telegram ni la dirección secreta del calendario.
- Los fichajes automáticos se hacen **sin ubicación**. Si normalmente fichas desde el móvil con GPS, tu
  empresa verá esa diferencia.
- Sesame registra la IP desde la que fichas: será la de tu servidor.

## Cómo funciona por dentro

Sesame 1 no tiene API pública para empleados, así que el programa usa el panel web como lo haría el navegador:

1. `GET /` para leer los campos ocultos del formulario y `POST /` con usuario y contraseña (sesión por cookies).
2. Lee el botón principal de fichar (`#check_button`) para saber si estás dentro o fuera y qué enlace abre.
3. Abre ese enlace (`/admin/checks/check_panel/...`) y vuelve a comprobar el botón.
4. Los festivos salen de "Mis fichajes" (`/admin/users/checks/<id>/AAAA-MM`): Sesame marca esos días con la clase `holiday`.

Como depende del HTML de Sesame, **si Sesame cambia su web puede dejar de funcionar**. En ese caso lo
detecta y te avisa por Telegram para que fiches a mano.

## Problemas conocidos

- Probado con una sola empresa. Si tu empresa usa "tipos de fichaje" al salir, o exige ubicación,
  puede que la salida no funcione. Abre un *issue* con lo que diga `docker logs fichaje` (revisa que no
  incluya datos tuyos).
- Si el servidor está apagado a la hora de fichar, no ficha con retraso: te avisa de que se lo ha saltado.

## Licencia

[MIT](LICENSE)
