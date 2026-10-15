import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';
import {classifyConfidence} from '../docs/assets/build-map-assets.mjs';

const metadata = JSON.parse(await readFile(new URL('../data/flock_ouis.json', import.meta.url), 'utf8'));
const classifier = metadata.classifier;
const confirmPatterns = classifier.ssid_confirm_patterns.map(pattern => new RegExp(pattern, 'i'));
const denyRules = classifier.ssid_denylist_rules.map(rule => ({
    label: rule.label,
    expression: new RegExp(rule.regex, 'i'),
}));

test('asset builder applies anchored SSID correlation patterns', () => {
    assert.equal(classifyConfidence('Flock', denyRules, confirmPatterns, 'mfr').confidence, 'ssid_correlated');
    assert.equal(classifyConfidence(' FLOCK-7EBB9D ', denyRules, confirmPatterns, 'high').confidence, 'ssid_correlated');
    assert.equal(classifyConfidence('Flock Camera net.', denyRules, confirmPatterns, 'high').confidence, 'ssid_correlated');
    assert.equal(classifyConfidence('Flocking', denyRules, confirmPatterns, 'high').confidence, 'oui_high');
});
