"""Fichaje automático en Sesame 1 (panel.sesametime.com).

Cada día arma el plan a partir de config.yml, se salta festivos y días del calendario
"Fichaje", avisa por Telegram antes de cada fichaje (con botones para cancelar o
retrasar) y ficha. Con MODO distinto de "real" solo avisa: no toca Sesame salvo para leer.
"""
import datetime as dt
import json
import logging
import os
import random
import re
import signal
import sys
import time
import unicodedata
from zoneinfo import ZoneInfo

import holidays
import httpx
import recurring_ical_events
import yaml
from icalendar import Calendar

CONFIG = os.environ.get("CONFIG", "/config.yml")
ESTADO = os.environ.get("ESTADO", "/estado/estado.json")
MODO_REAL = os.environ.get("MODO", "prueba") == "real"
PANEL = "https://panel.sesametime.com"
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0"
DIAS = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]
PERDIDO_TRAS = dt.timedelta(minutes=15)  # pasado este margen ya no se ficha (p. ej. tras un apagón)
REINTENTOS = 3

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("fichaje")
logging.getLogger("httpx").setLevel(logging.WARNING)  # registra URLs completas: token de Telegram e iCal secreto


class Fallo(Exception):
    pass


def norm(s):
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def leer_config():
    with open(CONFIG) as f:
        cfg = yaml.safe_load(f)
    for dia in DIAS:
        for ini, fin in cfg["horario"].get(dia) or []:
            if dt.time.fromisoformat(ini) >= dt.time.fromisoformat(fin):
                raise ValueError(f"{dia}: tramo {ini}-{fin} al revés")
    return cfg


# --- Sesame -----------------------------------------------------------------

class Sesame:
    """Cliente mínimo del panel web de Sesame 1 (formulario CakePHP + cookies)."""

    def __init__(self):
        self.c = httpx.Client(follow_redirects=True, timeout=30, headers={"User-Agent": UA})

    def entrar(self):
        self.c.cookies.clear()
        html = self.c.get(PANEL + "/").text
        form = re.search(r'<form action="/"[^>]*id="UserLoginForm".*?</form>', html, re.S)
        if not form:
            raise Fallo("no encuentro el formulario de login de Sesame")
        datos = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', form.group(0)))
        datos["data[User][email]"] = os.environ["SESAME_EMAIL"]
        datos["data[User][password]"] = os.environ["SESAME_PASSWORD"]
        r = self.c.post(PANEL + "/", data=datos)
        if "/admin/" not in r.url.path:
            raise Fallo("Sesame no acepta el usuario o la contraseña")
        m = re.search(r"/admin/users/checks/([0-9a-f-]{36})", r.url.path)
        self.usuario = m.group(1) if m else None
        return r.text

    def festivo(self, dia):
        """Texto del día si Sesame lo marca como no laborable (p. ej. "Permiso retribuido: <ciudad>"), si no None.
        Sale de la página "Mis fichajes" del mes, que marca así festivos y fines de semana."""
        self.entrar()
        if not self.usuario:
            raise Fallo("no encuentro mi id de usuario en Sesame")
        html = self.c.get(f"{PANEL}/admin/users/checks/{self.usuario}/{dia:%Y-%m}").text
        dias = re.findall(r'<li class="margin-top-10 ?">[^<]*?(\d+) de [^<]*</li>\s*<ul class="bar ([^"]*)"([^>]*)>', html)
        if not dias:
            raise Fallo("no entiendo el calendario del mes en Sesame")
        for num, clases, resto in dias:
            if int(num) == dia.day:
                if "holiday" not in clases.split():
                    return None
                titulo = re.search(r'_title="([^"]*)"', resto)
                return titulo.group(1) if titulo else "festivo"
        raise Fallo(f"el día {dia} no aparece en Sesame")

    @staticmethod
    def boton(html):
        """Devuelve (dentro, href) según el botón principal de fichar que muestra el panel."""
        m = re.search(r'<a href="(/admin/checks/check_panel/[^"]*)" id="check_button" class="([^"]*)"[^>]*>([^<]*)<', html)
        if not m:
            raise Fallo("no encuentro el botón de fichar en el panel")
        href, clases, texto = m.group(1), m.group(2).split(), m.group(3).strip().upper()
        fuera = "checkin" in clases or texto.endswith(" IN")
        dentro = "checkout" in clases or texto.endswith(" OUT")
        if fuera == dentro:
            raise Fallo(f"no sé si estás dentro o fuera (botón {texto!r}, {href})")
        return dentro, href

    def estado(self):
        return self.boton(self.entrar())

    def fichar(self, tipo):
        dentro, href = self.estado()
        if tipo == "entrada" and dentro:
            return "ya estabas dentro, no hago nada"
        if tipo == "salida" and not dentro:
            return "ya estabas fuera, no hago nada"
        if not MODO_REAL:
            return f"MODO PRUEBA: no fichado (habría abierto {href})"
        self.c.get(PANEL + href)
        dentro_ahora, _ = self.boton(self.c.get(PANEL + "/admin/users/checks").text)
        if dentro_ahora == dentro:
            raise Fallo(f"he pulsado {href} pero Sesame sigue igual")
        return "fichado ✅"


