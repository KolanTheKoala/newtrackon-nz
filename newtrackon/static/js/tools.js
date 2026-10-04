// Tracker tools: magnet booster and torrent fixer. Everything runs in the browser; only the tracker
// list is fetched from this site. Pure functions below are also exported for the tests (node).
(function (root) {
    'use strict';

    var MAX_ADD = 50;

    // ---------- magnet booster ----------

    // Keep the magnet exactly as given and only append &tr= for trackers it doesn't have yet,
    // so its own encoding (dn, +, spaces) is never touched.
    function boostMagnet(magnet, trackers, max) {
        var m = String(magnet || '').trim();
        if (!/^magnet:\?/i.test(m)) { return {error: 'That isn\'t a magnet link (it should start with magnet:?).'}; }
        var parts = m.slice(8).split('&');
        if (!parts.some(function (p) { return /^xt(\.\d+)?=urn:bt(ih|mh):/i.test(p); })) {
            return {error: 'No BitTorrent info hash in that magnet (xt=urn:btih:… or urn:btmh:…).'};
        }
        var have = {};
        parts.forEach(function (p) {
            var eq = p.indexOf('=');
            if (eq > 0 && /^tr(\.\d+)?$/i.test(p.slice(0, eq))) {
                var v = p.slice(eq + 1);
                try { v = decodeURIComponent(v.replace(/\+/g, ' ')); } catch (e) { /* keep as is */ }
                have[normUrl(v)] = true;
            }
        });
        var n = Math.max(0, Math.min(MAX_ADD, max | 0)), added = [];
        (trackers || []).forEach(function (u) {
            if (added.length < n && !have[normUrl(u)]) { have[normUrl(u)] = true; added.push(u); }
        });
        var out = m + added.map(function (u) { return '&tr=' + encodeURIComponent(u); }).join('');
        return {magnet: out, added: added, already: Object.keys(have).length - added.length};
    }

    function normUrl(u) {
        u = String(u || '').trim();
        var m = /^([a-z][a-z0-9+.-]*:\/\/)([^/?#]*)(.*)$/i.exec(u);
        return m ? m[1].toLowerCase() + m[2].toLowerCase() + m[3] : u;
    }

    // ---------- bencode, on bytes ----------

    function bdecode(buf) {
        var i = 0;
        function num(end) {
            var s = '';
            for (var k = i; k < end; k++) { s += String.fromCharCode(buf[k]); }
            if (!/^-?\d+$/.test(s)) { throw new Error('bad number'); }
            return Number(s);
        }
        function bytes() {
            var colon = buf.indexOf(0x3a, i);
            if (colon < 0) { throw new Error('bad string'); }
            var len = num(colon);
            var start = colon + 1, end = start + len;
            if (len < 0 || end > buf.length) { throw new Error('truncated'); }
            i = end;
            return buf.subarray(start, end);
        }
        function parse() {
            var c = buf[i];
            if (c === 0x69) { i++; var e = buf.indexOf(0x65, i); if (e < 0) { throw new Error('bad int'); } var n = num(e); i = e + 1; return n; }
            if (c === 0x6c) { i++; var l = []; while (buf[i] !== 0x65) { if (i >= buf.length) { throw new Error('truncated'); } l.push(parse()); } i++; return l; }
            if (c === 0x64) {
                i++; var d = {keys: [], map: {}};
                while (buf[i] !== 0x65) {
                    if (i >= buf.length) { throw new Error('truncated'); }
                    var kb = bytes(), ks = latin1(kb), vs = i, v = parse();
                    d.keys.push(ks); d.map[ks] = {key: kb, value: v, raw: buf.subarray(vs, i)};
                }
                i++; return d;
            }
            if (c >= 0x30 && c <= 0x39) { return bytes(); }
            throw new Error('not bencode');
        }
        var v = parse();
        if (i !== buf.length) { throw new Error('trailing data'); }
        return v;
    }

    function latin1(b) { var s = ''; for (var k = 0; k < b.length; k++) { s += String.fromCharCode(b[k]); } return s; }
    function utf8(s) { return new TextEncoder().encode(s); }
    function str(b) { return new TextDecoder('utf-8', {fatal: false}).decode(b); }

    function concat(chunks) {
        var n = 0; chunks.forEach(function (c) { n += c.length; });
        var out = new Uint8Array(n), o = 0;
        chunks.forEach(function (c) { out.set(c, o); o += c.length; });
        return out;
    }
    function encBytes(b) { if (typeof b === 'string') { b = utf8(b); } return concat([utf8(b.length + ':'), b]); }
    function encList(items) { return concat([utf8('l')].concat(items).concat([utf8('e')])); }

    // ---------- torrent fixer ----------

    // opts: {dropDown, onlyListed, add: [urls], max}; listed: {url: status} from /api/details.
    // The info dictionary is copied byte for byte, so the info hash can't change.
    function fixTorrent(bytes, opts, listed) {
        var top;
        try { top = bdecode(bytes); } catch (e) { return {error: 'That isn\'t a valid .torrent file (' + e.message + ').'}; }
        if (!top || !top.map || !top.map.info) { return {error: 'That isn\'t a .torrent file (no info section).'}; }
        var info = top.map.info.value;
        if (info && info.map && info.map['private'] && info.map['private'].value === 1) {
            return {error: 'This is a private torrent: its trackers must not be changed (that would break its tracker\'s rules), so it was left alone.'};
        }
        opts = opts || {}; listed = listed || {};
        var lk = {};
        Object.keys(listed).forEach(function (u) { lk[normUrl(u)] = listed[u]; });

        var tiers = [];
        var al = top.map['announce-list'];
        if (al && Array.isArray(al.value)) {
            al.value.forEach(function (tier) {
                if (Array.isArray(tier)) { tiers.push(tier.filter(function (x) { return x instanceof Uint8Array; }).map(str)); }
            });
        }
        if (!tiers.length && top.map.announce && top.map.announce.value instanceof Uint8Array) { tiers.push([str(top.map.announce.value)]); }

        var seen = {}, kept = [], removed = [];
        tiers = tiers.map(function (tier) {
            return tier.filter(function (u) {
                var k = normUrl(u), st = lk[k];
                if (seen[k]) { return false; }
                seen[k] = true;
                if ((opts.dropDown && st === 'down') || (opts.onlyListed && st === undefined)) { removed.push(u); return false; }
                kept.push(u); return true;
            });
        }).filter(function (t) { return t.length; });

        var added = [], n = Math.max(0, Math.min(MAX_ADD, opts.max | 0));
        (opts.add || []).forEach(function (u) {
            if (added.length < n && !seen[normUrl(u)]) { seen[normUrl(u)] = true; added.push(u); tiers.push([u]); }
        });

        // rebuild the top-level dict: keys in byte order, unchanged values copied raw
        var fresh = {};
        var keys = top.keys.filter(function (k) { return k !== 'announce' && k !== 'announce-list'; });
        if (tiers.length) {
            fresh.announce = encBytes(tiers[0][0]);
            fresh['announce-list'] = encList(tiers.map(function (t) { return encList(t.map(encBytes)); }));
            keys.push('announce', 'announce-list');
        }
        keys.sort(function (a, b) { return a < b ? -1 : a > b ? 1 : 0; });  // latin1 strings: same order as raw bytes
        var chunks = [utf8('d')];
        keys.forEach(function (k) {
            var keyBytes = top.map[k] ? top.map[k].key : utf8(k);
            chunks.push(encBytes(keyBytes), fresh[k] || top.map[k].raw);
        });
        chunks.push(utf8('e'));
        return {bytes: concat(chunks), kept: kept, removed: removed, added: added, info: top.map.info.raw};
    }

    // info hash(es) of a .torrent's info section: v1 (SHA-1) always, v2 (SHA-256) when it's a v2/hybrid torrent
    function infoHashes(infoRaw, subtle) {
        var hex = function (buf) { return Array.prototype.map.call(new Uint8Array(buf), function (x) { return ('0' + x.toString(16)).slice(-2); }).join(''); };
        var v2 = false;
        try { var d = bdecode(infoRaw); v2 = !!(d.map['meta version'] && d.map['meta version'].value === 2); } catch (e) { /* v1 */ }
        return Promise.all([subtle.digest('SHA-1', infoRaw), v2 ? subtle.digest('SHA-256', infoRaw) : null]).then(function (r) {
            return {v1: hex(r[0]), v2: r[1] ? hex(r[1]) : null};
        });
    }

    var api = {boostMagnet: boostMagnet, bdecode: bdecode, fixTorrent: fixTorrent, infoHashes: infoHashes, normUrl: normUrl, MAX_ADD: MAX_ADD};
    if (typeof module !== 'undefined' && module.exports) { module.exports = api; } else { root.NtTools = api; }
})(this);
