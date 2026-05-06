"""Dashboard Contracts tab JS.

Lists active contracts with their evidence-plan + stop-rule progress.
Contracts live in the Obsidian vault under ``contracts/`` (configured
via ``contracts.vault_path``); the loader fetches via ``/api/contracts``.
"""

from __future__ import annotations


def script() -> str:
    return r"""
// ---- Contracts ----
async function loadContracts() {
    const data = await fetchJSON('/api/contracts');
    if (!data) return;

    const contracts = data.contracts || [];
    if (contracts.length === 0) {
        document.getElementById('contracts-table').innerHTML = '<div class="empty-state">No active contracts</div>';
        return;
    }

    let rows = contracts.map(c => {
        const noteLink = c.vault_path
            ? `<a href="obsidian://open?vault=${encodeURIComponent(WB_VAULT_NAME)}&file=${encodeURIComponent(c.vault_path)}" title="Open contract in Obsidian" style="text-decoration:none;cursor:pointer;margin-left:6px;">&#x1F4D3;</a>`
            : '';
        return `
        <tr>
            <td><strong>${c.title}</strong>${noteLink}</td>
            <td>${statusBadge(c.status)}</td>
            <td>${c.type || '—'}</td>
            <td>${c.deadline || '—'}</td>
            <td>${c.priority || '—'}</td>
        </tr>
    `;
    }).join('');

    document.getElementById('contracts-table').innerHTML = `
        <table class="data-table">
            <thead><tr><th>Contract</th><th>Status</th><th>Type</th><th>Deadline</th><th>Priority</th></tr></thead>
            <tbody>${rows}</tbody>
        </table>
    `;
}
"""
