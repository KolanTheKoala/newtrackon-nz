// Tracker map: Mercator world map, neighbouring countries in different colours (communist states red),
// one small flag per tracker on its country, tooltip on hover, click to open the tracker in the main table.
(function () {
    var COMMUNIST = {'156': 1, '704': 1, '418': 1, '192': 1, '408': 1};   // China, Vietnam, Laos, Cuba, North Korea
    var RED = '#c62828';
    var PALETTE = ['#3f6f9f', '#5a8f5a', '#a0884a', '#7d5f9a', '#4f9a96', '#9a6a4f', '#6b7fb0'];  // no red
    var FLAG_W = 18, FLAG_H = 13.5, GAP = 3;
    var FLAG_URL = 'https://cdnjs.cloudflare.com/ajax/libs/flag-icons/7.5.0/flags/4x3/';
    var STATUS_COLOR = {up_good: '#00e676', up_new: '#00e676', down: '#9e9e9e'};
    var STATUS_TEXT = {up_good: 'Up/Good', up_new: 'Up/New', up_slow: 'Up/Slow', up_unreliable: 'Up/Unreliable',
        up_junk: 'Up/Junk', up_bad: 'Up/Bad', up_broken: 'Up/Broken', down: 'Down'};

    var box = document.getElementById('nt-map');
    var tip = document.getElementById('nt-map-tip');
    var width = box.clientWidth, height = Math.max(420, Math.round(width * 0.62));
    var svg = d3.select(box).append('svg').attr('viewBox', [0, 0, width, height]).attr('width', '100%').attr('height', height);
    var world = svg.append('g'), flagsLayer = svg.append('g');

    function host(u) { try { return new URL(u).hostname; } catch (e) { return u; } }
    function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]; }); }

    // centroid of a country's largest part, so e.g. France isn't pulled towards French Guiana
    function mainCentroid(f) {
        if (f.geometry.type !== 'MultiPolygon') { return d3.geoCentroid(f); }
        var best = null, bestA = -1;
        f.geometry.coordinates.forEach(function (poly) {
            var p = {type: 'Polygon', coordinates: poly}, a = d3.geoArea(p);
            if (a > bestA) { bestA = a; best = p; }
        });
        return d3.geoCentroid(best);
    }

    Promise.all([
        d3.json('/static/data/countries-110m.json'),
        d3.json('/static/data/iso-numeric.json'),
        d3.json('/api/details')
    ]).then(function (r) {
        var topo = r[0], iso = r[1], trackers = r[2];
        var geoms = topo.objects.countries.geometries;
        var countries = topojson.feature(topo, topo.objects.countries).features.filter(function (f) { return f.id !== '010'; });  // no Antarctica
        var projection = d3.geoMercator().fitExtent([[8, 8], [width - 8, height - 8]], {type: 'FeatureCollection', features: countries});
        var path = d3.geoPath(projection);

        // colour: red for communist states; everyone else gets a palette colour different from all neighbours
        var neighbors = topojson.neighbors(geoms), color = {};
        geoms.forEach(function (g, i) { if (COMMUNIST[g.id]) { color[i] = RED; } });
        geoms.map(function (g, i) { return i; })
            .sort(function (a, b) { return neighbors[b].length - neighbors[a].length; })
            .forEach(function (i) {
                if (color[i]) { return; }
                var used = {}; neighbors[i].forEach(function (n) { if (color[n]) { used[color[n]] = 1; } });
                color[i] = PALETTE.filter(function (c) { return !used[c]; })[0] || PALETTE[i % PALETTE.length];
            });
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
            var center = projection(f ? mainCentroid(f) : [0, 0]);
            if (!f || !center) { return; }
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
