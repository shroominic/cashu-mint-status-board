const MintSorter = (() => {
    // Default weights
    const DEFAULTS = {
        status: true,
        currency: 1000,
        capacity: 500,
        latency: 1
    };

    // State
    const state = {
        mode: 'weighted', // 'weighted' or 'column'
        columnKey: null,
        direction: 'desc',
        weights: { ...DEFAULTS }
    };

    // DOM Elements
    const elements = {
        table: null,
        tbody: null,
        inputs: {
            status: null,
            currency: null,
            capacity: null,
            latency: null
        },
        displays: {
            currency: null,
            capacity: null,
            latency: null
        },
        resetBtn: null
    };

    function init() {
        cacheElements();
        bindEvents();
        // Initial sort
        applySort();
        
        // HTMX hook to re-apply sort after refresh
        document.body.addEventListener('htmx:afterSwap', (evt) => {
            if (evt.detail.target.id === 'dashboard') {
                cacheTableElements(); // Re-cache table elements as they were replaced
                applySort();
            }
        });
    }

    function cacheElements() {
        elements.inputs.status = document.getElementById('w_status');
        elements.inputs.currency = document.getElementById('w_currency');
        elements.inputs.capacity = document.getElementById('w_capacity');
        elements.inputs.latency = document.getElementById('w_latency');
        
        elements.displays.currency = document.getElementById('val-curr');
        elements.displays.capacity = document.getElementById('val-cap');
        elements.displays.latency = document.getElementById('val-lat');
        
        elements.resetBtn = document.getElementById('reset-sort');
        
        cacheTableElements();
    }

    function cacheTableElements() {
        elements.table = document.getElementById('mint-table');
        elements.tbody = document.getElementById('dashboard');
        
        if (elements.table) {
            const headers = elements.table.querySelectorAll('th.sortable');
            headers.forEach(th => {
                th.onclick = () => handleHeaderClick(th);
                
                // Update visual state of header
                th.classList.remove('asc', 'desc');
                if (state.mode === 'column' && th.dataset.key === state.columnKey) {
                    th.classList.add(state.direction);
                }
            });
        }
    }

    function bindEvents() {
        // Inputs
        if (elements.inputs.status) {
            elements.inputs.status.onchange = (e) => {
                state.weights.status = e.target.checked;
                state.mode = 'weighted';
                applySort();
            };
        }
        
        ['currency', 'capacity', 'latency'].forEach(key => {
            const input = elements.inputs[key];
            if (input) {
                input.oninput = (e) => {
                    const val = parseInt(e.target.value, 10);
                    state.weights[key] = val;
                    elements.displays[key].innerText = val;
                    state.mode = 'weighted';
                    applySort();
                };
            }
        });

        if (elements.resetBtn) {
            elements.resetBtn.onclick = () => {
                // Reset State
                state.mode = 'weighted';
                state.weights = { ...DEFAULTS };
                
                // Reset UI
                if (elements.inputs.status) elements.inputs.status.checked = DEFAULTS.status;
                
                ['currency', 'capacity', 'latency'].forEach(key => {
                    if (elements.inputs[key]) {
                        elements.inputs[key].value = DEFAULTS[key];
                    }
                    if (elements.displays[key]) {
                        elements.displays[key].innerText = DEFAULTS[key];
                    }
                });

                applySort();
            };
        }
    }

    function handleHeaderClick(th) {
        const key = th.dataset.key;
        if (state.mode === 'column' && state.columnKey === key) {
            state.direction = state.direction === 'desc' ? 'asc' : 'desc';
        } else {
            state.mode = 'column';
            state.columnKey = key;
            state.direction = 'desc'; // Default to desc for most stats
            if (key === 'latency' || key === 'url') {
                state.direction = 'asc'; // Asc for latency/name usually better
            }
        }
        applySort();
    }

    function getRowData(row) {
        return {
            element: row,
            url: row.dataset.url,
            isUp: parseInt(row.dataset.up || '0'),
            uptime: parseFloat(row.dataset.uptime || '0'),
            capacity: parseInt(row.dataset.capacity || '0'),
            channels: parseInt(row.dataset.channels || '0'),
            currencies: parseInt(row.dataset.currencies || '0'),
            latency: parseInt(row.dataset.latency || '99999')
        };
    }

    function calculateScore(data) {
        let score = 0;
        
        // 1. Status
        if (state.weights.status) {
            score += data.isUp ? 1_000_000_000 : 0;
        }
        
        // 2. Currencies
        score += data.currencies * state.weights.currency;
        
        // 3. Capacity (logarithmic)
        if (data.capacity > 0) {
            score += Math.log10(data.capacity) * state.weights.capacity;
        }
        
        // 4. Latency (penalty)
        // If latency is unknown (99999), apply big penalty
        if (data.latency >= 99999) {
            score -= 1000 * state.weights.latency;
        } else {
            score -= data.latency * state.weights.latency;
        }
        
        return score;
    }

    function applySort() {
        if (!elements.tbody) return;

        const rows = Array.from(elements.tbody.querySelectorAll('tr:not(.section-divider)'));
        const rowData = rows.map(getRowData);

        // Sort
        rowData.sort((a, b) => {
            if (state.mode === 'weighted') {
                const scoreA = calculateScore(a);
                const scoreB = calculateScore(b);
                return scoreB - scoreA; // Descending score
            } else {
                // Column sort
                let valA = a[state.columnKey];
                let valB = b[state.columnKey];
                
                if (state.columnKey === 'ln_name') {
                    // Dirty hack: read text content of 4th column
                    valA = a.element.children[3].innerText.trim();
                    valB = b.element.children[3].innerText.trim();
                }

                if (valA < valB) return state.direction === 'asc' ? -1 : 1;
                if (valA > valB) return state.direction === 'asc' ? 1 : -1;
                return 0;
            }
        });

        // Reorder DOM
        const divider = elements.tbody.querySelector('.section-divider');
        if (divider) divider.remove();
        
        rows.forEach(r => r.remove());
        
        rowData.forEach(d => {
            elements.tbody.appendChild(d.element);
        });

        // Update headers visual state
        if (elements.table) {
            elements.table.querySelectorAll('th.sortable').forEach(th => {
                th.classList.remove('asc', 'desc');
                if (state.mode === 'column' && th.dataset.key === state.columnKey) {
                    th.classList.add(state.direction);
                }
            });
        }
    }

    return { init };
})();

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', MintSorter.init);
} else {
    MintSorter.init();
}