# --- Días libres ---------------------------------------------------------------

def motivo_libre(dia, cfg, sesame):
    """Texto con el motivo si ese día no se ficha; None si se trabaja."""
    nf = cfg.get("no_fichar") or {}
    if dia.isoformat() in {str(f) for f in nf.get("fechas") or []}:
        return "fecha puesta a mano en config.yml"
    if nf.get("sesame"):
        texto = sesame.festivo(dia)
        if texto:
            return f"Sesame: {texto}"
    fo = nf.get("festivos_oficiales") or {}
    if fo.get("activo"):
        fest = holidays.country_holidays(fo.get("pais", "ES"), subdiv=fo.get("comunidad") or None, years=dia.year)
        if dia in fest:
            return f"festivo: {fest[dia]}"
    gc = nf.get("google_calendar") or {}
    if gc.get("activo"):
        titulo = evento_dia_completo(dia, gc.get("titulos") or [])
        if titulo:
            return f"calendario Fichaje: {titulo}"
    return None


def evento_dia_completo(dia, titulos):
    r = httpx.get(os.environ["GOOGLE_ICAL_URL"], timeout=30, follow_redirects=True)
    r.raise_for_status()
    claves = [norm(t) for t in titulos]
    for ev in recurring_ical_events.of(Calendar.from_ical(r.content)).at(dia):
        if isinstance(ev.decoded("DTSTART"), dt.datetime):
            continue  # solo cuentan los de día completo; una cita de 1 h no anula el día
        titulo = str(ev.get("SUMMARY", "(sin título)"))
        if not claves or any(c in norm(titulo) for c in claves):
            return titulo
    return None


# --- Telegram ----------------------------------------------------------------

class Telegram:
    """Opcional: sin TELEGRAM_TOKEN/TELEGRAM_CHAT los mensajes solo van al registro (docker logs)."""

    def __init__(self):
        token = os.environ.get("TELEGRAM_TOKEN", "").strip()
        chat = os.environ.get("TELEGRAM_CHAT", "").strip()
        self.activo = bool(token and chat)
        self.url = f"https://api.telegram.org/bot{token}/"
        self.chat = int(chat) if self.activo else None
        self.c = httpx.Client(timeout=60)

    def api(self, metodo, **datos):
        r = self.c.post(self.url + metodo, json=datos)
        r.raise_for_status()
        return r.json()["result"]

    def enviar(self, texto, botones=None, silencioso=False):
        if not self.activo:
            log.info("[aviso] %s", texto)
            return None
        datos = {"chat_id": self.chat, "text": texto, "disable_notification": silencioso}
        if botones:
            datos["reply_markup"] = {"inline_keyboard": botones}
        try:
            return self.api("sendMessage", **datos)["message_id"]
        except Exception as e:  # un fallo de Telegram no debe impedir fichar
            log.error("Telegram: %s", e)

    def editar(self, msg_id, texto):
        if not self.activo:
            log.info("[aviso] %s", texto)
            return
        try:
            self.api("editMessageText", chat_id=self.chat, message_id=msg_id, text=texto)
        except Exception as e:
            log.warning("Telegram (editar): %s", e)


