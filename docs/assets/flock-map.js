(function () {
    'use strict';

    const TIER_LABELS = {
        ssid_correlated: 'SSID-correlated',
        oui_high: 'OUI high',
        oui_mfr: 'OUI contract manufacturer',
        identified_other: 'identified as other hardware',
    };
    const CONFIDENCE_LEVELS = Object.keys(TIER_LABELS);
    const DEFAULT_CENTER = [-98.5795, 39.8283];
    const DETAIL_ZOOM = 12;
    const MAX_RESULTS = 200;
    maplibregl.setWorkerUrl(new URL(
        'vendor/maplibre-gl-5.19.0/maplibre-gl-csp-worker.js',
        document.baseURI,
    ).href);
    const config = Object.assign({
        tileArchive: document.querySelector('meta[name="flock-tile-archive"]')?.content || 'assets/flock-cameras.pmtiles',
        tileChunks: document.querySelector('meta[name="flock-tile-chunks"]')?.content || '',
        tileChunkSize: 256 * 1024,
        bssidIndexPath: document.querySelector('meta[name="flock-bssid-index"]')?.content || 'assets/bssid',
        basemapStyleUrl: document.querySelector('meta[name="basemap-style"]')?.content || 'https://tiles.openfreemap.org/styles/dark',
        geocoderUrl: document.querySelector('meta[name="geocoder-url"]')?.content || 'https://nominatim.openstreetmap.org/search',
        geocoderMinIntervalMs: 1000,
        geocoderCacheMs: 30 * 24 * 60 * 60 * 1000,
    }, window.FLOCK_FINDER_CONFIG || {});

    const searchStatus = document.getElementById('search-status');
    const resultsList = document.getElementById('results-list');
    const resultsLive = document.getElementById('results-live');
    const resultsHeading = document.getElementById('results-heading');
    const dialog = document.getElementById('result-dialog');
    const dialogBody = document.getElementById('result-dialog-body');
    let stats = null;
    let metadata = null;
    let firstIdleRecorded = false;
    let lastGeocoderStart = 0;
    let pendingResultFocus = false;
    const geocoderMemoryCache = new Map();

    function escapeHtml(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function cameraSourceDefinition() {
        return {
            type: 'vector',
            url: archiveUrl,
            attribution: 'Camera observations: WiGLE',
            minzoom: 0,
            maxzoom: 12,
        };
    }

    function cameraLayerDefinitions() {
        return [
            {
                id: 'camera-clusters',
                type: 'circle',
                source: 'cameras',
                'source-layer': 'cameras',
                minzoom: 0,
                maxzoom: DETAIL_ZOOM,
                filter: ['all', ['has', 'point_count'], ['>', clusterCountExpression(), 0]],
                paint: {
                    'circle-color': '#b52323',
                    'circle-opacity': 0.86,
                    'circle-stroke-color': '#fff',
                    'circle-stroke-width': 1.5,
                    'circle-radius': ['step', clusterCountExpression(), 11, 10, 15, 100, 21, 1000, 29],
                },
            },
            {
                id: 'camera-points',
                type: 'circle',
                source: 'cameras',
                'source-layer': 'cameras',
                minzoom: DETAIL_ZOOM,
                filter: detailFilter(),
                paint: {
                    'circle-color': [
                        'match', ['get', 'confidence'],
                        'ssid_correlated', '#00e676',
                        'oui_high', '#ff3b3b',
                        'oui_mfr', '#ffb300',
                        '#969696',
                    ],
                    'circle-radius': 5,
                    'circle-stroke-color': '#fff',
                    'circle-stroke-width': 1.5,
                },
            },
        ];
    }

    function setSearchStatus(message, state = '') {
        searchStatus.textContent = message;
        searchStatus.dataset.state = state;
    }

    function normalizeBssid(value) {
        const compact = String(value).replace(/[^0-9a-f]/gi, '').toUpperCase();
        return compact.length === 12 && /^[0-9A-F]{12}$/.test(compact) ? compact : '';
    }

    function isBssidShaped(value) {
        const input = String(value).trim();
        return /^(?:[0-9a-z]{2}[:-]){5}[0-9a-z]{2}$/i.test(input)
            || /^[0-9a-z]{12}$/i.test(input)
            || /^(?:[0-9a-z]{4}\.){2}[0-9a-z]{4}$/i.test(input);
    }

    function activeConfidenceFilters() {
        return Array.from(document.querySelectorAll('#tier-filters input[data-confidence]'))
            .filter(input => input.checked)
            .map(input => input.dataset.confidence);
    }

    function clusterCountExpression() {
        const inMarketOnly = document.getElementById('filter-hide-out-of-market').checked;
        const prefix = inMarketOnly ? 'count_in_market_' : 'count_';
        const terms = activeConfidenceFilters().map(level => ['coalesce', ['get', `${prefix}${level}`], 0]);
        return terms.length ? ['+', ...terms] : 0;
    }

    function detailFilter() {
        const selected = activeConfidenceFilters();
        const clauses = [
            ['!', ['has', 'point_count']],
            ['in', ['get', 'confidence'], ['literal', selected]],
        ];
        if (document.getElementById('filter-hide-out-of-market').checked) {
            clauses.push(['==', ['get', 'out_of_market'], false]);
        }
        return ['all', ...clauses];
    }

    class ChunkedPmtilesSource {
        constructor(root, chunkSize) {
            this.root = new URL(root, document.baseURI).href.replace(/\/$/, '');
            this.chunkSize = chunkSize;
        }

        getKey() {
            return `${this.root}/archive.pmtiles`;
        }

        async getBytes(offset, length, signal) {
            const firstChunk = Math.floor(offset / this.chunkSize);
            const lastChunk = Math.floor((offset + length - 1) / this.chunkSize);
            const chunks = await Promise.all(Array.from(
                {length: lastChunk - firstChunk + 1},
                async (_, position) => {
                    const index = firstChunk + position;
                    const response = await fetch(`${this.root}/chunk-${String(index).padStart(6, '0')}.bin`, {
                        cache: 'force-cache',
                        signal,
                    });
                    if (!response.ok) throw new Error(`Map chunk request returned ${response.status}`);
                    return new Uint8Array(await response.arrayBuffer());
                },
            ));
            const joinedLength = chunks.reduce((total, chunk) => total + chunk.length, 0);
            const joined = new Uint8Array(joinedLength);
            let cursor = 0;
            for (const chunk of chunks) {
                joined.set(chunk, cursor);
                cursor += chunk.length;
            }
            const start = offset - firstChunk * this.chunkSize;
            const data = joined.slice(start, start + length);
            return {data: data.buffer};
        }
    }

    const protocol = new pmtiles.Protocol();
    maplibregl.addProtocol('pmtiles', protocol.tile);
    let archiveKey = new URL(config.tileArchive, document.baseURI).href;
    if (config.tileChunks) {
        const chunkedSource = new ChunkedPmtilesSource(config.tileChunks, config.tileChunkSize);
        archiveKey = chunkedSource.getKey();
        protocol.add(new pmtiles.PMTiles(chunkedSource));
    }
    const archiveUrl = `pmtiles://${archiveKey}`;
    const map = new maplibregl.Map({
        container: 'map',
        center: DEFAULT_CENTER,
        zoom: 3.2,
        minZoom: 1,
        maxZoom: 19,
        attributionControl: true,
        cooperativeGestures: true,
        style: {
            version: 8,
            sources: {
                cameras: cameraSourceDefinition(),
            },
            layers: [
                {id: 'basemap-fallback', type: 'background', paint: {'background-color': '#0d0d12'}},
                ...cameraLayerDefinitions(),
            ],
        },
    });
    map.addControl(new maplibregl.NavigationControl({showCompass: false}), 'top-right');

    async function loadBasemap() {
        const response = await fetch(config.basemapStyleUrl, {headers: {'Accept': 'application/json'}});
        if (!response.ok) throw new Error(`Basemap style request returned ${response.status}`);
        const style = await response.json();
        if (style.version !== 8 || !style.sources || !Array.isArray(style.layers)) {
            throw new Error('Basemap style response is invalid');
        }
        style.sources = {...style.sources, cameras: cameraSourceDefinition()};
        style.layers = [...style.layers, ...cameraLayerDefinitions()];
        map.setStyle(style);
    }

    loadBasemap().catch(() => {
        // The local camera layer remains usable over the dark fallback.
    });

    function selectedClusterCount(properties) {
        const prefix = document.getElementById('filter-hide-out-of-market').checked
            ? 'count_in_market_'
            : 'count_';
        return activeConfidenceFilters().reduce((total, level) =>
            total + Number(properties[`${prefix}${level}`] || 0), 0);
    }

    function evidenceLabel(confidence) {
        return TIER_LABELS[confidence] || confidence || 'unclassified';
    }

    function locationLabel(properties) {
        return [properties.road, properties.city, properties.region, properties.country]
            .filter(Boolean)
            .join(', ') || 'Location details unavailable';
    }

    function detailsHtml(properties) {
        const row = (label, value) => `<strong>${label}:</strong> ${escapeHtml(value || 'Unknown')}<br>`;
        const outside = properties.out_of_market === true || properties.out_of_market === 1;
        return `${row('BSSID', properties.netid)}${row('SSID', properties.ssid || '(hidden)')}`
            + `${row('OUI', properties.oui)}${row('Evidence', evidenceLabel(properties.confidence))}`
            + `${row('Channel', properties.channel)}${row('Location', locationLabel(properties))}`
            + `${row('First seen', properties.firsttime)}${row('Last seen', properties.lasttime)}`
            + `${row('Country', properties.country)}${outside ? '<strong>Market note:</strong> Outside the primary US market<br>' : ''}`;
    }

    function showDetails(properties) {
        dialogBody.innerHTML = detailsHtml(properties);
        dialog.showModal();
        document.getElementById('result-dialog-close').focus();
    }

    function uniqueRenderedFeatures(layerId, keyFor) {
        const seen = new Set();
        return map.queryRenderedFeatures({layers: [layerId]}).filter(feature => {
            const key = keyFor(feature);
            if (seen.has(key)) return false;
            seen.add(key);
            return true;
        });
    }

    function cameraResultsReady() {
        return Boolean(map.getLayer('camera-clusters'))
            && Boolean(map.getLayer('camera-points'))
            && map.isSourceLoaded('cameras');
    }

    function resultButton(primary, secondary, evidence, action, label) {
        const item = document.createElement('li');
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'result-button';
        button.setAttribute('aria-label', label);
        const primaryNode = document.createElement('span');
        primaryNode.className = 'result-primary';
        primaryNode.textContent = primary;
        const secondaryNode = document.createElement('span');
        secondaryNode.className = 'result-secondary';
        secondaryNode.textContent = secondary;
        button.append(primaryNode, secondaryNode);
        if (evidence) {
            const badge = document.createElement('span');
            badge.className = `evidence-label ${evidence}`;
            badge.textContent = evidenceLabel(evidence);
            button.appendChild(badge);
        }
        button.addEventListener('click', action);
        item.appendChild(button);
        return item;
    }

    function renderResults() {
        if (!cameraResultsReady()) return;
        resultsList.replaceChildren();
        const zoom = map.getZoom();
        let features;
        if (zoom < DETAIL_ZOOM) {
            features = uniqueRenderedFeatures('camera-clusters', feature =>
                `${feature.geometry.coordinates.join(',')}:${feature.properties.point_count}`);
            features.sort((a, b) => selectedClusterCount(b.properties) - selectedClusterCount(a.properties));
            for (const feature of features.slice(0, MAX_RESULTS)) {
                const count = selectedClusterCount(feature.properties);
                if (!count) continue;
                const coordinates = feature.geometry.coordinates.slice();
                const primary = `${count.toLocaleString()} matching records`;
                const secondary = `Cluster near ${coordinates[1].toFixed(3)}, ${coordinates[0].toFixed(3)}. Activate to zoom in.`;
                resultsList.appendChild(resultButton(primary, secondary, '', () => {
                    pendingResultFocus = true;
                    map.easeTo({center: coordinates, zoom: Math.min(DETAIL_ZOOM, zoom + 3)});
                }, `${primary}. ${secondary}`));
            }
            document.getElementById('results-help').textContent = 'Grouped results are shown at this zoom. Activate a group to zoom in.';
        } else {
            features = uniqueRenderedFeatures('camera-points', feature => feature.properties.netid || feature.id);
            features.sort((a, b) => String(b.properties.lasttime || '').localeCompare(String(a.properties.lasttime || '')));
            for (const feature of features.slice(0, MAX_RESULTS)) {
                const properties = feature.properties;
                resultsList.appendChild(resultButton(
                    properties.netid || 'Unknown BSSID',
                    locationLabel(properties),
                    properties.confidence,
                    () => showDetails(properties),
                    `View details for ${properties.netid || 'camera record'}, ${locationLabel(properties)}, ${evidenceLabel(properties.confidence)}`,
                ));
            }
            document.getElementById('results-help').textContent = features.length > MAX_RESULTS
                ? `Showing the first ${MAX_RESULTS} records. Zoom in for a shorter list.`
                : 'Activate a result to read its details without using the map.';
        }
        const shown = Math.min(features.length, MAX_RESULTS);
        document.getElementById('results-count').textContent = shown.toLocaleString();
        document.getElementById('filter-summary').textContent = zoom < DETAIL_ZOOM
            ? `${shown.toLocaleString()} groups in view`
            : `${shown.toLocaleString()} records in view`;
        resultsLive.textContent = zoom < DETAIL_ZOOM
            ? `${shown.toLocaleString()} grouped results in the current map area. Zoom level ${Math.round(zoom)}.`
            : `${shown.toLocaleString()} camera records in the current map area. Zoom level ${Math.round(zoom)}.`;
        if (pendingResultFocus) {
            pendingResultFocus = false;
            resultsHeading.tabIndex = -1;
            resultsHeading.focus();
        }
    }

    function applyFilters() {
        if (!map.getLayer('camera-clusters') || !map.getLayer('camera-points')) return;
        const count = clusterCountExpression();
        map.setFilter('camera-clusters', ['all', ['has', 'point_count'], ['>', count, 0]]);
        map.setPaintProperty('camera-clusters', 'circle-radius', ['step', count, 11, 10, 15, 100, 21, 1000, 29]);
        map.setFilter('camera-points', detailFilter());
        renderResults();
    }

    map.on('click', 'camera-clusters', event => {
        const feature = event.features && event.features[0];
        if (!feature) return;
        map.easeTo({center: feature.geometry.coordinates, zoom: Math.min(DETAIL_ZOOM, map.getZoom() + 3)});
    });
    map.on('click', 'camera-points', event => {
        const feature = event.features && event.features[0];
        if (!feature) return;
        new maplibregl.Popup({closeButton: true, maxWidth: '340px'})
            .setLngLat(feature.geometry.coordinates)
            .setHTML(detailsHtml(feature.properties))
            .addTo(map);
    });
    for (const layer of ['camera-clusters', 'camera-points']) {
        map.on('mouseenter', layer, () => { map.getCanvas().classList.add('map-pointer-cursor'); });
        map.on('mouseleave', layer, () => { map.getCanvas().classList.remove('map-pointer-cursor'); });
    }
    map.on('idle', () => {
        renderResults();
        if (!firstIdleRecorded) {
            firstIdleRecorded = true;
            performance.mark('flock-map-first-idle');
            const archiveEntries = performance.getEntriesByType('resource')
                .filter(entry => entry.name.includes('flock-cameras'));
            window.__flockFinderMetrics = {
                firstIdleMs: Math.round(performance.now()),
                archiveRequests: archiveEntries.length,
                archiveTransferBytes: archiveEntries.reduce((sum, entry) => sum + (entry.transferSize || 0), 0),
                archiveDecodedBytes: archiveEntries.reduce((sum, entry) => sum + (entry.decodedBodySize || 0), 0),
            };
        }
    });
    map.on('moveend', renderResults);
    map.on('sourcedata', event => {
        if (event.sourceId === 'cameras' && event.isSourceLoaded) renderResults();
    });
    map.on('error', event => {
        if (String(event.error?.message || '').toLowerCase().includes('pmtiles')) {
            setSearchStatus('Map data failed to load. Refresh the page or try again later.', 'error');
        }
    });

    function resetMap() {
        document.getElementById('search-input').value = '';
        setSearchStatus('');
        map.jumpTo({center: DEFAULT_CENTER, zoom: 3.2});
    }

    async function searchBssid(input) {
        const normalized = normalizeBssid(input);
        if (!normalized) {
            setSearchStatus('Enter a valid 12-digit hexadecimal BSSID.', 'error');
            return;
        }
        setSearchStatus('Searching the local BSSID index.', 'loading');
        try {
            const response = await fetch(`${config.bssidIndexPath}/${normalized.slice(0, 2)}.json`);
            if (!response.ok) {
                if (response.status === 404) {
                    setSearchStatus('No record matched this BSSID.', 'empty');
                    return;
                }
                throw new Error(`Index request returned ${response.status}`);
            }
            const shard = await response.json();
            const coordinates = shard[normalized];
            if (!coordinates) {
                setSearchStatus('No record matched this BSSID.', 'empty');
                return;
            }
            setSearchStatus('BSSID found. The map and results now show its area.', 'success');
            pendingResultFocus = true;
            map.jumpTo({center: coordinates, zoom: 16});
        } catch (error) {
            setSearchStatus('The local BSSID index failed to load. Try again.', 'error');
        }
    }

    function geocoderCacheKey(query) {
        return `flock-geocoder:${config.geocoderUrl}:${query.toLowerCase()}`;
    }

    function readGeocoderCache(query) {
        const key = geocoderCacheKey(query);
        if (geocoderMemoryCache.has(key)) return geocoderMemoryCache.get(key);
        try {
            const value = JSON.parse(localStorage.getItem(key));
            if (value && Date.now() - value.savedAt < config.geocoderCacheMs) {
                geocoderMemoryCache.set(key, value.results);
                return value.results;
            }
        } catch (_) {
            return null;
        }
        return null;
    }

    function writeGeocoderCache(query, results) {
        const key = geocoderCacheKey(query);
        geocoderMemoryCache.set(key, results);
        try {
            localStorage.setItem(key, JSON.stringify({savedAt: Date.now(), results}));
        } catch (_) {
            // Memory cache still covers this page session.
        }
    }

    async function searchPlace(query) {
        const cached = readGeocoderCache(query);
        if (cached) {
            useGeocoderResults(cached, true);
            return;
        }
        const delay = Math.max(0, config.geocoderMinIntervalMs - (Date.now() - lastGeocoderStart));
        if (delay) {
            setSearchStatus('Search queued for the one-request-per-second geocoder limit.', 'rate-limit');
            await new Promise(resolve => setTimeout(resolve, delay));
        }
        lastGeocoderStart = Date.now();
        setSearchStatus('Searching the configured place provider.', 'loading');
        try {
            const url = new URL(config.geocoderUrl, document.baseURI);
            url.searchParams.set('format', 'jsonv2');
            url.searchParams.set('q', query);
            url.searchParams.set('limit', '5');
            const response = await fetch(url, {headers: {'Accept': 'application/json'}});
            if (response.status === 429) {
                setSearchStatus('The place provider rate limit was reached. Wait, then search again.', 'rate-limit');
                return;
            }
            if (!response.ok) throw new Error(`Geocoder returned ${response.status}`);
            const results = await response.json();
            writeGeocoderCache(query, results);
            useGeocoderResults(results, false);
        } catch (error) {
            setSearchStatus('The place search failed. Check your connection and try again.', 'error');
        }
    }

    function useGeocoderResults(results, fromCache) {
        if (!Array.isArray(results) || !results.length) {
            setSearchStatus('No place matched this search.', 'empty');
            return;
        }
        const result = results[0];
        const lon = Number(result.lon);
        const lat = Number(result.lat);
        if (!Number.isFinite(lon) || !Number.isFinite(lat)) {
            setSearchStatus('The place provider returned an invalid location.', 'error');
            return;
        }
        setSearchStatus(fromCache ? 'Place found in your browser cache.' : 'Place found. Map results updated.', 'success');
        map.jumpTo({center: [lon, lat], zoom: 13});
    }

    document.getElementById('search-form').addEventListener('submit', event => {
        event.preventDefault();
        const query = document.getElementById('search-input').value.trim();
        if (!query) {
            setSearchStatus('Enter a place or exact BSSID.', 'error');
            return;
        }
        if (isBssidShaped(query)) {
            searchBssid(query);
            return;
        }
        searchPlace(query);
    });
    document.getElementById('locate-button').addEventListener('click', () => {
        setSearchStatus('Requesting your browser location.', 'loading');
        navigator.geolocation.getCurrentPosition(position => {
            map.jumpTo({center: [position.coords.longitude, position.coords.latitude], zoom: 14});
            setSearchStatus('Map centered on your browser location.', 'success');
        }, () => setSearchStatus('Location access failed or was denied.', 'error'), {enableHighAccuracy: false, timeout: 10000});
    });
    document.getElementById('reset-map-button').addEventListener('click', resetMap);
    document.getElementById('filter-reset').addEventListener('click', () => {
        document.querySelectorAll('#tier-filters input[data-confidence]').forEach(input => {
            input.checked = input.dataset.confidence !== 'identified_other';
        });
        document.getElementById('filter-hide-out-of-market').checked = false;
        applyFilters();
    });
    document.querySelectorAll('#tier-filters input').forEach(input => input.addEventListener('change', applyFilters));
    document.getElementById('result-dialog-close').addEventListener('click', () => dialog.close());

    async function firstAvailableJson(paths) {
        for (const path of paths) {
            try {
                const response = await fetch(path);
                if (response.ok) return await response.json();
            } catch (_) {
                // Try the next configured path.
            }
        }
        throw new Error('No JSON source loaded');
    }

    function updateStats() {
        if (!stats) return;
        const rawCounts = stats.by_confidence || stats.confidence_summary?.by_confidence || {};
        const counts = {...rawCounts};
        counts.ssid_correlated = Number(rawCounts.ssid_correlated ?? rawCounts.ssid_confirmed ?? 0);
        counts.oui_high = Number(rawCounts.oui_high ?? 0);
        counts.oui_mfr = Number(rawCounts.oui_mfr ?? 0);
        const correlated = counts.ssid_correlated;
        const ouiOnly = counts.oui_high + counts.oui_mfr;
        const publicTotal = correlated + ouiOnly;
        const suppression = stats.suppression || stats.suppression_stats || stats.suppression_registry || {};
        const excluded = Number(
            suppression.registry_entries
            ?? suppression.excluded_from_release
            ?? suppression.excluded_count
            ?? suppression.total_excluded
            ?? rawCounts.identified_other
            ?? 0
        );
        const outsidePrimaryMarket = Number(
            stats.confidence_summary?.actionable_out_of_market
            ?? stats.confidence_summary?.out_of_market
            ?? stats.out_of_market
            ?? 0
        );
        document.getElementById('total-cameras').textContent = correlated.toLocaleString();
        document.getElementById('total-regions').textContent = Object.keys(stats.cameras_by_region || {}).length.toLocaleString();
        document.getElementById('last-updated').textContent = stats.scan_timestamp
            ? new Date(stats.scan_timestamp).toLocaleDateString()
            : 'Unknown';
        document.querySelectorAll('[data-count]').forEach(element => {
            const key = element.dataset.count;
            const value = counts[key] ?? 0;
            element.textContent = Number(value).toLocaleString();
        });
        const chips = [
            ['tier-ssid', correlated, 'SSID-correlated sightings'],
            ['tier-high', ouiOnly, 'OUI-only candidate sightings'],
            ['tier-total', publicTotal, 'total public candidate sightings'],
            ['tier-other', excluded, 'unrelated-device exclusions'],
            ['tier-market', outsidePrimaryMarket, 'published observations outside the US'],
        ];
        const strip = document.getElementById('tier-strip');
        strip.replaceChildren(...chips.map(([className, value, label]) => {
            const chip = document.createElement('span');
            chip.className = `tier-chip ${className}`;
            const strong = document.createElement('strong');
            strong.textContent = Number(value).toLocaleString();
            chip.append(strong, ` ${label}`);
            return chip;
        }));
        const note = document.getElementById('tier-note');
        note.replaceChildren('OUI-only candidate sightings are suspected matches, not confirmed devices. ');
        const link = document.createElement('a');
        link.href = '#confidence-breakdown';
        link.textContent = 'How the tiers work →';
        note.append(link);
    }

    function populateOuiTable() {
        if (!metadata) return;
        const tbody = document.getElementById('oui-table-body');
        tbody.replaceChildren();
        const counts = stats?.cameras_by_oui || {};
        const evidence = stats?.by_oui_confidence || {};
        for (const [index, entry] of (metadata.ouis || []).entries()) {
            const row = document.createElement('tr');
            const evidenceCounts = evidence[entry.oui] || {};
            const values = [
                index + 1,
                entry.oui,
                entry.tier || 'high',
                entry.vendor || 'Unknown',
                entry.source || 'Unknown',
                Number(counts[entry.oui] || 0).toLocaleString(),
                Number(
                    evidenceCounts.ssid_correlated
                    ?? evidenceCounts.ssid_confirmed
                    ?? 0
                ).toLocaleString(),
                evidence[entry.oui] ? 'Measured' : 'No current records',
            ];
            for (const value of values) {
                const cell = document.createElement('td');
                cell.textContent = value;
                row.appendChild(cell);
            }
            tbody.appendChild(row);
        }
        document.getElementById('total-ouis').textContent = (metadata.ouis || []).length.toLocaleString();
    }

    Promise.allSettled([
        firstAvailableJson(['data/scan_stats.json', '../data/scan_stats.json']).then(value => { stats = value; updateStats(); }),
        firstAvailableJson(['data/flock_ouis.json', '../data/flock_ouis.json']).then(value => { metadata = value; }),
    ]).then(() => {
        populateOuiTable();
        if (!stats) setSearchStatus('Summary statistics failed to load. Map browsing still works.', 'error');
    });

    window.__flockFinder = {map, config, isBssidShaped, normalizeBssid, searchPlace, searchBssid, applyFilters};
}());
