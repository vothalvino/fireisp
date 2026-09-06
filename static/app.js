document.querySelectorAll('select[data-lookup]').forEach(select => {
  const input = document.createElement('input');
  input.type = 'search';
  input.placeholder = select.dataset.lookup === 'customer' ? 'Buscar nombre o número de cliente…' : 'Buscar cliente o usuario de servicio…';
  input.setAttribute('aria-label', input.placeholder);
  input.autocomplete = 'off';
  input.className = 'lookup-search';
  const status = document.createElement('span');
  status.className = 'helptext';
  status.setAttribute('role', 'status');
  select.before(input);
  select.after(status);
  if (select.multiple) select.size = 6;
  let timer;
  let controller;
  const search = async () => {
    if (controller) controller.abort();
    controller = new AbortController();
    status.textContent = 'Buscando…';
    try {
      const result = await fetch(`/lookup/${select.dataset.lookup}/?q=${encodeURIComponent(input.value)}`, {signal: controller.signal, headers: {'Accept': 'application/json'}});
      if (!result.ok) throw new Error('lookup');
      const data = await result.json();
      const keep = Array.from(select.selectedOptions).filter(option => option.value).map(option => {
        const copy = option.cloneNode(true);
        copy.selected = true;
        return copy;
      });
      const selectedValues = new Set(keep.map(option => option.value));
      const placeholder = new Option('Selecciona un resultado…', '');
      if (select.multiple) placeholder.disabled = true;
      select.replaceChildren(placeholder);
      for (const option of keep) select.append(option);
      for (const item of data.results) {
        if (selectedValues.has(String(item.id))) continue;
        select.append(new Option(item.label, item.id));
      }
      if (keep.length && !select.multiple) select.value = keep[0].value;
      status.textContent = data.results.length ? `${data.results.length} ${data.results.length === 1 ? 'resultado' : 'resultados'}. Selecciona uno de la lista.` : 'Sin resultados. Cambia tu búsqueda.';
      if (select.multiple) status.textContent += ' Mantén Ctrl o ⌘ para seleccionar varios.';
    } catch (error) {
      if (error.name !== 'AbortError') status.textContent = 'No se pudo consultar. Vuelve a escribir para intentar de nuevo.';
    }
  };
  input.addEventListener('input', () => {clearTimeout(timer); timer = setTimeout(search, 250);});
  search();
});
