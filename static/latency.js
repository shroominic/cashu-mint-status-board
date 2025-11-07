const LatencyMonitor = (() => {
    const latencyCache = new Map();
    const pendingRequests = new Map();
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
    }

    async function measureAllVisibleMints(staggered = false) {
        const rows = document.querySelectorAll('tbody tr:not(.section-divider)');
        const measurements = [];
        const STAGGER_DELAY_MS = 100;

        for (const row of rows) {
            const linkElement = row.querySelector('td.mint a.link');
            if (!linkElement) continue;

            const mintUrl = linkElement.href;
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

