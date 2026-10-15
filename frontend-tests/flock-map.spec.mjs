import {expect, test} from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import {readFile} from 'node:fs/promises';

async function openMap(page, config = {}) {
    await page.addInitScript(value => { window.FLOCK_FINDER_CONFIG = value; }, {
        tileArchive: '/frontend-tests/fixtures/assets/flock-cameras.pmtiles',
        bssidIndexPath: '/frontend-tests/fixtures/assets/bssid',
        ...config,
    });
    await page.route('https://tiles.openfreemap.org/styles/dark', route => route.fulfill({
        json: {
            version: 8,
            sources: {},
            layers: [{id: 'test-basemap', type: 'background', paint: {'background-color': '#202028'}}],
        },
    }));
    await page.goto('/docs/index.html');
    await page.waitForFunction(() => window.__flockFinderMetrics?.firstIdleMs > 0);
}

test('loads viewport PMTiles without fetching full GeoJSON', async ({page}) => {
    let legacyGeoJsonRequests = 0;
    let cartoRequests = 0;
    let openFreeMapStyleRequests = 0;
    const archiveRanges = [];
    const workerRequests = [];
    page.on('request', request => {
        if (request.url().includes('flock_cameras.geojson')) legacyGeoJsonRequests += 1;
        if (request.url().includes('cartocdn.com')) cartoRequests += 1;
        if (request.url() === 'https://tiles.openfreemap.org/styles/dark') openFreeMapStyleRequests += 1;
        if (request.url().includes('flock-cameras.pmtiles')) archiveRanges.push(request.headers().range || '');
        if (request.url().includes('maplibre-gl-csp-worker.js')) workerRequests.push(request.url());
    });
    await openMap(page);

    expect(legacyGeoJsonRequests).toBe(0);
    expect(cartoRequests).toBe(0);
    expect(openFreeMapStyleRequests).toBe(1);
    expect(archiveRanges.length).toBeGreaterThan(0);
    expect(archiveRanges.every(value => value.startsWith('bytes='))).toBeTruthy();
    expect(workerRequests).toHaveLength(1);
    const metrics = await page.evaluate(() => window.__flockFinderMetrics);
    console.log(`PERF ${JSON.stringify(metrics)}`);
    expect(metrics.archiveRequests).toBeGreaterThan(0);
    await expect(page.locator('#results-list .result-button').first()).toBeVisible();
    const firstOuiRow = page.locator('#oui-table-body tr').filter({hasText: '70:C9:4E'});
    await expect(firstOuiRow.locator('td').nth(6)).toHaveText('196');
});

test('loads PMTiles from static chunks without range requests', async ({page}) => {
    const archive = await readFile(new URL('./fixtures/assets/flock-cameras.pmtiles', import.meta.url));
    const chunkSize = 1024;
    const requestRanges = [];
    await page.route('**/test-map-chunks/chunk-*.bin', route => {
        requestRanges.push(route.request().headers().range || '');
        const match = route.request().url().match(/chunk-(\d+)\.bin$/);
        const index = Number(match[1]);
        return route.fulfill({
            status: 200,
            contentType: 'application/octet-stream',
            body: archive.subarray(index * chunkSize, (index + 1) * chunkSize),
        });
    });

    await openMap(page, {
        tileChunks: '/test-map-chunks',
        tileChunkSize: chunkSize,
    });

    expect(requestRanges.length).toBeGreaterThan(0);
    expect(requestRanges.every(value => value === '')).toBeTruthy();
    await expect(page.locator('#results-list .result-button').first()).toBeVisible();
});

test('keeps BSSID local and sends place queries only after submit', async ({page}) => {
    const geocoderCalls = [];
    await page.route('**/test-geocoder**', async route => {
        geocoderCalls.push({url: route.request().url(), at: Date.now()});
        await route.fulfill({json: [{lat: '41.8818', lon: '-87.6231', display_name: 'Chicago'}]});
    });
    await openMap(page, {geocoderUrl: 'http://127.0.0.1:4173/test-geocoder', geocoderMinIntervalMs: 1000});

    const input = page.getByLabel('Search by place or exact BSSID');
    await input.fill('Chicago');
    await page.waitForTimeout(150);
    expect(geocoderCalls).toHaveLength(0);

    await input.fill('00:F4:8D:01:B7:DB');
    await page.getByRole('button', {name: 'Search'}).click();
    await expect(page.locator('#search-status')).toContainText('BSSID found');
    expect(geocoderCalls).toHaveLength(0);

    await input.fill('GG:GG:GG:GG:GG:GG');
    await page.getByRole('button', {name: 'Search'}).click();
    await expect(page.locator('#search-status')).toContainText('valid 12-digit hexadecimal BSSID');
    expect(geocoderCalls).toHaveLength(0);

    await input.fill('Chicago');
    await page.getByRole('button', {name: 'Search'}).click();
    await expect(page.locator('#search-status')).toContainText('Place found');
    expect(geocoderCalls).toHaveLength(1);

    await input.fill('Milwaukee');
    await page.getByRole('button', {name: 'Search'}).click();
    await expect(page.locator('#search-status')).toContainText('Place found');
    expect(geocoderCalls).toHaveLength(2);
    expect(geocoderCalls[1].at - geocoderCalls[0].at).toBeGreaterThanOrEqual(950);

    await input.fill('Chicago');
    await page.getByRole('button', {name: 'Search'}).click();
    await expect(page.locator('#search-status')).toContainText('browser cache');
    expect(geocoderCalls).toHaveLength(2);
});