# --- Programa ----------------------------------------------------------------

class Fichaje:
    def __init__(self):
        self.tz = ZoneInfo(os.environ.get("TZ", "Europe/Madrid"))
        self.tg = Telegram()
        self.sesame = Sesame()
        self.st = {"offset": 0, "dia": None, "libre": None, "eventos": []}
        if os.path.exists(ESTADO):
            with open(ESTADO) as f:
                self.st.update(json.load(f))

    def guardar(self):
        tmp = ESTADO + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.st, f, indent=1)
        os.replace(tmp, ESTADO)

    def ahora(self):
        return dt.datetime.now(self.tz)

    # Plan del día

    def planificar(self, dia):
        cfg = leer_config()
        self.st.update(dia=dia.isoformat(), libre=None, eventos=[])
        tramos = cfg["horario"].get(DIAS[dia.weekday()]) or []
        if tramos:
            try:
                self.st["libre"] = motivo_libre(dia, cfg, self.sesame)
            except Exception as e:
                self.tg.enviar(f"⚠️ No he podido comprobar si hoy es día libre ({e}). "
                               "Si no trabajas, cancela cada fichaje desde el aviso.")
        if tramos and not self.st["libre"]:
            var = int(cfg.get("variacion_minutos", 0))
            anterior = None
            for ini, fin in tramos:
                for hora, tipo in ((ini, "entrada"), (fin, "salida")):
                    t = dt.datetime.combine(dia, dt.time.fromisoformat(hora), self.tz)
                    t += dt.timedelta(minutes=random.randint(-var, var), seconds=random.randint(0, 59))
                    if anterior and t <= anterior + dt.timedelta(minutes=1):
                        t = anterior + dt.timedelta(minutes=2)
                    anterior = t
                    # Si se planifica tarde (arranque a media mañana), lo ya pasado no se ficha ni se avisa.
                    estado = "omitido" if t + PERDIDO_TRAS < self.ahora() else "pendiente"
                    self.st["eventos"].append({"hora": t.isoformat(), "tipo": tipo, "estado": estado,
                                               "msg": None, "intentos": 0})
        aviso = cfg.get("aviso") or {}
        self.st["aviso_activo"] = bool(aviso.get("activo", True))
        self.st["aviso_min"] = int(aviso.get("minutos_antes", 10))  # sin aviso, a esa hora se recomprueba el día libre
        self.st["notificar"] = bool(cfg.get("notificar_fichajes", True))
        self.guardar()
        log.info("Plan %s: %s", dia, self.resumen())
        if self.st["libre"]:
            self.tg.enviar(f"🌴 Hoy no ficho ({self.st['libre']}).")

    def resumen(self):
        modo = "" if MODO_REAL else " [MODO PRUEBA]"
        if self.st["libre"]:
            return f"📅 {self.st['dia']}: día libre ({self.st['libre']}){modo}"
        if not self.st["eventos"]:
            return f"📅 {self.st['dia']}: no se trabaja{modo}"
        iconos = {"pendiente": "⏳", "avisado": "🔔", "hecho": "✅", "cancelado": "❌", "perdido": "⚠️",
                  "error": "🛑", "omitido": "⏭️"}
        lineas = [f"{iconos.get(e['estado'], '?')} {e['hora'][11:16]} {e['tipo']}" for e in self.st["eventos"]]
        return f"📅 {self.st['dia']}{modo}\n" + "\n".join(lineas)

    # Bucle

    def paso(self):
        ahora = self.ahora()
        if self.st["dia"] != ahora.date().isoformat():
            self.planificar(ahora.date())
        aviso = dt.timedelta(minutes=self.st.get("aviso_min", 10))
        for i, e in enumerate(self.st["eventos"]):
            hora = dt.datetime.fromisoformat(e["hora"])
            if e["estado"] in ("pendiente", "avisado") and ahora > hora + PERDIDO_TRAS:
                e["estado"] = "perdido"
                self.tg.enviar(f"⚠️ No he fichado la {e['tipo']} de las {e['hora'][11:16]} "
                               "(el servidor estaba parado). Revísalo en Sesame.")
            elif e["estado"] == "pendiente" and ahora >= hora - aviso:
                self.avisar(i, e)
            elif e["estado"] == "avisado" and ahora >= hora:
                self.ejecutar(e)
            else:
                continue
            self.guardar()

    def avisar(self, i, e):
        e["estado"] = "avisado"
        libre = None
        try:
            libre = motivo_libre(dt.date.fromisoformat(self.st["dia"]), leer_config(), self.sesame)
        except Exception as ex:
            log.warning("No pude recomprobar el día libre: %s", ex)
        if libre:  # p. ej. se añadieron vacaciones para hoy después de planificar
            self.cancelar_resto(f"🌴 Cancelo lo que queda de hoy: {libre}")
            return
        if not self.st.get("aviso_activo", True):
            return
        prueba = " (MODO PRUEBA)" if not MODO_REAL else ""
        e["msg"] = self.tg.enviar(
            f"🔔 A las {e['hora'][11:16]} ficho {e['tipo'].upper()}{prueba}.",
            [[{"text": "❌ No fichar", "callback_data": f"no:{i}"},
              {"text": "⏰ +15 min", "callback_data": f"mas:{i}"}],
             [{"text": "🚫 Nada más hoy", "callback_data": "hoy"}]])

    def ejecutar(self, e):
        try:
            resultado = self.sesame.fichar(e["tipo"])
        except Exception as ex:
            e["intentos"] += 1
            log.error("Fallo al fichar %s: %s", e["tipo"], ex)
            if e["intentos"] < REINTENTOS:
                e["hora"] = (self.ahora() + dt.timedelta(minutes=2)).isoformat()
                return
            e["estado"] = "error"
            self.tg.enviar(f"🛑 No he podido fichar la {e['tipo']}: {ex}\n¡Ficha a mano!")
            return
        e["estado"] = "hecho"
        texto = f"{e['tipo'].capitalize()} {self.ahora():%H:%M}: {resultado}"
        log.info(texto)
        if e["msg"]:
            self.tg.editar(e["msg"], texto)
        elif self.st.get("notificar", True):
            self.tg.enviar(texto, silencioso=True)

    def cancelar_resto(self, texto):
        for e in self.st["eventos"]:
            if e["estado"] in ("pendiente", "avisado"):
                e["estado"] = "cancelado"
                if e["msg"]:
                    self.tg.editar(e["msg"], f"❌ {e['tipo']} de las {e['hora'][11:16]} cancelada")
        self.guardar()
        self.tg.enviar(texto)

    # Telegram entrante

    def atender(self, espera):
        if not self.tg.activo:
            time.sleep(espera)
            return
        try:
            updates = self.tg.api("getUpdates", offset=self.st["offset"], timeout=espera,
                                  allowed_updates=["message", "callback_query"])
        except Exception as e:
            log.warning("Telegram (getUpdates): %s", e)
            time.sleep(10)
            return
        for u in updates:
            self.st["offset"] = u["update_id"] + 1
            if "callback_query" in u:
                self.boton(u["callback_query"])
            elif "message" in u:
                self.comando(u["message"])
        if updates:
            self.guardar()

    def boton(self, q):
        try:
            self.tg.api("answerCallbackQuery", callback_query_id=q["id"])
        except Exception:
            pass
        if q["from"]["id"] != self.tg.chat or q.get("message", {}).get("chat", {}).get("id") != self.tg.chat:
            return  # solo el chat configurado en TELEGRAM_CHAT
        accion, _, idx = q.get("data", "").partition(":")
        if accion == "hoy":
            self.cancelar_resto("🚫 Hoy no ficho nada más. /reanudar para deshacerlo.")
            return
        eventos = self.st["eventos"]
        if not idx.isdigit() or int(idx) >= len(eventos):
            return
        e = eventos[int(idx)]
        if e["estado"] != "avisado" or q["message"]["message_id"] != e["msg"]:
            return  # aviso de otro día o ya resuelto
        if accion == "no":
            e["estado"] = "cancelado"
            self.tg.editar(e["msg"], f"❌ {e['tipo']} de las {e['hora'][11:16]} cancelada")
        elif accion == "mas":
            hora = dt.datetime.fromisoformat(e["hora"]) + dt.timedelta(minutes=15)
            e["hora"] = hora.isoformat()
            self.tg.editar(e["msg"], f"⏰ {e['tipo']} retrasada a las {hora:%H:%M}")
            e["msg"] = self.tg.enviar(
                f"🔔 A las {hora:%H:%M} ficho {e['tipo'].upper()}.",
                [[{"text": "❌ No fichar", "callback_data": f"no:{idx}"},
                  {"text": "⏰ +15 min", "callback_data": f"mas:{idx}"}]])

    def comando(self, m):
        if m.get("chat", {}).get("id") != self.tg.chat:
            return
        cmd = (m.get("text") or "").split("@")[0].strip().lower()
        if cmd == "/hoy":
            self.tg.enviar(self.resumen())
        elif cmd == "/nofichar":
            self.cancelar_resto("🚫 Hoy no ficho nada más. /reanudar para deshacerlo.")
        elif cmd == "/reanudar":
            ahora = self.ahora()
            n = 0
            for e in self.st["eventos"]:
                if e["estado"] == "cancelado" and dt.datetime.fromisoformat(e["hora"]) > ahora:
                    e.update(estado="pendiente", msg=None)
                    n += 1
            self.tg.enviar(f"▶️ Reanudados {n} fichajes.\n" + self.resumen())
        elif cmd == "/estado":
            try:
                dentro, _ = self.sesame.estado()
                self.tg.enviar("🟢 Ahora estás DENTRO en Sesame" if dentro else "⚪ Ahora estás FUERA en Sesame")
            except Exception as e:
                self.tg.enviar(f"🛑 No puedo leer Sesame: {e}")
        elif cmd in ("/ayuda", "/start"):
            self.tg.enviar("/hoy plan de hoy\n/estado dentro o fuera en Sesame\n"
                           "/nofichar cancela lo que queda de hoy\n/reanudar deshace /nofichar")

    def siguiente_espera(self):
        """Segundos hasta lo próximo que haya que hacer (máx. 25, para el sondeo de Telegram)."""
        ahora = self.ahora()
        aviso = dt.timedelta(minutes=self.st.get("aviso_min", 10))
        proximos = []
        for e in self.st["eventos"]:
            hora = dt.datetime.fromisoformat(e["hora"])
            if e["estado"] == "pendiente":
                proximos.append(hora - aviso)
            elif e["estado"] == "avisado":
                proximos.append(hora)
        segundos = min([(p - ahora).total_seconds() for p in proximos] + [25])
        return max(0, int(segundos))

    def correr(self):
        leer_config()  # falla al arrancar si config.yml está mal
        log.info("Arrancando en modo %s", "REAL" if MODO_REAL else "PRUEBA")
        if not self.tg.activo:
            log.warning("Sin Telegram: los avisos (también los de error) solo saldrán en docker logs")
        self.paso()
        self.tg.enviar(f"🤖 Fichaje arrancado ({'REAL' if MODO_REAL else 'MODO PRUEBA'}).\n" + self.resumen())
        while True:
            self.atender(self.siguiente_espera())
            try:
                self.paso()
            except Exception as e:
                log.exception("Error en el bucle")
                self.tg.enviar(f"🛑 Error en el fichaje automático: {e}")
                time.sleep(60)


if __name__ == "__main__":
    # Como proceso 1 del contenedor, Python ignora SIGTERM: sin esto `docker stop` espera 10 s y mata.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    Fichaje().correr()
