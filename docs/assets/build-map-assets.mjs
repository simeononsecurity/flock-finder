import {createHash} from 'node:crypto';
import {mkdtemp, mkdir, readFile, rm, stat, writeFile} from 'node:fs/promises';
import {spawn} from 'node:child_process';
import {tmpdir} from 'node:os';
import {join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

export function classifyConfidence(ssid, denyRules, confirmPatterns, ouiTier) {
    const denied = denyRules.find(rule => rule.expression.test(ssid));
    if (denied) return {confidence: 'identified_other', blockedReason: denied.label};
    if (confirmPatterns.some(pattern => pattern.test(ssid))) {
        return {confidence: 'ssid_correlated', blockedReason: ''};
    }
    return {confidence: ouiTier === 'mfr' ? 'oui_mfr' : 'oui_high', blockedReason: ''};
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
const [sourceArg = '../../data/flock_cameras.geojson', metadataArg = '../../data/flock_ouis.json', outputArg = '.'] = process.argv.slice(2);
const sourcePath = resolve(sourceArg);
const metadataPath = resolve(metadataArg);
const outputDir = resolve(outputArg);
const tempDir = await mkdtemp(join(tmpdir(), 'flock-map-'));
const classifiedPath = join(tempDir, 'flock-cameras-classified.geojson');

function run(command, args) {
    return new Promise((resolveRun, reject) => {
        const child = spawn(command, args, {
            stdio: 'inherit',
            env: {...process.env, TIPPECANOE_MAX_THREADS: '1'},
        });
        child.on('error', reject);
        child.on('exit', code => code === 0
            ? resolveRun()
            : reject(new Error(`${command} exited with status ${code}`)));
    });
}

try {
    const [collection, metadata] = await Promise.all([
        readFile(sourcePath, 'utf8').then(JSON.parse),
        readFile(metadataPath, 'utf8').then(JSON.parse),
    ]);
    const classifier = metadata.classifier || {};
    const tiers = new Map((metadata.ouis || []).map(entry => [String(entry.oui).toUpperCase(), entry.tier || 'high']));
    const denyRules = (classifier.ssid_denylist_rules || []).map(rule => ({
        label: rule.label,
        expression: new RegExp(rule.regex, 'i'),
    }));
    const confirmPatterns = (classifier.ssid_confirm_patterns || ['^\\s*flock\\s*$', '^\\s*fs\\s+ext\\s+battery']).map(pattern => new RegExp(pattern, 'i'));
    const markets = new Set(classifier.primary_market_countries || ['US']);
    const confidenceKeys = ['ssid_correlated', 'oui_high', 'oui_mfr', 'identified_other'];
    const shards = new Map();

    for (const feature of collection.features || []) {
        const properties = feature.properties || {};
        const ssid = String(properties.ssid || '');
        const classification = classifyConfidence(
            ssid,
            denyRules,
            confirmPatterns,
            tiers.get(String(properties.oui || '').toUpperCase()),
        );
        properties.confidence = classification.confidence;
        properties.blocked_reason = classification.blockedReason;
        properties.out_of_market = !markets.has(String(properties.country || '').toUpperCase());
        properties.record_count = 1;
        for (const key of confidenceKeys) {
            properties[`count_${key}`] = key === classification.confidence ? 1 : 0;
            properties[`count_in_market_${key}`] = key === classification.confidence && !properties.out_of_market ? 1 : 0;
        }

        const netid = String(properties.netid || '').replace(/[^0-9a-f]/gi, '').toUpperCase();
        const coordinates = feature.geometry && feature.geometry.coordinates;
        if (netid.length === 12 && Array.isArray(coordinates)) {
            const prefix = netid.slice(0, 2);
            if (!shards.has(prefix)) shards.set(prefix, {});
            shards.get(prefix)[netid] = [coordinates[0], coordinates[1]];
        }
    }

    await mkdir(join(outputDir, 'bssid'), {recursive: true});
    await Promise.all(Array.from(shards, ([prefix, entries]) =>
        writeFile(join(outputDir, 'bssid', `${prefix}.json`), JSON.stringify(entries))));
    await writeFile(classifiedPath, JSON.stringify(collection));

    console.log(`Preparing ${collection.features.length} records across ${shards.size} BSSID shards.`);
    await run('tippecanoe', ['--version']);

    await run('tippecanoe', [
        '--force',
        '--output', join(outputDir, 'flock-cameras.pmtiles'),
        '--layer', 'cameras',
        '--maximum-zoom', '12',
        '--minimum-zoom', '0',
        '--preserve-input-order',
        '--cluster-distance', '18',
        '--cluster-maxzoom', '11',
        '--drop-rate', '1',
        '--no-feature-limit',
        '--no-tile-size-limit',
        '--accumulate-attribute', 'record_count:sum',
        ...confidenceKeys.flatMap(key => ['--accumulate-attribute', `count_${key}:sum`]),
        ...confidenceKeys.flatMap(key => ['--accumulate-attribute', `count_in_market_${key}:sum`]),
        classifiedPath,
    ]);
    const archivePath = join(outputDir, 'flock-cameras.pmtiles');
    const archiveBytes = (await stat(archivePath)).size;
    const archiveHash = createHash('sha256').update(await readFile(archivePath)).digest('hex');
    console.log(`Built ${archivePath}: ${archiveBytes} bytes, sha256 ${archiveHash}`);
} finally {
    await rm(tempDir, {recursive: true, force: true});
}
}
