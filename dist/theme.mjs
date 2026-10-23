export function localHour(date, timeZone) {
  return Number(new Intl.DateTimeFormat('en-GB', {timeZone, hour:'2-digit', hourCycle:'h23'}).format(date));
}
export function resolveTheme(preference, hour) {
  return preference === 'auto' ? (hour >= 7 && hour < 19 ? 'light' : 'dark') : preference;
}
export function readPreference(storage) {
  const saved = storage.getItem('cosens-theme');
  return ['auto','light','dark'].includes(saved) ? saved : 'auto';
}
if (typeof document !== 'undefined') {
  let preference = readPreference(localStorage);
  const controls = document.querySelectorAll('[data-theme-control]');
  controls.forEach(control => {
    control.value = preference;
    control.addEventListener('change', () => {
      preference = control.value;
      localStorage.setItem('cosens-theme', preference);
      controls.forEach(item => item.value = preference);
      apply();
    });
  });
  function apply() {
    const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    const date = new Date();
    const theme = resolveTheme(preference, localHour(date, zone));
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;
    const time = new Intl.DateTimeFormat('en-GB', {timeZone:zone, hour:'2-digit', minute:'2-digit', hourCycle:'h23'}).format(date);
    document.querySelectorAll('[data-local-time]').forEach(el => el.textContent = `${zone.replaceAll('_',' ')} · ${time}`);
    document.querySelectorAll('[data-theme-status]').forEach(el => el.textContent = preference === 'auto' ? `${theme === 'light' ? 'Day' : 'Night'} mode · local time` : `${theme === 'light' ? 'Light' : 'Dark'} mode · manual`);
  }
  apply();
  setInterval(apply, 60_000);
  window.addEventListener('focus', apply);
  document.addEventListener('visibilitychange', () => {if (!document.hidden) apply();});
}
