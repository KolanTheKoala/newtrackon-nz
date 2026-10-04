// Tracker map: Mercator world map, neighbouring countries in different colours (communist states red),
// one small flag per tracker on its country, tooltip on hover, click to open the tracker in the main table.
(function () {
    // fixed colours: communist states in distinct reds, Russia dark red, Greenland white, Brazil green
    var FIXED = {
        '156': '#e53935',  // China
        '704': '#ff8a80',  // Vietnam
        '418': '#b71c1c',  // Laos
        '408': '#ff5252',  // North Korea
        '192': '#c62828',  // Cuba
        '643': '#7f0000',  // Russia
        '304': '#f2f2f2',  // Greenland
        '010': '#f2f2f2',  // Antarctica
        '076': '#2e9d48',  // Brazil
        '124': '#43a047',  // Canada
        '036': '#b5653a'   // Australia (outback red ochre)
    };
    // countries whose main landmass reaches within EQUATOR_BAND degrees of the equator form a band of greens
    var EQUATOR_BAND = 10;
    var GREENS = ['#2e9d48', '#1b5e20', '#7cb342', '#4caf50', '#9ccc65', '#388e3c', '#00897b'];
    // Sahara and Middle East countries get desert tans and browns (ahead of the green band)
    var DESERT = {'504': 1, '732': 1, '012': 1, '788': 1, '434': 1, '818': 1, '478': 1, '466': 1, '562': 1, '148': 1, '729': 1,
        '682': 1, '887': 1, '512': 1, '784': 1, '634': 1, '414': 1, '368': 1, '400': 1, '760': 1, '376': 1, '275': 1, '422': 1, '364': 1};
    var TANS = ['#d2b48c', '#c19a6b', '#e0c9a6', '#a67b5b', '#deb887', '#b8956a', '#8b6b4a'];
    var PALETTE = ['#3f6f9f', '#5c6bc0', '#7d5f9a', '#4f9a96', '#8e6fb5', '#6b7fb0', '#b07aa1'];  // no reds, greens, tans or white
    // neighbours across a narrow sea, which must not share a colour either
    var SEA_NEIGHBORS = [['036', '554'], ['036', '360'], ['826', '372'], ['826', '250'], ['392', '410'], ['392', '156'],
        ['158', '156'], ['144', '356'], ['450', '508'], ['840', '192'], ['124', '304'], ['352', '304']];
    var FLAG_W = 18, FLAG_H = 13.5, GAP = 3;
    // countries too small for the 110m map: [lon, lat]
    var SMALL = {sg: [103.82, 1.35], hk: [114.17, 22.32], mo: [113.54, 22.19], bh: [50.56, 26.07], mt: [14.38, 35.94],
        mc: [7.42, 43.74], li: [9.55, 47.16], sm: [12.46, 43.94], va: [12.45, 41.9], ad: [1.52, 42.51], mv: [73.22, 3.2],
        gi: [-5.35, 36.14], im: [-4.55, 54.24], je: [-2.13, 49.21], gg: [-2.58, 49.45], mu: [57.55, -20.35], sc: [55.49, -4.68],
        bb: [-59.54, 13.19], ag: [-61.8, 17.06], lc: [-60.98, 13.91], gd: [-61.68, 12.11], kn: [-62.78, 17.3], dm: [-61.37, 15.41],
        aw: [-69.97, 12.52], cw: [-68.99, 12.17], bm: [-64.75, 32.31], ky: [-81.25, 19.31], pf: [-149.41, -17.68], ws: [-172.1, -13.76],
        to: [-175.2, -21.18], ki: [173.0, 1.87], mh: [171.18, 7.13], fm: [158.21, 6.92], pw: [134.58, 7.51], nr: [166.93, -0.52],
        tv: [179.2, -8.52], gu: [144.79, 13.44], as: [-170.7, -14.27], mp: [145.75, 15.18], km: [43.33, -11.65], st: [6.61, 0.19],
        cv: [-23.6, 15.12], ax: [19.94, 60.19], fo: [-6.91, 62.01]};
    var FLAG_URL = 'https://cdnjs.cloudflare.com/ajax/libs/flag-icons/7.5.0/flags/4x3/';
    var STATUS_COLOR = {up_good: '#00e676', up_new: '#00e676', down: '#9e9e9e'};
    var STATUS_TEXT = {up_good: 'Up/Good', up_new: 'Up/New', up_slow: 'Up/Slow', up_unreliable: 'Up/Unreliable',
        up_junk: 'Up/Junk', up_bad: 'Up/Bad', up_broken: 'Up/Broken', down: 'Down'};

    var box = document.getElementById('nt-map');
    var tip = document.getElementById('nt-map-tip');
    var width = box.clientWidth, height = Math.round(width * 0.75);   // set from the projection once the map loads
    var svg = d3.select(box).append('svg').attr('width', '100%');
    var world = svg.append('g'), flagsLayer = svg.append('g');

    function host(u) { try { return new URL(u).hostname; } catch (e) { return u; } }
    function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]; }); }

    // a country's largest part, so e.g. France isn't pulled towards French Guiana
    function mainPolygon(f) {
        var best = null, bestA = -1;
        f.geometry.coordinates.forEach(function (poly) {
            var p = {type: 'Polygon', coordinates: poly}, a = d3.geoArea(p);
            if (a > bestA) { bestA = a; best = p; }
        });
        return best;
    }
    function mainCentroid(f) {
        return d3.geoCentroid(f.geometry.type === 'MultiPolygon' ? mainPolygon(f) : f);
    }

    Promise.all([
        d3.json('/static/data/countries-110m.json'),
        d3.json('/static/data/iso-numeric.json'),
        d3.json('/api/details')
    ]).then(function (r) {
        var topo = r[0], iso = r[1], trackers = r[2];
        var geoms = topo.objects.countries.geometries;
        geoms.forEach(function (g, i) { if (!g.id) { g.id = 'x' + i; } });   // Kosovo, Somaliland, N. Cyprus have no ISO number
        var countries = topojson.feature(topo, topo.objects.countries).features;
        // fit the map to everything but Antarctica, plus 77°S so Antarctica's coast shows as a strip along the bottom
        var fitTo = countries.filter(function (f) { return f.id !== '010'; }).concat([{type: 'Feature', geometry: {type: 'Point', coordinates: [0, -77]}}]);
        var projection = d3.geoMercator().fitWidth(width - 16, {type: 'FeatureCollection', features: fitTo});
        var top = d3.geoPath(projection).bounds({type: 'FeatureCollection', features: fitTo});
        projection.translate([projection.translate()[0] + 8 - top[0][0], projection.translate()[1] + 8 - top[0][1]]);
        height = Math.round(projection([0, -77])[1]);
        projection.clipExtent([[0, 0], [width, height]]);
        svg.attr('viewBox', [0, 0, width, height]).attr('height', height);
        var path = d3.geoPath(projection);

        // colour: fixed ones first; everyone else gets a palette colour different from all neighbours (land and sea)
        var neighbors = topojson.neighbors(geoms), color = {}, index = {};
        geoms.forEach(function (g, i) { index[g.id] = i; if (FIXED[g.id]) { color[i] = FIXED[g.id]; } });
        SEA_NEIGHBORS.forEach(function (p) {
            var a = index[p[0]], b = index[p[1]];
            if (a !== undefined && b !== undefined) { neighbors[a] = neighbors[a].concat([b]); neighbors[b] = neighbors[b].concat([a]); }
        });
        var equatorial = {};
        countries.forEach(function (f) {
            var main = f.geometry.type === 'MultiPolygon' ? mainPolygon(f) : f;
            var b = d3.geoBounds(main);   // [[west, south], [east, north]]
            if (b[0][1] <= EQUATOR_BAND && b[1][1] >= -EQUATOR_BAND) { equatorial[f.id] = 1; }
        });
        function pick(i, choices) {   // first colour none of the neighbours (land or sea) already has
            var used = {}; neighbors[i].forEach(function (n) { if (color[n]) { used[color[n]] = 1; } });
            return choices.filter(function (c) { return !used[c]; })[0];
        }
        var order = geoms.map(function (g, i) { return i; }).sort(function (a, b) { return neighbors[b].length - neighbors[a].length; });
        order.forEach(function (i) { if (!color[i] && DESERT[geoms[i].id]) { color[i] = pick(i, TANS) || pick(i, PALETTE); } });
        order.forEach(function (i) { if (!color[i] && equatorial[geoms[i].id]) { color[i] = pick(i, GREENS) || pick(i, PALETTE); } });
        order.forEach(function (i) { if (!color[i]) { color[i] = pick(i, PALETTE) || pick(i, GREENS) || pick(i, TANS) || PALETTE[i % PALETTE.length]; } });
        var colorById = {};
        geoms.forEach(function (g, i) { colorById[g.id] = color[i]; });

        world.selectAll('path').data(countries).join('path')
            .attr('d', path).attr('fill', function (f) { return colorById[f.id] || PALETTE[0]; })
            .attr('stroke', '#0b1030').attr('stroke-width', 0.5)
            .append('title').text(function (f) { return f.properties.name; });

        // one flag per tracker, gridded around its country's centre
        var byCountry = {}, placed = 0;
        trackers.forEach(function (t) {
            var cc = (t.country_codes || [])[0];
            if (!cc || !iso[cc]) { return; }
            (byCountry[cc] = byCountry[cc] || []).push(t);
        });
        var featById = {};
        countries.forEach(function (f) { featById[f.id] = f; });
        var flags = [];
        Object.keys(byCountry).forEach(function (cc) {
            var f = featById[iso[cc]];
            var ts = byCountry[cc].sort(function (a, b) { return b.score - a.score; });
            var lonlat = f ? mainCentroid(f) : SMALL[cc];
            var center = lonlat && projection(lonlat);
            if (!center) { return; }
            var cols = Math.ceil(Math.sqrt(ts.length)), rows = Math.ceil(ts.length / cols);
            ts.forEach(function (t, k) {
                flags.push({t: t, cc: cc, cx: center[0], cy: center[1],
                    dx: (k % cols - (cols - 1) / 2) * (FLAG_W + GAP), dy: (Math.floor(k / cols) - (rows - 1) / 2) * (FLAG_H + GAP)});
                placed++;
            });
        });
        var flagSel = flagsLayer.selectAll('g').data(flags).join('g').attr('class', 'nt-flag').style('cursor', 'pointer');
        flagSel.append('rect').attr('x', -1.5).attr('y', -1.5).attr('width', FLAG_W + 3).attr('height', FLAG_H + 3).attr('rx', 2)
            .attr('fill', function (d) { return STATUS_COLOR[d.t.status] || '#ff9100'; });
        flagSel.append('image').attr('href', function (d) { return FLAG_URL + d.cc + '.svg'; })
            .attr('width', FLAG_W).attr('height', FLAG_H).attr('preserveAspectRatio', 'none');

        function position(transform) {
            flagSel.attr('transform', function (d) {
                var p = transform.apply([d.cx, d.cy]);
                return 'translate(' + (p[0] + d.dx - FLAG_W / 2) + ',' + (p[1] + d.dy - FLAG_H / 2) + ')';
            });
        }
        position(d3.zoomIdentity);
        svg.call(d3.zoom().scaleExtent([1, 12]).translateExtent([[0, 0], [width, height]]).on('zoom', function (e) {
            world.attr('transform', e.transform);
            world.selectAll('path').attr('stroke-width', 0.5 / e.transform.k);
            position(e.transform);
            hide();
        }));

        // tooltip: stays open while the pointer is on it, so its link can be clicked
        var hideTimer = null;
        function hide() { tip.style.display = 'none'; }
        function later() { clearTimeout(hideTimer); hideTimer = setTimeout(hide, 400); }
        flagSel.on('mouseenter', function (e, d) {
            clearTimeout(hideTimer);
            var t = d.t, h = host(t.url), lat = t.latency_ms != null ? t.latency_ms + ' ms' : '-';
            tip.innerHTML = '<b>' + esc(t.url) + '</b><br>' + esc(STATUS_TEXT[t.status] || t.status) + ' &middot; score ' +
                Math.round(t.score) + ' &middot; ' + esc(lat) + '<br>' + esc((t.countries || [])[0] || '') +
                '<br><a href="/#q=' + encodeURIComponent(h) + '">Show in table &rarr;</a>';
            var r = box.getBoundingClientRect();
            tip.style.left = Math.min(e.clientX - r.left + 14, r.width - 280) + 'px';
            tip.style.top = (e.clientY - r.top + 14) + 'px';
            tip.style.display = 'block';
        }).on('mouseleave', later)
          .on('click', function (e, d) { location.href = '/#q=' + encodeURIComponent(host(d.t.url)); });
        tip.addEventListener('mouseenter', function () { clearTimeout(hideTimer); });
        tip.addEventListener('mouseleave', later);

        document.getElementById('nt-map-count').textContent =
            placed + ' trackers in ' + Object.keys(byCountry).length + ' countries';
    });
})();
