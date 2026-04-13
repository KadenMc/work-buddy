"""Command palette JS."""

from __future__ import annotations


def _command_palette_script() -> str:
    return """
// ---- Command Palette ----
(function() {
    let _cpCommands = [];
    let _cpFiltered = [];
    let _cpIndex = 0;
    let _cpOpen = false;
    let _cpProviderFilter = 'all';  // 'all', 'obsidian', or 'work-buddy'
    let _cpCacheTime = 0;
    let _cpParamCmd = null;  // currently showing param form for

    const overlay = document.getElementById('cp-overlay');
    const input = document.getElementById('cp-input');
    const results = document.getElementById('cp-results');
    const paramForm = document.getElementById('cp-param-form');
    const statusLeft = document.getElementById('cp-status-left');

    // ---- Open / Close ----

    window.cpOpen = async function() {
        if (_cpOpen) { cpClose(); return; }
        _cpOpen = true;
        _cpParamCmd = null;
        paramForm.className = 'cp-param-form';
        paramForm.innerHTML = '';
        results.style.display = '';
        overlay.classList.add('open');
        input.value = '';
        input.focus();

        // Fetch commands if cache is stale (>60s)
        if (Date.now() - _cpCacheTime > 60000 || _cpCommands.length === 0) {
            results.innerHTML = '<div class="cp-empty">Loading commands...</div>';
            statusLeft.textContent = 'Loading...';
            try {
                const data = await fetchJSON('/api/palette/commands');
                if (data && data.commands) {
                    _cpCommands = data.commands;
                    _cpCacheTime = Date.now();
                    const prov = data.providers || {};
                    const parts = [];
                    for (const [k, v] of Object.entries(prov)) {
                        parts.push(k + ': ' + (v.count || 0));
                    }
                    statusLeft.textContent = parts.join(' \\u00b7 ') || 'Ready';
                }
            } catch (e) {
                results.innerHTML = '<div class="cp-empty">Failed to load commands</div>';
                statusLeft.textContent = 'Error';
                return;
            }
        }
        _cpFilter('');
    };

    window.cpClose = function() {
        _cpOpen = false;
        _cpParamCmd = null;
        _cpProviderFilter = 'all';
        overlay.classList.remove('open');
        input.value = '';
        _cpIndex = 0;
        // Reset filter pills
        document.querySelectorAll('.cp-filter-pill').forEach(p => {
            p.className = 'cp-filter-pill';
            if (p.dataset.cpFilter === 'all') p.classList.add('active-all');
        });
    };

    // ---- Client-side fuzzy scoring (instant, used while typing fast) ----

    function _score(query, text) {
        if (!query) return 1;
        const q = query.toLowerCase();
        const t = text.toLowerCase();
        if (t.startsWith(q)) return 100 + (1 / t.length);
        const idx = t.indexOf(q);
        if (idx >= 0) return 60 + (1 / (idx + 1));
        let qi = 0;
        for (let ti = 0; ti < t.length && qi < q.length; ti++) {
            if (t[ti] === q[qi]) qi++;
        }
        if (qi === q.length) return 40;
        return 0;
    }

    function _cpFilterLocal(query) {
        const pool = _cpProviderFilter === 'all'
            ? _cpCommands
            : _cpCommands.filter(c => c.provider === _cpProviderFilter);
        const scored = [];
        for (const cmd of pool) {
            const s = Math.max(
                _score(query, cmd.name),
                _score(query, cmd.description || ''),
                _score(query, cmd.category || '') * 0.8,
                cmd.slash_command ? _score(query, cmd.slash_command) * 0.9 : 0
            );
            if (s > 0) scored.push({ cmd, score: s });
        }
        scored.sort((a, b) => b.score - a.score);
        _cpFiltered = scored.map(s => s.cmd);
        _cpIndex = 0;
        _cpRender();
    }

    // ---- Server-side hybrid search (debounced, BM25 + semantic) ----

    let _cpSearchTimer = null;
    let _cpSearchVersion = 0;

    function _cpFilter(query) {
        // Instant: client-side fuzzy filter for responsive feel
        _cpFilterLocal(query);

        // Debounced: server-side hybrid search for quality results
        clearTimeout(_cpSearchTimer);
        if (!query || query.length < 2) return;

        _cpSearchTimer = setTimeout(async () => {
            const version = ++_cpSearchVersion;
            try {
                const data = await fetchJSON('/api/palette/commands?q=' + encodeURIComponent(query));
                if (!data || version !== _cpSearchVersion) return;
                if (!_cpOpen) return;
                let cmds = data.commands || [];
                if (_cpProviderFilter !== 'all') {
                    cmds = cmds.filter(c => c.provider === _cpProviderFilter);
                }
                _cpFiltered = cmds;
                _cpIndex = 0;
                _cpRender();
                const method = data.search_method || 'unknown';
                statusLeft.textContent = _cpFiltered.length + ' results (' + method + ')';
            } catch (e) {
                // Keep client-side results on failure
            }
        }, 250);
    }

    function _cpRender() {
        results.innerHTML = '';

        if (_cpFiltered.length === 0) {
            results.innerHTML = '<div class="cp-empty">No matching commands</div>';
            return;
        }

        // Group by provider + category
        const groups = [];
        let lastKey = '';
        for (let i = 0; i < _cpFiltered.length; i++) {
            const cmd = _cpFiltered[i];
            const key = cmd.provider + '/' + cmd.category;
            if (key !== lastKey) {
                groups.push({ label: cmd.category || cmd.provider, items: [] });
                lastKey = key;
            }
            groups[groups.length - 1].items.push({ cmd, index: i });
        }

        for (const g of groups) {
            const label = document.createElement('div');
            label.className = 'cp-group-label';
            label.textContent = g.label;
            results.appendChild(label);

            for (const { cmd, index } of g.items) {
                const item = document.createElement('div');
                item.className = 'cp-item' + (index === _cpIndex ? ' active' : '');
                item.dataset.index = index;

                // Type icon
                const typeIcon = document.createElement('span');
                const ctype = cmd.command_type || 'inline';
                typeIcon.className = 'cp-type-icon cp-type-' + ctype;
                const typeSymbols = { inline: '\\u25B8', parameterized: '\\u25A3', workflow: '\\u2699' };
                const typeTitles = { inline: 'Runs instantly', parameterized: 'Opens parameter form', workflow: 'Launches agent session' };
                typeIcon.textContent = typeSymbols[ctype] || '\\u25B8';
                typeIcon.title = typeTitles[ctype] || '';
                item.appendChild(typeIcon);

                const nameSpan = document.createElement('span');
                nameSpan.className = 'cp-item-name';
                nameSpan.textContent = cmd.name;
                item.appendChild(nameSpan);

                if (cmd.slash_command) {
                    const slashSpan = document.createElement('span');
                    slashSpan.className = 'cp-item-desc';
                    slashSpan.style.cssText = 'font-family:monospace;font-size:11px;color:var(--accent);opacity:0.8';
                    slashSpan.textContent = '/' + cmd.slash_command;
                    item.appendChild(slashSpan);
                } else if (cmd.description) {
                    const descSpan = document.createElement('span');
                    descSpan.className = 'cp-item-desc';
                    descSpan.textContent = cmd.description.slice(0, 80);
                    item.appendChild(descSpan);
                }

                const provSpan = document.createElement('span');
                provSpan.className = 'cp-item-provider ' + (cmd.provider === 'obsidian' ? 'cp-prov-obsidian' : 'cp-prov-workbuddy');
                provSpan.textContent = cmd.provider;
                item.appendChild(provSpan);

                // Direct click handler — no delegation needed
                item.addEventListener('click', ((idx) => () => window.cpExec(idx))(index));

                item.addEventListener('mouseenter', ((idx) => () => window.cpHover(idx))(index));

                results.appendChild(item);
            }
        }

        // Scroll active into view
        const activeEl = results.querySelector('.cp-item.active');
        if (activeEl) activeEl.scrollIntoView({ block: 'nearest' });
    }

    function _esc(s) {
        const d = document.createElement('div');
        d.textContent = s;
        return d.innerHTML;
    }

    // ---- Hover / Navigate ----

    window.cpHover = function(i) {
        _cpIndex = i;
        // Update active class in-place — do NOT call _cpRender() here.
        // _cpRender() destroys and recreates all DOM nodes (results.innerHTML=''),
        // which removes the element under the cursor between mousedown and mouseup,
        // causing clicks on .cp-item to silently disappear.
        results.querySelectorAll('.cp-item').forEach(el => {
            el.classList.toggle('active', parseInt(el.dataset.index) === i);
        });
    };

    function _cpNav(delta) {
        _cpIndex = Math.max(0, Math.min(_cpFiltered.length - 1, _cpIndex + delta));
        _cpRender();
    }

    // ---- Execute ----

    window.cpExec = function(index) {
        const cmd = _cpFiltered[index];
        if (!cmd) return;

        if (cmd.has_params && cmd.parameters && Object.keys(cmd.parameters).length > 0) {
            _cpShowParams(cmd);
            return;
        }

        const cmdId = cmd.id;
        const cmdName = cmd.name;
        cpClose();
        _cpRunCommand(cmdId, {}, cmdName);
    };

    function _cpToast(title, body, isError) {
        const container = document.getElementById('toast-container');
        if (!container) return;
        const toast = document.createElement('div');
        toast.className = isError ? 'toast toast-request' : 'toast toast-note';
        toast.style.cursor = 'pointer';
        const pill = isError
            ? '<span class="type-pill request1">ERROR</span>'
            : '<span class="type-pill note1">OK</span>';
        toast.innerHTML = '<div class="toast-header">'
            + '<div style="display:flex;align-items:center;gap:6px">' + pill
            + '<span class="toast-title">' + _esc(title) + '</span></div>'
            + '<button class="toast-close">\\u2715</button></div>'
            + '<div class="toast-body">' + _esc(body) + '</div>';
        toast.addEventListener('click', () => toast.remove());
        container.appendChild(toast);
        setTimeout(() => { if (toast.parentNode) toast.remove(); }, 5000);
    }

    async function _cpRunCommand(commandId, params, displayName) {
        if (_readOnly) { _cpToast('Dashboard is in read-only mode', true); return; }
        try {
            const resp = await fetch('/api/palette/execute', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ command_id: commandId, params: params }),
            });
            const data = await resp.json();
            if (data.success) {
                if (data.awaiting_consent || data.view_id) {
                    // View created — tab auto-opens via polling, no toast needed
                } else {
                    _cpToast(displayName, data.result || 'Done', false);
                }
            } else {
                _cpToast(displayName, data.error || 'Unknown error', true);
            }
        } catch (e) {
            _cpToast('Network error', e.message, true);
        }
    }

    // ---- Param form ----

    function _cpShowParams(cmd) {
        _cpParamCmd = cmd;
        results.style.display = 'none';
        paramForm.className = 'cp-param-form open';

        let html = '<div class="cp-param-title">' + _esc(cmd.name) + '</div>';
        const paramEntries = Object.entries(cmd.parameters);
        for (const [pname, pschema] of paramEntries) {
            const req = pschema.required ? ' *' : '';
            const desc = pschema.description || '';
            const ptype = pschema.type || 'str';
            html += '<div class="cp-param-field">';
            html += '<label>' + _esc(pname) + req + '</label>';
            if (ptype === 'bool') {
                html += '<select data-param="' + _esc(pname) + '">'
                    + '<option value="">-- select --</option>'
                    + '<option value="true">true</option>'
                    + '<option value="false">false</option>'
                    + '</select>';
            } else {
                html += '<input type="text" data-param="' + _esc(pname) + '" placeholder="' + _esc(ptype) + '" />';
            }
            if (desc) html += '<div class="cp-param-hint">' + _esc(desc) + '</div>';
            html += '</div>';
        }
        html += '<div class="cp-param-actions">'
            + '<button class="cp-btn-cancel" onclick="cpParamBack()">Back</button>'
            + '<button class="cp-btn-run" onclick="cpParamSubmit()">Run</button>'
            + '</div>';
        paramForm.innerHTML = html;

        // Focus first input
        const firstInput = paramForm.querySelector('input, select');
        if (firstInput) firstInput.focus();
    }

    window.cpParamBack = function() {
        _cpParamCmd = null;
        paramForm.className = 'cp-param-form';
        paramForm.innerHTML = '';
        results.style.display = '';
        input.focus();
    };

    window.cpParamSubmit = function() {
        if (!_cpParamCmd) return;
        const params = {};
        const fields = paramForm.querySelectorAll('[data-param]');
        for (const f of fields) {
            const name = f.dataset.param;
            let val = f.value.trim();
            if (!val) continue;
            // Type coercion
            const pschema = _cpParamCmd.parameters[name] || {};
            if (pschema.type === 'int') val = parseInt(val, 10);
            else if (pschema.type === 'float') val = parseFloat(val);
            else if (pschema.type === 'bool') val = val === 'true';
            params[name] = val;
        }
        const displayName = _cpParamCmd.name;
        const commandId = _cpParamCmd.id;
        cpClose();
        _cpRunCommand(commandId, params, displayName);
    };

    // ---- Keyboard ----

    document.addEventListener('keydown', function(e) {
        // Ctrl+K / Cmd+K to toggle
        if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
            e.preventDefault();
            e.stopPropagation();
            cpOpen();
            return;
        }
    });

    input.addEventListener('keydown', function(e) {
        if (e.key === 'Escape') {
            e.preventDefault();
            if (_cpParamCmd) { cpParamBack(); return; }
            cpClose();
        } else if (e.key === 'ArrowDown') {
            e.preventDefault();
            _cpNav(1);
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            _cpNav(-1);
        } else if (e.key === 'Enter') {
            e.preventDefault();
            cpExec(_cpIndex);
        }
    });

    input.addEventListener('input', function() {
        if (_cpParamCmd) return;  // don't filter while in param form
        _cpFilter(input.value);
    });

    // Also handle Enter in param form fields
    paramForm.addEventListener('keydown', function(e) {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            cpParamSubmit();
        } else if (e.key === 'Escape') {
            e.preventDefault();
            cpParamBack();
        }
    });

    // Click + hover handled by per-item listeners in _cpRender() — no delegation needed.

    // Source filter pills — pure client-side whitelist, no IR re-computation
    function _cpSetProviderFilter(filter) {
        _cpProviderFilter = filter;
        document.querySelectorAll('.cp-filter-pill').forEach(p => {
            p.className = 'cp-filter-pill';
            if (p.dataset.cpFilter === filter) {
                if (filter === 'all') p.classList.add('active-all');
                else if (filter === 'obsidian') p.classList.add('active-obsidian');
                else p.classList.add('active-workbuddy');
            }
        });
        // Client-side only — just re-filter the cached commands
        _cpFilterLocal(input.value);
        input.focus();
    }

    document.querySelectorAll('.cp-filter-pill').forEach(pill => {
        pill.addEventListener('click', function(e) {
            e.stopPropagation();
            const clicked = this.dataset.cpFilter;
            // Toggle: clicking active filter resets to 'all'
            const next = (clicked === _cpProviderFilter && clicked !== 'all') ? 'all' : clicked;
            _cpSetProviderFilter(next);
        });
    });
})();
"""
