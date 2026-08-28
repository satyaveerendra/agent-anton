"""Human-action recorder (SPEC §11): an init script installed at browser-context creation (so every
document, frame and popup is instrumented) that reports click / change / submit / Enter-on-input as
``ElementDescriptor``s through the ``__anton_record`` binding. JS always calls the binding; the
Python side drops calls while the recorder is disarmed. Idempotent (``window.__anton_recorder_installed``).
"""

from __future__ import annotations

__all__ = ["RECORDER_JS", "install_recorder", "arm_loaded_frames"]

RECORDER_JS = r"""
(() => {
  if (window.__anton_recorder_installed) return;
  window.__anton_recorder_installed = true;
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const roleOf = el => {
    const r = el.getAttribute('role'); if (r) return r;
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return el.hasAttribute('href') ? 'link' : 'text';
    if (tag === 'button') return 'button';
    if (tag === 'input') {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      if (t === 'submit' || t === 'button' || t === 'reset' || t === 'image') return 'button';
      if (t === 'checkbox') return 'checkbox'; if (t === 'radio') return 'radio';
      return 'textbox';
    }
    if (tag === 'select') return 'combobox'; if (tag === 'textarea') return 'textbox';
    if (/^h[1-6]$/.test(tag)) return 'heading'; if (tag === 'th') return 'columnheader'; if (tag === 'td') return 'cell';
    if (tag === 'label') return 'label';
    return 'text';
  };
  const prevCellText = cell => { let p = cell.previousElementSibling; while (p) { const t = norm(p.textContent); if (t) return t; p = p.previousElementSibling; } return null; };
  const headerRow = cell => { const row = cell.parentElement; const table = cell.closest('table'); if (!row || !table) return null; const first = table.querySelector('tr'); return (first && first !== row) ? first : null; };
  const labelContext = el => {
    const id = el.getAttribute('id');
    if (id) { const l = document.querySelector('label[for="' + CSS.escape(id) + '"]'); if (l) return norm(l.textContent); }
    const lab = el.closest('label'); if (lab) { const t = norm(lab.textContent); if (t) return t; }
    const cell = el.closest('td,th'); if (cell) { const t = prevCellText(cell); if (t) return t; }
    let p = el.previousElementSibling; while (p) { const t = norm(p.textContent); if (t) return t; p = p.previousElementSibling; }
    return null;
  };
  const describe = el => {
    const tag = el.tagName.toLowerCase();
    const r = el.getBoundingClientRect();
    const inter = el.matches('a[href],button,input,select,textarea,[role="button"],[role="link"],[role="textbox"],[role="checkbox"]');
    const ctx = labelContext(el);
    let text;
    if (tag === 'select') { const o = el.selectedOptions && el.selectedOptions[0]; text = norm(o ? o.textContent : ''); }
    else if (tag === 'input' || tag === 'textarea') text = '';
    else text = norm(el.textContent).slice(0, 200);
    let name;
    const al = el.getAttribute('aria-label');
    if (al) name = norm(al);
    else if (inter && (tag === 'input' || tag === 'select' || tag === 'textarea') && roleOf(el) !== 'button') name = ctx || norm(el.getAttribute('placeholder') || el.getAttribute('title') || el.getAttribute('name') || '');
    else if (tag === 'input') name = norm(el.value || el.getAttribute('title') || '');
    else name = text || norm(el.getAttribute('alt') || el.getAttribute('title') || '');
    let row_anchor = null, col_header = null, row_anchor_col_header = null;
    const cell = el.closest('td,th');
    const hdr = cell ? headerRow(cell) : null;
    if (cell && hdr && cell.parentElement.children.length >= 3) {
      const idx = Array.prototype.indexOf.call(cell.parentElement.children, cell);
      col_header = hdr.children[idx] ? norm(hdr.children[idx].textContent) || null : null;
      for (let i = 0; i < cell.parentElement.children.length; i++) {
        const c = cell.parentElement.children[i]; const t = norm(c.textContent);
        if (c !== cell && t) { row_anchor = t; row_anchor_col_header = hdr.children[i] ? norm(hdr.children[i].textContent) || null : null; break; }
      }
    }
    const type = tag === 'input' ? (el.getAttribute('type') || 'text').toLowerCase() : null;
    const value = (tag === 'input' || tag === 'textarea' || tag === 'select') ? (type === 'password' ? '***' : (el.value ?? null)) : null;
    const checked = (tag === 'input' && (type === 'checkbox' || type === 'radio')) ? !!el.checked : null;
    return { role: roleOf(el), name, text, control_type: tag, bbox: [r.left, r.top, r.width, r.height], label_context: ctx,
             row_anchor, col_header, row_anchor_col_header, value, checked, input_type: type, url: location.href };
  };
  const send = (event, el) => { try { if (window.__anton_record) window.__anton_record({ event, ...describe(el) }); } catch (e) {} };
  const targetOf = ev => { let el = ev.target; if (!(el instanceof Element)) return null; const c = el.closest('a[href],button,input,select,textarea,[role="button"],[role="link"]'); return c || el; };
  document.addEventListener('click', ev => { const el = targetOf(ev); if (el) send('click', el); }, true);
  document.addEventListener('change', ev => { const el = ev.target; if (el instanceof Element) send('change', el); }, true);
  document.addEventListener('submit', ev => { const el = ev.target; if (el instanceof Element) send('submit', el); }, true);
  document.addEventListener('keydown', ev => { if (ev.key === 'Enter') { const el = ev.target; if (el instanceof Element && el.matches('input,textarea')) send('enter', el); } }, true);
})();
"""


def install_recorder(context, callback) -> None:  # noqa: ANN001 - Playwright objects
    """At context creation, before any navigation (SPEC §11)."""
    context.add_init_script(RECORDER_JS)
    context.expose_binding("__anton_record", callback)


def arm_loaded_frames(page) -> None:  # noqa: ANN001
    """Safety net on arming: evaluate the (idempotent) recorder into every frame already loaded."""
    for frame in page.frames:
        try:
            frame.evaluate(RECORDER_JS)
        except Exception:  # noqa: BLE001, S110 - a navigating frame is instrumented by the init script anyway
            pass
