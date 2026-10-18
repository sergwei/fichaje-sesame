// Copia tus vacaciones de un calendario compartido (p. ej. el del equipo) a un calendario propio "Fichaje",
// que sí tiene dirección secreta iCal para que el servidor lo lea.
// Copia este archivo, rellena ORIGEN y PALABRAS, pégalo en https://script.google.com
// (Nuevo proyecto) y ejecuta instalar() una vez.
// Lo que añadas a mano en "Fichaje" no se toca.

const ORIGEN = 'ID_DEL_CALENDARIO@group.calendar.google.com';  // calendario compartido (Configuración → Integrar el calendario → ID)
const DESTINO = 'Fichaje';
// Se copia un evento si su título contiene TODAS estas palabras, en cualquier orden
// (sin distinguir mayúsculas ni tildes): "Vacaciones Ana", "Ana - vacaciones"...
const PALABRAS = ['vacaciones', 'tunombre'];

const norm = s => s.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();

function sincronizar() {
  const origen = CalendarApp.getCalendarById(ORIGEN);
  if (!origen) throw new Error('No encuentro el calendario del equipo');
  const destino = CalendarApp.getOwnedCalendarsByName(DESTINO)[0] || CalendarApp.createCalendar(DESTINO);

  const desde = new Date(); desde.setDate(desde.getDate() - 30);
  const hasta = new Date(); hasta.setFullYear(hasta.getFullYear() + 1);
  const clave = e => [e.getId(), e.getStartTime().getTime(), e.getEndTime().getTime()].join('|');

  const coincide = e => { const t = norm(e.getTitle()); return PALABRAS.every(p => t.includes(p)); };
  const fuente = origen.getEvents(desde, hasta).filter(coincide);
  const claves = new Set(fuente.map(clave));

  // Borra las copias cuyo original ya no existe o ha cambiado de fecha.
  const yaCopiados = new Set();
  destino.getEvents(desde, hasta).forEach(c => {
    const k = c.getTag('origen');
    if (!k) return;  // evento puesto a mano
    if (claves.has(k)) yaCopiados.add(k); else c.deleteEvent();
  });

  fuente.forEach(e => {
    const k = clave(e);
    if (yaCopiados.has(k)) return;
    const copia = e.isAllDayEvent()
      ? destino.createAllDayEvent(e.getTitle(), e.getAllDayStartDate(), e.getAllDayEndDate())
      : destino.createEvent(e.getTitle(), e.getStartTime(), e.getEndTime());
    copia.setTag('origen', k);
  });
}

function instalar() {
  ScriptApp.getProjectTriggers().forEach(t => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger('sincronizar').timeBased().everyHours(1).create();
  sincronizar();
}

// Diagnóstico: muestra en el registro los eventos del calendario del equipo que contienen
// "vacac" en el último año y el próximo, y si encajan con PALABRAS (solo se copian los futuros). Ejecutar a mano y mirar el "Registro de ejecución".
function listar() {
  const origen = CalendarApp.getCalendarById(ORIGEN);
  if (!origen) throw new Error('No encuentro el calendario del equipo');
  const desde = new Date(); desde.setFullYear(desde.getFullYear() - 1);  // también el pasado, para comprobar
  const hasta = new Date(); hasta.setFullYear(hasta.getFullYear() + 1);
  origen.getEvents(desde, hasta)
    .filter(e => norm(e.getTitle()).includes('vacac'))
    .forEach(e => {
      const t = norm(e.getTitle());
      const si = PALABRAS.every(p => t.includes(p));
      Logger.log((si ? 'SÍ  ' : 'no  ') + JSON.stringify(e.getTitle()) + '  ' +
        Utilities.formatDate(e.getStartTime(), 'Europe/Madrid', 'yyyy-MM-dd'));
    });
  Logger.log('Zona horaria del script: ' + Session.getScriptTimeZone());
}
