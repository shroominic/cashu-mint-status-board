const MintSorter = (() => {
    // Default weights
    const DEFAULTS = {
        status: true,
        currency: 1000,
        capacity: 500,
        latency: 1,
        mints: 10,
        melts: 10,
        errors: 100
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
            latency: null,
            mints: null,
            melts: null,
            errors: null
        },
        displays: {
            currency: null,
            capacity: null,
            latency: null,
            mints: null,
            melts: null,
            errors: null
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
        elements.inputs.mints = document.getElementById('w_mints');
        elements.inputs.melts = document.getElementById('w_melts');
        elements.inputs.errors = document.getElementById('w_errors');
        
        elements.displays.currency = document.getElementById('val-curr');
        elements.displays.capacity = document.getElementById('val-cap');
        elements.displays.latency = document.getElementById('val-lat');
        elements.displays.mints = document.getElementById('val-mints');
        elements.displays.melts = document.getElementById('val-melts');
        elements.displays.errors = document.getElementById('val-errors');
        
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
        
        ['currency', 'capacity', 'latency', 'mints', 'melts', 'errors'].forEach(key => {
            const input = elements.inputs[key];
            if (input) {
                input.oninput = (e) => {
                    const val = parseInt(e.target.value, 10);
                    state.weights[key] = val;
                    if (elements.displays[key]) {
                        elements.displays[key].innerText = val;
                    }
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
                
                ['currency', 'capacity', 'latency', 'mints', 'melts', 'errors'].forEach(key => {
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
            if (key === 'latency' || key === 'url' || key === 'errors') {
                state.direction = 'asc'; // Asc for latency/name/errors usually better
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
            latency: parseInt(row.dataset.latency || '99999'),
            mints: parseInt(row.dataset.mints || '0'),
            melts: parseInt(row.dataset.melts || '0'),
            errors: parseInt(row.dataset.errors || '0')
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
        if (data.latency >= 99999) {
            score -= 1000 * state.weights.latency;
        } else {
            score -= data.latency * state.weights.latency;
        }

        // 5. Activity Stats (Mints/Melts) modulated by Errors
        // "errors as metric should only modulate the impact of mints/melts and not be considered independently"
        const activityScore = (data.mints * state.weights.mints) + (data.melts * state.weights.melts);
        
        if (activityScore > 0) {
            const totalOps = data.mints + data.melts + data.errors;
            const errorRate = totalOps > 0 ? (data.errors / totalOps) : 0;
            
            // Use weights.errors (default 100) as a percentage scaling factor for impact.
            // 100 means 100% error rate removes 100% of activity score.
            // 200 means 50% error rate removes 100% of activity score.
            const penaltyFactor = errorRate * (state.weights.errors / 100);
            
            // Apply modulation (clamped to 0 to avoid negative activity score)
            const modulation = Math.max(0, 1 - penaltyFactor);
            score += activityScore * modulation;
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
