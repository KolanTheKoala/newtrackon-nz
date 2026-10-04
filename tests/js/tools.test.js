// node --test tests/js   (after: python tests/js/make_fixtures.py "$NT_FIXTURES")
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {subtle} = require('node:crypto').webcrypto;
const T = require('../../newtrackon/static/js/tools.js');

const DIR = process.env.NT_FIXTURES || '/tmp/nt-fixtures';
const M = JSON.parse(fs.readFileSync(path.join(DIR, 'manifest.json'), 'utf8'));
const load = (n) => new Uint8Array(fs.readFileSync(path.join(DIR, n + '.torrent')));
const dec = (b) => new TextDecoder().decode(b);
const LISTED = {'udp://down.example:6969/announce': 'down', 'udp://good.example:1337/announce': 'up_good',
                'udp://best.example:1/announce': 'up_good'};
const trackersOf = (bytes) => {
    const d = T.bdecode(bytes);
    return (d.map['announce-list'] ? d.map['announce-list'].value : []).map((tier) => tier.map(dec));
};

// ---------- torrent fixer ----------

test('info hash unchanged, v1 and hybrid', async () => {
    for (const name of ['v1', 'hybrid']) {
        const r = T.fixTorrent(load(name), {dropDown: true, add: ['udp://best.example:1/announce'], max: 5}, LISTED);
        assert.ok(!r.error, r.error);
        const after = T.bdecode(r.bytes).map.info.raw;
        const h = await T.infoHashes(after, subtle);
        assert.equal(h.v1, M[name].sha1, name + ' v1 hash');
        assert.equal(h.v2, M[name].sha256, name + ' v2 hash');
    }
});

test('unknown top-level keys pass through byte for byte (piece layers, url-list, comment)', () => {
    for (const name of ['v1', 'hybrid']) {
        const before = T.bdecode(load(name)), after = T.bdecode(T.fixTorrent(load(name), {dropDown: true}, LISTED).bytes);
        for (const k of before.keys) {
            if (k === 'announce' || k === 'announce-list') { continue; }
            assert.deepEqual(after.map[k].raw, before.map[k].raw, name + ': ' + k);
        }
        assert.deepEqual([...after.keys], [...after.keys].sort(), 'keys sorted');
    }
});

test('drops Down trackers and duplicates, keeps unlisted ones by default, adds new ones', () => {
    const r = T.fixTorrent(load('v1'), {dropDown: true, add: ['udp://good.example:1337/announce', 'udp://best.example:1/announce'], max: 5}, LISTED);
    assert.deepEqual(r.removed, ['udp://down.example:6969/announce']);
    assert.deepEqual(r.added, ['udp://best.example:1/announce']);  // good.example was already there
    assert.deepEqual(trackersOf(r.bytes), [['udp://good.example:1337/announce'], ['http://unlisted.example:80/announce'], ['udp://best.example:1/announce']]);
    assert.equal(dec(T.bdecode(r.bytes).map.announce.value), 'udp://good.example:1337/announce');
});

test('only-listed keeps just trackers this site lists', () => {
    const r = T.fixTorrent(load('v1'), {dropDown: true, onlyListed: true}, LISTED);
    assert.deepEqual(trackersOf(r.bytes), [['udp://good.example:1337/announce']]);
    assert.ok(r.removed.includes('http://unlisted.example:80/announce'));
});

test('respects the add limit', () => {
    const many = Array.from({length: 80}, (_, i) => 'udp://t' + i + '.example:1/announce');
    assert.equal(T.fixTorrent(load('bare'), {add: many, max: 10}, {}).added.length, 10);
    assert.equal(T.fixTorrent(load('bare'), {add: many, max: 999}, {}).added.length, T.MAX_ADD);
});

test('torrent with no trackers gets them; nothing to add leaves it trackerless', () => {
    const r = T.fixTorrent(load('bare'), {add: ['udp://best.example:1/announce'], max: 5}, LISTED);
    assert.deepEqual(trackersOf(r.bytes), [['udp://best.example:1/announce']]);
    const none = T.bdecode(T.fixTorrent(load('bare'), {}, LISTED).bytes);
    assert.equal(none.map.announce, undefined);
});

test('private torrents are refused, untouched', () => {
    const r = T.fixTorrent(load('private'), {add: ['udp://best.example:1/announce'], max: 5}, LISTED);
    assert.match(r.error, /private torrent/);
    assert.equal(r.bytes, undefined);
});

test('rejects garbage', () => {
    assert.match(T.fixTorrent(new TextEncoder().encode('hello'), {}, {}).error, /valid \.torrent/);
    assert.match(T.fixTorrent(new TextEncoder().encode('d3:fooi1ee'), {}, {}).error, /no info section/);
    assert.match(T.fixTorrent(load('v1').slice(0, 100), {}, {}).error, /valid \.torrent/);
});

// ---------- magnet booster ----------

const HASH = 'xt=urn:btih:0123456789abcdef0123456789abcdef01234567';

test('magnet: keeps the original text and appends encoded trackers', () => {
    const m = 'magnet:?' + HASH + '&dn=My+File%20(2024)&tr=udp%3A%2F%2Fgood.example%3A1337%2Fannounce';
    const r = T.boostMagnet(m, ['udp://GOOD.example:1337/announce', 'udp://best.example:1/announce', 'https://x.example/announce?a=b&c=d'], 10);
    assert.ok(r.magnet.startsWith(m), 'original kept verbatim');
    assert.equal(r.magnet.slice(m.length), '&tr=' + encodeURIComponent('udp://best.example:1/announce') + '&tr=' + encodeURIComponent('https://x.example/announce?a=b&c=d'));
    assert.deepEqual(r.added, ['udp://best.example:1/announce', 'https://x.example/announce?a=b&c=d']);
});

test('magnet: limit, v2 hashes, and bad input', () => {
    const many = Array.from({length: 80}, (_, i) => 'udp://t' + i + '.example:1/announce');
    assert.equal(T.boostMagnet('magnet:?' + HASH, many, 10).added.length, 10);
    assert.equal(T.boostMagnet('magnet:?' + HASH, many, 999).added.length, T.MAX_ADD);
    assert.ok(!T.boostMagnet('magnet:?xt=urn:btmh:1220abcd', [], 5).error);
    assert.match(T.boostMagnet('https://example.com', [], 5).error, /isn't a magnet/);
    assert.match(T.boostMagnet('magnet:?dn=nohash', [], 5).error, /No BitTorrent info hash/);
});
