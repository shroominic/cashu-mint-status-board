const LatencyMonitor = (() => {
    const latencyCache = new Map();
    const pendingRequests = new Map();
    const currencyCache = new Map();
    const pendingCurrencyRequests = new Map();
    const CACHE_DURATION_MS = 10000;
    const REQUEST_TIMEOUT_MS = 5000;
    const REFRESH_INTERVAL_MS = 15000;

    async function measureLatency(mintUrl) {
        const cached = latencyCache.get(mintUrl);
        if (cached && Date.now() - cached.timestamp < CACHE_DURATION_MS) {
            return cached.latency;
        }

        if (pendingRequests.has(mintUrl)) {
            return pendingRequests.get(mintUrl);
        }

        const measurePromise = (async () => {
            try {
                const controller = new AbortController();
                const timeoutId = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
                
                const start = performance.now();
                const response = await fetch(`${mintUrl.replace(/\/$/, '')}/v1/info`, {
                    method: 'GET',
                    signal: controller.signal,
                    cache: 'no-store'
                });
                clearTimeout(timeoutId);
                
                const latency = Math.round(performance.now() - start);
                
                if (response.ok) {
                    latencyCache.set(mintUrl, { latency, timestamp: Date.now() });
                    return latency;
                }
                return null;
            } catch (error) {
                return null;
            } finally {
                pendingRequests.delete(mintUrl);
            }
        })();

        pendingRequests.set(mintUrl, measurePromise);
        return measurePromise;
    }

    async function fetchSupportedUnits(mintUrl) {
        const cached = currencyCache.get(mintUrl);
        if (cached && Date.now() - cached.timestamp < CACHE_DURATION_MS) {
            return cached.units;
        }

        if (pendingCurrencyRequests.has(mintUrl)) {
            return pendingCurrencyRequests.get(mintUrl);
        }

        const fetchPromise = (async () => {
            try {
                const controller = new AbortController();
                const timeoutId = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);

                const response = await fetch(`${mintUrl.replace(/\/$/, '')}/v1/keysets`, {
                    method: 'GET',
                    signal: controller.signal,
                    cache: 'no-store'
                });
                clearTimeout(timeoutId);

                if (!response.ok) {
                    return null;
                }
                const data = await response.json();
                const keysets = Array.isArray(data?.keysets) ? data.keysets : [];
                const units = [...new Set(
                    keysets
                        .filter(ks => ks && ks.active === true && typeof ks.unit === 'string')
                        .map(ks => ks.unit)
                )];

                currencyCache.set(mintUrl, { units, timestamp: Date.now() });
                return units;
            } catch (error) {
                return null;
            } finally {
                pendingCurrencyRequests.delete(mintUrl);
            }
        })();

        pendingCurrencyRequests.set(mintUrl, fetchPromise);
        return fetchPromise;
    }

    function getLatencyClass(latency) {
        if (latency === null) return 'none';
        if (latency <= 300) return 'fast';
        if (latency <= 1000) return 'ok';
        return 'slow';
    }

    function updateLatencyDisplay(cell, latency) {
        if (latency === null) {
            cell.textContent = '-';
            cell.className = '';
        } else {
            const badge = document.createElement('span');
            badge.className = `badge latency ${getLatencyClass(latency)}`;
            badge.textContent = `${latency} ms`;
            cell.textContent = '';
            cell.appendChild(badge);
        }
        const row = cell.closest('tr');
        if (row) {
            row.dataset.latency = latency === null ? 99999 : latency;
        }
    }

    function updateCurrenciesDisplay(cell, units) {
        const row = cell.closest('tr');
        if (!units || units.length === 0) {
            cell.textContent = '-';
            if (row) row.dataset.currencies = 0;
            return;
        }
        cell.textContent = '';
        for (const unit of units) {
            const badge = document.createElement('span');
            badge.className = 'badge unit';
            badge.textContent = unit;
            cell.appendChild(badge);
            cell.appendChild(document.createTextNode(' '));
        }
        if (row) {
            row.dataset.currencies = units.length;
        }
    }

    async function measureAllVisibleMints(staggered = false) {
        const rows = document.querySelectorAll('tbody tr:not(.section-divider)');
        const measurements = [];
        const STAGGER_DELAY_MS = 100;

        for (const row of rows) {
            const linkElement = row.querySelector('td.mint a.link');
            if (!linkElement) continue;

            let mintUrl = linkElement.href;

            // Force HTTPS only if we are on HTTPS (to avoid Mixed Content)
            if (window.location.protocol === 'https:' && mintUrl.startsWith('http:')) {
                mintUrl = mintUrl.replace('http:', 'https:');
            }

            const latencyCell = row.querySelector('td:nth-last-child(1)');
            if (!latencyCell) continue;

            const existingBadge = latencyCell.querySelector('.badge.latency');
            if (!existingBadge && latencyCell.textContent.trim() === '-') {
                latencyCell.innerHTML = '<span class="badge latency none">...</span>';
            }

            if (staggered) {
                await new Promise(resolve => setTimeout(resolve, STAGGER_DELAY_MS));
            }

            measurements.push(
                measureLatency(mintUrl).then(latency => {
                    updateLatencyDisplay(latencyCell, latency);
                })
            );

            const currenciesCell = row.querySelector('td.currencies');
            if (currenciesCell) {
                if (currenciesCell.textContent.trim() === '-') {
                    currenciesCell.innerHTML = '<span class="badge unit">...</span>';
                }
                measurements.push(
                    fetchSupportedUnits(mintUrl).then(units => {
                        updateCurrenciesDisplay(currenciesCell, units);
                    })
                );
            }
        }

        await Promise.all(measurements);
    }

    function observeDashboardChanges() {
        const dashboard = document.getElementById('dashboard');
        if (!dashboard) {
            setTimeout(observeDashboardChanges, 100);
            return;
        }

        const observer = new MutationObserver((mutations) => {
            for (const mutation of mutations) {
                if (mutation.type === 'childList' && mutation.addedNodes.length > 0) {
                    measureAllVisibleMints();
                    break;
                }
            }
        });

        observer.observe(dashboard.parentElement, {
            childList: true,
            subtree: false
        });

        measureAllVisibleMints();
    }

    function startPeriodicRefresh() {
        setInterval(() => {
            latencyCache.clear();
            currencyCache.clear();
            measureAllVisibleMints(true);
        }, REFRESH_INTERVAL_MS);
    }

    return {
        init: () => {
            if (document.readyState === 'loading') {
                document.addEventListener('DOMContentLoaded', () => {
                    observeDashboardChanges();
                    startPeriodicRefresh();
                });
            } else {
                observeDashboardChanges();
                startPeriodicRefresh();
            }
        }
    };
})();

LatencyMonitor.init();