test('renders local camera results while the third-party basemap is stalled', async ({page}) => {
    await page.addInitScript(value => { window.FLOCK_FINDER_CONFIG = value; }, {
        tileArchive: '/frontend-tests/fixtures/assets/flock-cameras.pmtiles',
        bssidIndexPath: '/frontend-tests/fixtures/assets/bssid',
    });
    await page.route('https://tiles.openfreemap.org/styles/dark', () => new Promise(() => {}));
    await page.goto('/docs/index.html', {waitUntil: 'domcontentloaded'});

    const input = page.getByLabel('Search by place or exact BSSID');
    await input.fill('00:F4:8D:01:B7:DB');
    await page.getByRole('button', {name: 'Search'}).click();
    await expect(page.locator('#search-status')).toContainText('BSSID found');
    await expect(page.locator('#results-list .result-button').first()).toBeVisible();
});

test('exposes empty, rate-limit, and failure search states', async ({page}) => {
    await page.route('**/test-geocoder**', async route => {
        const query = new URL(route.request().url()).searchParams.get('q');
        if (query === 'empty') return route.fulfill({json: []});
        if (query === 'limited') return route.fulfill({status: 429, body: 'rate limited'});
        return route.fulfill({status: 500, body: 'failed'});
    });
    await openMap(page, {geocoderUrl: 'http://127.0.0.1:4173/test-geocoder', geocoderMinIntervalMs: 0});
    const input = page.getByLabel('Search by place or exact BSSID');
    for (const [query, expected] of [
        ['empty', 'No place matched'],
        ['limited', 'rate limit was reached'],
        ['failed', 'place search failed'],
    ]) {
        await input.fill(query);
        await page.getByRole('button', {name: 'Search'}).click();
        await expect(page.locator('#search-status')).toContainText(expected);
    }
});

test('supports keyboard result access and has no serious axe findings', async ({page}) => {
    await openMap(page);
    const firstResult = page.locator('#results-list .result-button').first();
    await firstResult.focus();
    await page.keyboard.press('Enter');
    await expect(page.locator('#results-heading')).toBeFocused();

    await page.evaluate(() => window.__flockFinder.map.jumpTo({center: [-93.5604, 42.1026], zoom: 16}));
    await page.waitForFunction(() => document.querySelector('#results-list .evidence-label'));
    await page.locator('#results-list .result-button').first().focus();
    await page.keyboard.press('Enter');
    await expect(page.getByRole('dialog')).toBeVisible();
    await expect(page.getByRole('button', {name: 'Close'})).toBeFocused();
    await page.keyboard.press('Escape');
    await expect(page.getByRole('dialog')).not.toBeVisible();

    const results = await new AxeBuilder({page})
        .exclude('.video-container')
        .disableRules(['color-contrast'])
        .analyze();
    expect(results.violations.filter(item => ['critical', 'serious'].includes(item.impact))).toEqual([]);
});

test('filters map results and stacks the results panel on narrow screens', async ({page}) => {
    await page.setViewportSize({width: 390, height: 844});
    await openMap(page);
    const layout = await page.locator('.map-results-layout').evaluate(element => {
        const map = element.querySelector('#map').getBoundingClientRect();
        const results = element.querySelector('.results-panel').getBoundingClientRect();
        return {mapBottom: map.bottom, resultsTop: results.top, columns: getComputedStyle(element).gridTemplateColumns};
    });
    expect(layout.resultsTop).toBeGreaterThanOrEqual(layout.mapBottom);
    expect(layout.columns.trim().split(/\s+/)).toHaveLength(1);

    const confidenceInputs = page.locator('#tier-filters input[data-confidence]');
    for (let index = 0; index < await confidenceInputs.count(); index += 1) {
        const input = confidenceInputs.nth(index);
        if (await input.isChecked()) await input.uncheck();
    }
    await expect(page.locator('#results-list')).toBeEmpty();
    await page.getByRole('button', {name: 'Reset', exact: true}).click();
    await expect(page.locator('#results-list .result-button').first()).toBeVisible();
});

test('about page has landmarks, skip navigation, and no serious axe findings', async ({page}) => {
    await page.goto('/docs/about.html');
    await expect(page.getByRole('main')).toBeVisible();
    await page.keyboard.press('Tab');
    await expect(page.getByRole('link', {name: 'Skip to main content'})).toBeFocused();
    const results = await new AxeBuilder({page})
        .exclude('.video-container')
        .disableRules(['color-contrast'])
        .analyze();
    expect(results.violations.filter(item => ['critical', 'serious'].includes(item.impact))).toEqual([]);
});
