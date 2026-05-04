/* ============================================================
   WAInsight Media Dashboard — UI engine
   ------------------------------------------------------------
   Loaded after manifest + chunk scripts have populated:
     window.__MANIFEST  — case info, facet vocab, schema, hist
     window.META[][]    — array of row arrays (chunked push)
     window.ORPHANS[]   — optional orphan-files chunk
   ------------------------------------------------------------
   No fetch/import/Worker — works under file://.  All data lives
   in JS heap; thumbs are <img src=thumbs/aa/bb/<sha>.jpg>.
   ============================================================ */

(function () {
  'use strict';

  // ===== State ===============================================
  // Hydrated from raw chunks into typed arrays for speed.  Arrays
  // are shared columnar storage; row index in [0, N) = position in
  // the original META.  All bitsets are Uint32Array of length
  // ceil(N/32).
  var S = {
    M: null,            // window.__MANIFEST shorthand
    N: 0,               // total row count
    cols: {},           // cols.<name> -> typed array (or string array)
    bm: {               // bitmaps
      status: [],       // bm.status[code_idx] = Uint32Array
      mime:   [],
      ext:    [],
      conv:   [],
      sender: [],
    },
    selected: {
      status: new Set(),
      mime:   new Set(),
      ext:    new Set(),
      conv:   new Set(),
      sender: new Set(),
    },
    search: '',
    dateRange: null,    // [startDayIdx, endDayIdx]  (inclusive)
    sort: { key: 'ts', dir: 'desc' },
    activeMask: null,   // Uint32Array — current AND result
    visibleIndices: null, // Int32Array — sorted indices to render
    visibleCount: 0,
    rowH: 64,           // px (matches --row-h in CSS)
    cache: { thumbsLoaded: new Map() }, // LRU for img.src eviction
  };

  // ===== Bitset helpers ======================================
  function makeBitset(n) {
    return new Uint32Array((n + 31) >>> 5);
  }
  function setBit(bs, i) { bs[i >>> 5] |= (1 << (i & 31)); }
  function getBit(bs, i) { return (bs[i >>> 5] >>> (i & 31)) & 1; }
  function clearBit(bs, i) { bs[i >>> 5] &= ~(1 << (i & 31)); }
  function popcount32(v) {
    v = v - ((v >>> 1) & 0x55555555);
    v = (v & 0x33333333) + ((v >>> 2) & 0x33333333);
    return (((v + (v >>> 4)) & 0x0f0f0f0f) * 0x01010101) >>> 24;
  }
  function bsCount(bs) {
    var c = 0;
    for (var i = 0; i < bs.length; i++) c += popcount32(bs[i]);
    return c;
  }
  function bsAndIntoNew(a, b) {
    var out = new Uint32Array(a.length);
    for (var i = 0; i < a.length; i++) out[i] = a[i] & b[i];
    return out;
  }
  function bsAndInPlace(target, src) {
    for (var i = 0; i < target.length; i++) target[i] &= src[i];
  }
  function bsOrInPlace(target, src) {
    for (var i = 0; i < target.length; i++) target[i] |= src[i];
  }
  function bsAllOnes(n) {
    var bs = makeBitset(n);
    var full = (n >>> 5);
    for (var i = 0; i < full; i++) bs[i] = 0xFFFFFFFF;
    var rem = n & 31;
    if (rem) bs[full] = (1 << rem) - 1;
    return bs;
  }

  // ===== Hydration ===========================================
  function hydrate() {
    var raw = window.META || [];
    var n = 0;
    for (var i = 0; i < raw.length; i++) n += raw[i].length;
    S.N = n;
    if (n === 0) return;

    // Allocate columnar typed arrays.  Strings stored in arrays.
    S.cols.id          = new Int32Array(n);
    S.cols.msgId       = new Int32Array(n);
    S.cols.convIdx     = new Int32Array(n);
    S.cols.senderIdx   = new Int32Array(n);
    S.cols.ts          = new Float64Array(n);
    S.cols.statusIdx   = new Uint8Array(n);
    S.cols.mimeIdx     = new Int16Array(n);
    S.cols.extIdx      = new Int16Array(n);
    S.cols.size        = new Float64Array(n);  // bytes; can exceed 4GB sum
    S.cols.shareCount  = new Uint16Array(n);
    S.cols.flags       = new Uint8Array(n);
    S.cols.w           = new Int16Array(n);
    S.cols.h           = new Int16Array(n);
    S.cols.dur         = new Int32Array(n);
    S.cols.expiry      = new Float64Array(n);
    S.cols.recoveryTs  = new Float64Array(n);
    S.cols.hdTwinMsgId = new Int32Array(n);

    S.cols.hash      = new Array(n);   // WhatsApp base64 SHA-256
    S.cols.sha256    = new Array(n);   // canonical hex SHA-256 (display)
    S.cols.name      = new Array(n);
    S.cols.caption   = new Array(n);
    S.cols.thumbId   = new Array(n);
    S.cols.path      = new Array(n);
    S.cols.convJid   = new Array(n);
    S.cols.senderJid = new Array(n);
    S.cols.senderLid = new Array(n);
    S.cols.encHash    = new Array(n);  // WhatsApp base64 enc SHA-256
    S.cols.encSha256  = new Array(n);  // canonical hex enc SHA-256
    S.cols.url       = new Array(n);
    S.cols.recovery  = new Array(n);
    S.cols.rawStatus = new Array(n);
    S.cols.assocKind = new Array(n);

    var k = 0;
    for (var ci = 0; ci < raw.length; ci++) {
      var chunk = raw[ci];
      for (var ri = 0; ri < chunk.length; ri++) {
        var r = chunk[ri];
        S.cols.id[k]          = r[0]  | 0;
        S.cols.msgId[k]       = r[1]  | 0;
        S.cols.convIdx[k]     = r[2]  | 0;
        S.cols.senderIdx[k]   = r[3]  | 0;
        S.cols.ts[k]          = +r[4] || 0;
        S.cols.statusIdx[k]   = r[5]  | 0;
        S.cols.mimeIdx[k]     = r[6]  | 0;
        S.cols.extIdx[k]      = r[7]  | 0;
        S.cols.size[k]        = +r[8] || 0;
        S.cols.hash[k]        = r[9]  || '';
        S.cols.name[k]        = r[10] || '';
        S.cols.caption[k]     = r[11] || '';
        S.cols.thumbId[k]     = r[12] || '';
        S.cols.shareCount[k]  = r[13] | 0;
        S.cols.flags[k]       = r[14] | 0;
        S.cols.w[k]           = r[15] | 0;
        S.cols.h[k]           = r[16] | 0;
        S.cols.dur[k]         = r[17] | 0;
        S.cols.path[k]        = r[18] || '';
        S.cols.convJid[k]     = r[19] || '';
        S.cols.senderJid[k]   = r[20] || '';
        S.cols.senderLid[k]   = r[21] || '';
        S.cols.encHash[k]     = r[22] || '';
        S.cols.url[k]         = r[23] || '';
        S.cols.recovery[k]    = r[24] || '';
        S.cols.recoveryTs[k]  = +r[25] || 0;
        S.cols.expiry[k]      = +r[26] || 0;
        S.cols.rawStatus[k]   = r[27] || '';
        S.cols.assocKind[k]   = r[28] || '';
        S.cols.hdTwinMsgId[k] = r[29] | 0;
        S.cols.sha256[k]      = r[30] || '';
        S.cols.encSha256[k]   = r[31] || '';
        k++;
      }
    }
    // Free the raw chunks; the hydrated columns are now the source of truth.
    window.META = null;
  }

  // ===== Bitmap construction =================================
  function buildBitmaps() {
    var n = S.N, M = S.M;
    if (n === 0) return;
    var i;

    // Per-facet bitmaps initialised to all zero (Uint32Array default).
    function alloc(count) {
      var arr = new Array(count);
      for (var j = 0; j < count; j++) arr[j] = makeBitset(n);
      return arr;
    }
    S.bm.status = alloc(M.status.length);
    S.bm.mime   = alloc(M.mime.length);
    S.bm.ext    = alloc(M.ext.length);
    S.bm.conv   = alloc(M.conv.length);
    S.bm.sender = alloc(M.sender.length);

    var st = S.cols.statusIdx;
    var mi = S.cols.mimeIdx;
    var ex = S.cols.extIdx;
    var co = S.cols.convIdx;
    var se = S.cols.senderIdx;

    for (i = 0; i < n; i++) {
      setBit(S.bm.status[st[i]], i);
      var mIdx = mi[i]; if (mIdx >= 0) setBit(S.bm.mime[mIdx], i);
      var eIdx = ex[i]; if (eIdx >= 0) setBit(S.bm.ext[eIdx], i);
      var cIdx = co[i]; if (cIdx >= 0) setBit(S.bm.conv[cIdx], i);
      var sIdx = se[i]; if (sIdx >= 0) setBit(S.bm.sender[sIdx], i);
    }
  }

  // ===== Cascading filter calculation ========================
  function unionSelected(facet) {
    // OR of bitmaps for selected values.  Empty selection ⇒ null,
    // meaning "no constraint from this facet" (treated as all-ones).
    var sel = S.selected[facet];
    if (sel.size === 0) return null;
    var bms = S.bm[facet];
    var out = makeBitset(S.N);
    sel.forEach(function (vIdx) { bsOrInPlace(out, bms[vIdx]); });
    return out;
  }

  function dateMask() {
    if (!S.dateRange || !S.M.hist || !S.M.hist.bins.length) return null;
    var bins = S.M.hist.bins;
    var startMs = bins[S.dateRange[0]];
    var endMs = bins[S.dateRange[1]] + S.M.hist.dayMs;
    var ts = S.cols.ts;
    var bs = makeBitset(S.N);
    for (var i = 0; i < S.N; i++) {
      var t = ts[i];
      if (t >= startMs && t < endMs) setBit(bs, i);
    }
    return bs;
  }

  function searchMask() {
    var q = (S.search || '').trim().toLowerCase();
    if (!q) return null;
    var bs = makeBitset(S.N);
    var name = S.cols.name, cap = S.cols.caption;
    var hash = S.cols.hash, sha = S.cols.sha256;
    var encH = S.cols.encHash, encSha = S.cols.encSha256;
    var sjid = S.cols.senderJid, slid = S.cols.senderLid, cjid = S.cols.convJid;
    var path = S.cols.path;
    var mimeIdx = S.cols.mimeIdx, extIdx = S.cols.extIdx;
    var mimeVocab = S.M.mime, extVocab = S.M.ext;
    var convIdx = S.cols.convIdx, senderIdx = S.cols.senderIdx;
    var convVocab = S.M.conv, senderVocab = S.M.sender;
    for (var i = 0; i < S.N; i++) {
      // Cheap denormalised text fields first
      if (
        (name[i] && name[i].toLowerCase().indexOf(q) !== -1) ||
        (cap[i]  && cap[i].toLowerCase().indexOf(q)  !== -1) ||
        (hash[i] && hash[i].toLowerCase().indexOf(q) !== -1) ||
        (sha[i]  && sha[i].indexOf(q) !== -1) ||
        (encH[i] && encH[i].toLowerCase().indexOf(q) !== -1) ||
        (encSha[i] && encSha[i].indexOf(q) !== -1) ||
        (sjid[i] && sjid[i].toLowerCase().indexOf(q) !== -1) ||
        (slid[i] && slid[i].toLowerCase().indexOf(q) !== -1) ||
        (cjid[i] && cjid[i].toLowerCase().indexOf(q) !== -1) ||
        (path[i] && path[i].toLowerCase().indexOf(q) !== -1)
      ) { setBit(bs, i); continue; }
      // Vocabulary lookups — match MIME, extension, conv name, sender name
      var mi = mimeIdx[i];
      if (mi >= 0 && mimeVocab[mi] && mimeVocab[mi].indexOf(q) !== -1) {
        setBit(bs, i); continue;
      }
      var ei = extIdx[i];
      if (ei >= 0 && extVocab[ei] && extVocab[ei].indexOf(q) !== -1) {
        setBit(bs, i); continue;
      }
      var ci = convIdx[i];
      if (ci >= 0 && convVocab[ci]) {
        var cn = convVocab[ci].name;
        if (cn && cn.toLowerCase().indexOf(q) !== -1) {
          setBit(bs, i); continue;
        }
      }
      var si = senderIdx[i];
      if (si >= 0 && senderVocab[si]) {
        var sn = senderVocab[si].name;
        if (sn && sn.toLowerCase().indexOf(q) !== -1) {
          setBit(bs, i); continue;
        }
      }
    }
    return bs;
  }

  function sizeMask() {
    if (!S.sizeRange) return null;
    var lo = S.sizeRange[0], hi = S.sizeRange[1];
    var sz = S.cols.size;
    var bs = makeBitset(S.N);
    for (var i = 0; i < S.N; i++) {
      var v = sz[i];
      if (lo != null && v < lo) continue;
      if (hi != null && v > hi) continue;
      setBit(bs, i);
    }
    return bs;
  }

  function recompute() {
    var n = S.N;
    var mask = bsAllOnes(n);
    var FACETS = ['status','mime','ext','conv','sender'];
    var perFacet = {};
    for (var fi = 0; fi < FACETS.length; fi++) {
      var f = FACETS[fi];
      perFacet[f] = unionSelected(f);
      if (perFacet[f]) bsAndInPlace(mask, perFacet[f]);
    }
    var dm = dateMask();
    if (dm) bsAndInPlace(mask, dm);
    var sm = searchMask();
    if (sm) bsAndInPlace(mask, sm);
    var zm = sizeMask();
    if (zm) bsAndInPlace(mask, zm);
    S.activeMask = mask;
    S.visibleCount = bsCount(mask);

    // For each facet, compute counts of values under "all OTHER filters
    // applied" — flight-fare style.
    var counts = {};
    for (var f2i = 0; f2i < FACETS.length; f2i++) {
      var f2 = FACETS[f2i];
      // Mask without this facet's selection
      var m2 = bsAllOnes(n);
      for (var f3i = 0; f3i < FACETS.length; f3i++) {
        var f3 = FACETS[f3i];
        if (f3 === f2) continue;
        if (perFacet[f3]) bsAndInPlace(m2, perFacet[f3]);
      }
      if (dm) bsAndInPlace(m2, dm);
      if (sm) bsAndInPlace(m2, sm);
      if (zm) bsAndInPlace(m2, zm);
      // Now count each value's intersection with m2
      var arr = S.bm[f2];
      var out = new Int32Array(arr.length);
      for (var vi = 0; vi < arr.length; vi++) {
        var inter = bsAndIntoNew(arr[vi], m2);
        out[vi] = bsCount(inter);
      }
      counts[f2] = out;
    }
    S.cascadingCounts = counts;

    // Build sorted visibleIndices
    rebuildVisibleIndices();
  }

  function rebuildVisibleIndices() {
    var n = S.N;
    var mask = S.activeMask;
    var idx = new Int32Array(S.visibleCount);
    var p = 0;
    for (var i = 0; i < n; i++) {
      if (getBit(mask, i)) idx[p++] = i;
    }
    var key = S.sort.key, dir = S.sort.dir === 'desc' ? -1 : 1;
    var cmp;
    if (key === 'ts')        cmp = numericCmp(S.cols.ts,         dir);
    else if (key === 'size') cmp = numericCmp(S.cols.size,       dir);
    else if (key === 'share')cmp = numericCmp(S.cols.shareCount, dir);
    else if (key === 'name') cmp = stringCmp(S.cols.name,        dir);
    else if (key === 'hash') cmp = stringCmp(S.cols.sha256,      dir);
    else if (key === 'sender') cmp = mappedStringCmp(S.cols.senderIdx, S.M.sender, 'name', dir);
    else if (key === 'conv') cmp = mappedStringCmp(S.cols.convIdx, S.M.conv, 'name', dir);
    else if (key === 'status') cmp = numericCmp(S.cols.statusIdx, dir);
    if (cmp) Array.prototype.sort.call(idx, cmp);
    S.visibleIndices = idx;
  }

  function updateSortHeaderUi() {
    var headers = document.querySelectorAll('#listHeader .sortable');
    for (var i = 0; i < headers.length; i++) {
      var h = headers[i];
      var active = h.dataset.sort === S.sort.key;
      h.classList.toggle('sort-active', active);
      h.classList.toggle('sort-asc',  active && S.sort.dir === 'asc');
      h.classList.toggle('sort-desc', active && S.sort.dir === 'desc');
    }
  }

  function numericCmp(arr, dir) {
    return function (a, b) {
      var av = arr[a], bv = arr[b];
      return av < bv ? -dir : (av > bv ? dir : 0);
    };
  }
  function stringCmp(arr, dir) {
    return function (a, b) {
      var av = arr[a] || '', bv = arr[b] || '';
      av = av.toLowerCase(); bv = bv.toLowerCase();
      return av < bv ? -dir : (av > bv ? dir : 0);
    };
  }
  function mappedStringCmp(idxArr, vocab, field, dir) {
    return function (a, b) {
      var ai = idxArr[a], bi = idxArr[b];
      var av = (ai >= 0 && vocab[ai]) ? (vocab[ai][field] || '') : '';
      var bv = (bi >= 0 && vocab[bi]) ? (vocab[bi][field] || '') : '';
      av = av.toLowerCase(); bv = bv.toLowerCase();
      return av < bv ? -dir : (av > bv ? dir : 0);
    };
  }

  // ===== Sidebar facet rendering =============================
  function renderFacets() {
    var M = S.M, counts = S.cascadingCounts || {};
    renderFacetList('status', counts.status || [],
      function (item, i) {
        var s = M.status[i];
        return { value: i,
                 label: s.label,
                 sub: '',
                 swatchClass: 'swatch-' + s.cls.replace(/^ms-/, '') };
      });
    renderFacetList('conv', counts.conv || [],
      function (item, i) {
        var c = M.conv[i];
        var subBits = [];
        if (c.type && c.type !== 'personal') subBits.push(c.type);
        if (c.jid) subBits.push(c.jid);
        return { value: i, label: c.name, sub: subBits.join(' · '),
                 avatar: c.avatar || '',
                 initial: avatarInitial(c.name) };
      });
    renderFacetList('sender', counts.sender || [],
      function (item, i) {
        var s = M.sender[i];
        var subBits = [];
        if (s.jid) subBits.push(s.jid);
        if (s.lid && s.lid !== s.jid) subBits.push('lid:' + s.lid);
        return { value: i, label: s.name, sub: subBits.join(' · '),
                 avatar: s.avatar || '',
                 initial: avatarInitial(s.name) };
      });
    renderFacetList('mime', counts.mime || [],
      function (item, i) { return { value: i, label: M.mime[i] || '?', sub: '' }; });
    renderFacetList('ext', counts.ext || [],
      function (item, i) {
        var e = M.ext[i] || '?';
        return { value: i, label: '.' + e, sub: '' };
      });
    // Update count headers
    document.getElementById('status-cnt').textContent =
      formatFacetCounter('status');
    document.getElementById('conv-cnt').textContent =
      formatFacetCounter('conv');
    document.getElementById('sender-cnt').textContent =
      formatFacetCounter('sender');
    document.getElementById('mime-cnt').textContent =
      formatFacetCounter('mime');
    document.getElementById('ext-cnt').textContent =
      formatFacetCounter('ext');
    // has-active style
    ['status','conv','sender','mime','ext'].forEach(function (f) {
      var sec = document.getElementById('facet-' + f);
      if (sec) sec.classList.toggle('has-active', S.selected[f].size > 0);
    });
  }
  function formatFacetCounter(f) {
    var sel = S.selected[f].size, total = S.M[f].length;
    if (sel) return sel + ' / ' + total;
    return total + '';
  }
  function shortJid(jid) {
    // Retained only for places that genuinely need a one-liner; the
    // sidebar facets and list rows now show the FULL JID.
    if (!jid) return '';
    if (jid.length > 32) return jid.slice(0, 14) + '…' + jid.slice(-12);
    return jid;
  }
  function avatarInitial(name) {
    if (!name) return '?';
    var s = name.replace(/^[\W_]+/, '');
    return (s.charAt(0) || '?').toUpperCase();
  }

  function renderFacetList(facet, counts, mapItem) {
    var ul = document.getElementById(facet + '-list');
    if (!ul) return;
    var search = (S._facetSearch && S._facetSearch[facet] || '').toLowerCase();
    var pairs = [];
    for (var i = 0; i < S.M[facet].length; i++) {
      var info = mapItem(null, i);
      if (search && (info.label + ' ' + info.sub).toLowerCase().indexOf(search) === -1)
        continue;
      pairs.push({ idx: i, info: info, n: counts[i] || 0 });
    }
    pairs.sort(function (a, b) {
      // Selected first, then by count desc
      var as = S.selected[facet].has(a.idx) ? 1 : 0;
      var bs = S.selected[facet].has(b.idx) ? 1 : 0;
      if (as !== bs) return bs - as;
      return b.n - a.n;
    });
    // Cap rendered facet items for very long lists
    var cap = 400;
    var capped = pairs.length > cap;
    if (capped) pairs = pairs.slice(0, cap);

    var html = '';
    for (var p = 0; p < pairs.length; p++) {
      var pp = pairs[p];
      var isActive = S.selected[facet].has(pp.idx);
      var zero = (pp.n === 0 && !isActive) ? ' zero' : '';
      var swatch = pp.info.swatchClass
        ? '<span class="swatch ' + pp.info.swatchClass + '"></span>' : '';
      var avatar = pp.info.avatar
        ? '<img class="facet-av" src="' + escapeAttr(pp.info.avatar) + '" alt="">' :
          (pp.info.initial
            ? '<span class="facet-av initial">' + escapeHtml(pp.info.initial) + '</span>'
            : '');
      var labelHtml = '<span class="label">' +
        '<span>' + escapeHtml(pp.info.label) + '</span>' +
        (pp.info.sub ? '<span class="sub">' + escapeHtml(pp.info.sub) + '</span>' : '') +
        '</span>';
      // Full-text tooltip so the analyst can read truncated names + JIDs
      // without having to click the row to open the detail flyout.
      var fullTip = pp.info.label +
                    (pp.info.sub ? '\n' + pp.info.sub : '') +
                    '\n\n' + formatNum(pp.n) + ' file' + (pp.n === 1 ? '' : 's');
      html += '<div class="facet-item' + (isActive ? ' active' : '') + zero +
              '" data-facet="' + facet + '" data-idx="' + pp.idx +
              '" title="' + escapeAttr(fullTip) + '">' +
              '<input type="checkbox"' + (isActive ? ' checked' : '') + '>' +
              swatch + avatar +
              labelHtml +
              '<span class="count">' + formatNum(pp.n) + '</span>' +
              '</div>';
    }
    if (capped) {
      html += '<div class="facet-item zero" style="cursor:default">' +
              '<span class="label">…' + (S.M[facet].length - cap) +
              ' more (refine search)</span></div>';
    }
    ul.innerHTML = html;
  }

  function bindFacetClicks() {
    // Single delegated handler — the facet items rebuild on every
    // recompute().
    document.getElementById('sidebar').addEventListener('click', function (e) {
      var item = e.target.closest('.facet-item[data-facet]');
      if (item) {
        var facet = item.dataset.facet;
        var idx = +item.dataset.idx;
        if (S.selected[facet].has(idx)) S.selected[facet].delete(idx);
        else S.selected[facet].add(idx);
        applyAndRender();
        return;
      }
      var clr = e.target.closest('.facet-clear[data-facet]');
      if (clr) {
        e.stopPropagation();
        var f = clr.dataset.facet;
        S.selected[f].clear();
        applyAndRender();
      }
    });

    // Per-facet text search inputs
    var inputs = document.querySelectorAll('.facet-search[data-facet]');
    S._facetSearch = {};
    for (var i = 0; i < inputs.length; i++) {
      (function (inp) {
        inp.addEventListener('input', function () {
          S._facetSearch[inp.dataset.facet] = inp.value;
          renderFacets();
        });
      })(inputs[i]);
    }
  }

  // ===== Topbar / search / sort =============================
  function bindTopbar() {
    var sb = document.getElementById('searchBox');
    var deb = debounce(function () {
      S.search = sb.value;
      applyAndRender();
    }, 180);
    sb.addEventListener('input', deb);

    document.getElementById('resetBtn').addEventListener('click', function () {
      S.selected.status.clear();
      S.selected.mime.clear();
      S.selected.ext.clear();
      S.selected.conv.clear();
      S.selected.sender.clear();
      S.dateRange = null;
      S.search = '';
      sb.value = '';
      var inputs = document.querySelectorAll('.facet-search');
      for (var i = 0; i < inputs.length; i++) inputs[i].value = '';
      S._facetSearch = {};
      applyAndRender();
    });

    document.getElementById('sortSelect').addEventListener('change', function (e) {
      var v = e.target.value.split(':');
      S.sort.key = v[0]; S.sort.dir = v[1];
      rebuildVisibleIndices();
      renderList();
      updateSortHeaderUi();
    });

    // Click any column header to sort by it; click again to flip direction
    document.getElementById('listHeader').addEventListener('click', function (e) {
      var t = e.target.closest('.sortable[data-sort]');
      if (!t) return;
      var key = t.dataset.sort;
      if (S.sort.key === key) {
        S.sort.dir = (S.sort.dir === 'asc') ? 'desc' : 'asc';
      } else {
        S.sort.key = key;
        // Sensible default direction per column
        S.sort.dir = (key === 'ts' || key === 'size' || key === 'share')
          ? 'desc' : 'asc';
      }
      // Sync the dropdown if there's a matching option
      var sel = document.getElementById('sortSelect');
      var want = key + ':' + S.sort.dir;
      var matched = false;
      for (var i = 0; i < sel.options.length; i++) {
        if (sel.options[i].value === want) {
          sel.selectedIndex = i; matched = true; break;
        }
      }
      if (!matched) sel.selectedIndex = -1;
      rebuildVisibleIndices();
      renderList();
      updateSortHeaderUi();
    });

    // Sticker toggle.
    //   • If the dashboard was BUILT with hide_stickers=True, the data
    //     has no sticker rows at all, so the toggle is informational
    //     only — disable the button + tooltip-explain.
    //   • Otherwise the toggle flips an in-memory bitset that hides
    //     rows with the FLAG_IS_STICKER bit set.
    var hideStickersBtn = document.getElementById('hideStickersBtn');
    var lbl = document.getElementById('stickerBtnLabel');
    var builtExcluded = !!S.M.hideStickers;
    if (builtExcluded) {
      hideStickersBtn.disabled = true;
      hideStickersBtn.classList.add('active');
      hideStickersBtn.title = 'Stickers were excluded when this dashboard was built — rebuild without that option to see them.';
      lbl.textContent = 'Stickers excluded';
      S._stickersHidden = false;   // nothing to hide; data is already clean
    } else {
      var stickersHidden = false;
      function setStickerUi() {
        hideStickersBtn.classList.toggle('active', stickersHidden);
        lbl.textContent = stickersHidden ? 'Show stickers' : 'Hide stickers';
        hideStickersBtn.title = stickersHidden
          ? 'Stickers are currently hidden — click to show them again'
          : 'Click to hide sticker rows from the table';
      }
      setStickerUi();
      hideStickersBtn.addEventListener('click', function () {
        stickersHidden = !stickersHidden;
        setStickerUi();
        S._stickersHidden = stickersHidden;
        applyAndRender();
      });
      S._stickersHidden = false;
    }

    // Timezone selector — re-render every visible timestamp on change.
    S.tz = (window.localStorage && localStorage.getItem('wainsight.tz')) || 'local';
    var tzSel = document.getElementById('tzSelect');
    tzSel.value = S.tz;
    tzSel.addEventListener('change', function () {
      S.tz = tzSel.value;
      try { localStorage.setItem('wainsight.tz', S.tz); } catch (_) {}
      renderTopbar(); renderList(); renderHistogram();
      // If detail flyout is open, re-render it too
      if (currentDetailIdx >= 0 &&
          document.getElementById('detail').style.display !== 'none') {
        openDetail(currentDetailIdx);
      }
    });

    // Size range filter
    function _parseSize(s) {
      if (!s) return null;
      s = ('' + s).trim().toUpperCase().replace(/\s+/g, '');
      if (!s) return null;
      var m = /^(-?\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB|K|M|G|T)?$/i.exec(s);
      if (!m) return null;
      var n = parseFloat(m[1]);
      var u = (m[2] || 'B').toUpperCase();
      var mul = ({ B: 1, K: 1024, KB: 1024, M: 1048576, MB: 1048576,
                   G: 1073741824, GB: 1073741824,
                   T: 1099511627776, TB: 1099511627776 })[u] || 1;
      return Math.round(n * mul);
    }
    function _applySizeFromInputs() {
      var lo = _parseSize(document.getElementById('sizeMin').value);
      var hi = _parseSize(document.getElementById('sizeMax').value);
      S.sizeRange = (lo == null && hi == null) ? null : [lo, hi];
      // Visual: highlight the matching preset chip if exact
      var presets = document.querySelectorAll('.size-preset');
      for (var i = 0; i < presets.length; i++) {
        var pmin = +(presets[i].dataset.min || 0);
        var pmax = presets[i].dataset.max ? +presets[i].dataset.max : null;
        var match = (S.sizeRange && S.sizeRange[0] === pmin
                                  && (pmax == null
                                      ? S.sizeRange[1] == null
                                      : S.sizeRange[1] === pmax));
        presets[i].classList.toggle('active', !!match);
      }
      var sec = document.getElementById('facet-size');
      if (sec) sec.classList.toggle('has-active', !!S.sizeRange);
      applyAndRender();
    }
    var debSize = debounce(_applySizeFromInputs, 200);
    document.getElementById('sizeMin').addEventListener('input', debSize);
    document.getElementById('sizeMax').addEventListener('input', debSize);
    document.querySelectorAll('.size-preset').forEach(function (b) {
      b.addEventListener('click', function () {
        var pmin = +(b.dataset.min || 0);
        var pmax = b.dataset.max ? +b.dataset.max : null;
        document.getElementById('sizeMin').value =
          pmin === 0 ? '' : formatBytes(pmin);
        document.getElementById('sizeMax').value =
          pmax == null ? '' : formatBytes(pmax);
        _applySizeFromInputs();
      });
    });
    var sizeClr = document.querySelector('.facet-clear[data-facet="size"]');
    if (sizeClr) sizeClr.addEventListener('click', function (e) {
      e.stopPropagation();
      document.getElementById('sizeMin').value = '';
      document.getElementById('sizeMax').value = '';
      _applySizeFromInputs();
    });

    document.getElementById('exportCsvBtn').addEventListener('click', exportCsv);
    document.getElementById('exportXlsxBtn').addEventListener('click', exportXlsx);
    document.getElementById('exportHtmlBtn').addEventListener('click', exportHtml);
    document.getElementById('copyHashesBtn').addEventListener('click', copyHashes);

    document.getElementById('histClear').addEventListener('click', function () {
      S.dateRange = null;
      applyAndRender();
    });

    // Tab switcher
    var tabs = document.querySelectorAll('#sidebar .tab');
    for (var i = 0; i < tabs.length; i++) {
      (function (t) {
        t.addEventListener('click', function () {
          var name = t.dataset.tab;
          for (var j = 0; j < tabs.length; j++) tabs[j].classList.remove('active');
          t.classList.add('active');
          var panes = document.querySelectorAll('.tab-pane');
          for (var k = 0; k < panes.length; k++) {
            panes[k].classList.toggle('active', panes[k].dataset.pane === name);
          }
          if (name === 'sharing') renderSharingTab();
          if (name === 'orphans') renderOrphansTab();
        });
      })(tabs[i]);
    }
  }

  // ===== Apply + render ======================================
  function applyAndRender() {
    // Sticker exclusion is encoded as: if S._stickersHidden, drop rows
    // with FLAG_IS_STICKER set.  Implement by intersecting activeMask
    // with a sticker-exclusion bitset stored once on first toggle.
    var t0 = performance.now();
    recompute();
    if (S._stickersHidden) {
      if (!S._stickerExcludeMask) {
        var bs = makeBitset(S.N);
        var fl = S.cols.flags;
        for (var i = 0; i < S.N; i++) {
          if (!(fl[i] & 8)) setBit(bs, i);  // 8 = FLAG_IS_STICKER
        }
        S._stickerExcludeMask = bs;
      }
      bsAndInPlace(S.activeMask, S._stickerExcludeMask);
      S.visibleCount = bsCount(S.activeMask);
      rebuildVisibleIndices();
    }
    renderFacets();
    renderTopbar();
    renderList();
    renderHistogram();
    updateSortHeaderUi();
    var t1 = performance.now();
    if (window.console && console.debug) {
      console.debug('[WAInsight] recompute+render', (t1 - t0).toFixed(1), 'ms');
    }
  }

  function renderTopbar() {
    var rc = document.getElementById('resultCount');
    var fs = document.getElementById('filterSummary');
    var ts = document.getElementById('topbarStats');
    rc.textContent = formatNum(S.visibleCount) + ' file' +
      (S.visibleCount === 1 ? '' : 's');

    // Active filter summary
    var bits = [];
    var fmap = {
      status: 'status', conv: 'conversation', sender: 'sender',
      mime: 'MIME', ext: 'extension'
    };
    var keys = Object.keys(fmap);
    for (var i = 0; i < keys.length; i++) {
      var sz = S.selected[keys[i]].size;
      if (sz) bits.push(fmap[keys[i]] + '×' + sz);
    }
    if (S.dateRange) bits.push('date range');
    if (S.search) bits.push('search "' + S.search + '"');
    fs.textContent = bits.length ? '· ' + bits.join(' · ') : (S.visibleCount === S.N ? '(no filters)' : '');

    // Quick stats — on disk / missing / size in current selection
    var onDisk = 0, missing = 0, totBytes = 0;
    var idx = S.visibleIndices, fl = S.cols.flags, sz2 = S.cols.size;
    for (var j = 0; j < idx.length; j++) {
      var k = idx[j];
      if (fl[k] & 16) onDisk++; else missing++;
      totBytes += sz2[k];
    }
    ts.innerHTML =
      '<span>on disk <b>' + formatNum(onDisk) + '</b></span>' +
      '<span>missing <b>' + formatNum(missing) + '</b></span>' +
      '<span>size <b>' + formatBytes(totBytes) + '</b></span>';

    // Empty state
    document.getElementById('emptyState').style.display =
      (S.visibleCount === 0) ? '' : 'none';
    document.getElementById('listWrap').style.display =
      (S.visibleCount === 0) ? 'none' : '';
  }

  // ===== Virtual list =======================================
  var listScroll, listSpacer, listWindow, io;
  function setupVirtualList() {
    listScroll = document.getElementById('listScroll');
    listSpacer = document.getElementById('listSpacer');
    listWindow = document.getElementById('listWindow');
    listScroll.addEventListener('scroll', function () {
      // RAF for smoothness on Windows / high-refresh
      if (S._scrollRaf) return;
      S._scrollRaf = requestAnimationFrame(function () {
        S._scrollRaf = null; renderListWindow();
      });
    });
    // IntersectionObserver to lazy-load thumbnails as rows enter viewport
    io = new IntersectionObserver(function (entries) {
      for (var i = 0; i < entries.length; i++) {
        var img = entries[i].target;
        if (entries[i].isIntersecting) {
          if (img.dataset.src && !img.src) img.src = img.dataset.src;
        }
        // No eviction — kept rows are limited by virtual list pool size
      }
    }, { root: listScroll, rootMargin: '200px 0px' });
  }

  function renderList() {
    if (!listSpacer) setupVirtualList();
    listSpacer.style.height = (S.visibleCount * S.rowH) + 'px';
    listScroll.scrollTop = 0;
    renderListWindow();
  }

  function renderListWindow() {
    if (S.visibleCount === 0) { listWindow.innerHTML = ''; return; }
    var top = listScroll.scrollTop;
    var h = listScroll.clientHeight;
    var first = Math.max(0, Math.floor(top / S.rowH) - 8);
    var last = Math.min(S.visibleCount - 1,
                        Math.ceil((top + h) / S.rowH) + 8);
    listWindow.style.transform = 'translateY(' + (first * S.rowH) + 'px)';
    var html = '';
    var idx = S.visibleIndices;
    for (var i = first; i <= last; i++) {
      var rowIdx = idx[i];
      html += renderRowHtml(rowIdx);
    }
    listWindow.innerHTML = html;
    // Wire thumb observer + click handlers
    var rows = listWindow.children;
    for (var j = 0; j < rows.length; j++) {
      var img = rows[j].querySelector('img.lz');
      if (img && img.dataset.src) io.observe(img);
    }
    listWindow.onclick = function (e) {
      var row = e.target.closest('.list-row[data-idx]');
      if (!row) return;
      // Toggle selection visual
      var prev = listWindow.querySelector('.list-row.selected');
      if (prev) prev.classList.remove('selected');
      row.classList.add('selected');
      openDetail(+row.dataset.idx);
    };
  }

  function renderRowHtml(rowIdx) {
    var M = S.M;
    // ``nm`` may be empty when WhatsApp recorded no filename for this
    // row.  We never invent one — instead the row shows "(no filename)"
    // (in muted text so it reads as an absence, not a real label) and
    // promotes the caption to the primary line when present.
    var rawName = S.cols.name[rowIdx] || '';
    var cap = S.cols.caption[rowIdx];
    var st = S.cols.statusIdx[rowIdx];
    var statusInfo = M.status[st];
    var mimeIdx = S.cols.mimeIdx[rowIdx];
    var mime = mimeIdx >= 0 ? M.mime[mimeIdx] : '';
    var size = S.cols.size[rowIdx];
    var hashB64 = S.cols.hash[rowIdx] || '';
    var hashHex = S.cols.sha256[rowIdx] || '';
    // Prefer the canonical hex form for display — it's what every
    // forensic SOP references; fall back to the base64 form if hex
    // was unavailable (unusual but defensive).
    var hash = hashHex || hashB64;
    var senderIdx = S.cols.senderIdx[rowIdx];
    var sender = senderIdx >= 0 ? M.sender[senderIdx] : null;
    var convIdx = S.cols.convIdx[rowIdx];
    var conv = convIdx >= 0 ? M.conv[convIdx] : null;
    var ts = S.cols.ts[rowIdx];
    var shareN = S.cols.shareCount[rowIdx];
    var thumbId = S.cols.thumbId[rowIdx];
    var fl = S.cols.flags[rowIdx];
    var fromMe = !!(fl & 1);

    // Thumb
    var thumbHtml;
    if (thumbId) {
      var tp = M.thumbsBase + thumbId.slice(0, 2) + '/' +
               thumbId.slice(2, 4) + '/' + thumbId + '.' + M.thumbsExt;
      thumbHtml = '<div class="col col-thumb"><img class="lz" data-src="' +
                  escapeAttr(tp) + '" alt=""></div>';
    } else {
      thumbHtml = '<div class="col col-thumb icon-fallback">' +
                  iconForMime(mime) + '</div>';
    }

    var hashShort = hash ? (hash.slice(0, 10) + '…' + hash.slice(-6)) : '—';
    var shareCls = shareN > 1 ? ' shared' : '';

    // Sender column: name + JID (full).  When fromMe, show the device
    // owner name + owner JID so the analyst can always tell who acted.
    var senderJid, senderName;
    if (fromMe) {
      senderName = '<span class="you-tag">You</span>';
      senderJid = M.owner && M.owner.jid ? M.owner.jid : '';
    } else if (sender) {
      senderName = escapeHtml(sender.name);
      senderJid = sender.jid || sender.lid || '';
    } else {
      senderName = '—'; senderJid = '';
    }
    var senderHtml = '<span class="nm">' + senderName + '</span>' +
      (senderJid ? '<span class="jid">' + escapeHtml(senderJid) + '</span>' : '');

    var convName = escapeHtml(conv ? conv.name : '—');
    var convJid = conv && conv.jid ? conv.jid : '';
    var convType = conv && conv.type ? conv.type : '';
    var typeTag = convType
      ? ' <span class="conv-type-tag" data-type="' + escapeAttr(convType) + '">' +
        escapeHtml(convType) + '</span>'
      : '';
    var convHtml = '<span class="nm">' + convName + typeTag + '</span>' +
      (convJid ? '<span class="jid">' + escapeHtml(convJid) + '</span>' : '');

    // Primary cell: prefer a real filename; otherwise promote caption
    // (italicised, marked); otherwise an honest "(no filename)" marker
    // with the message id so the analyst can quote the row.
    var primaryHtml, secondaryHtml = '', nameTip;
    if (rawName) {
      primaryHtml = '<span class="nm">' + escapeHtml(rawName) + '</span>';
      if (cap) secondaryHtml = '<span class="ca">' + escapeHtml(cap) + '</span>';
      nameTip = rawName + (cap ? '\n\nCaption: ' + cap : '');
    } else if (cap) {
      // Caption stands in for the filename — italic primary line +
      // explicit "(no filename)" so it can't be mistaken for a real name.
      primaryHtml = '<span class="nm caption-as-name">' + escapeHtml(cap) + '</span>';
      secondaryHtml = '<span class="ca no-filename">(no filename)</span>';
      nameTip = '(no filename)\n\nCaption: ' + cap;
    } else {
      // Truly nameless + caption-less — show the absence honestly,
      // include msg id as a quotable identifier.
      var msgId = S.cols.msgId[rowIdx] || 0;
      primaryHtml = '<span class="nm no-filename">(no filename)</span>';
      secondaryHtml = '<span class="ca no-filename">msg #' + msgId + '</span>';
      nameTip = '(no filename) — message id ' + msgId;
    }
    var dt = ts ? formatTs(ts) : '—';
    var senderTipName = fromMe ? 'You (device owner)' : (sender ? sender.name : '');
    var senderTip = senderTipName + (senderJid ? '\n' + senderJid : '');
    var convTip = (conv ? conv.name : '—') +
                  (convType ? '  (' + convType + ')' : '') +
                  (convJid ? '\n' + convJid : '');

    return '<div class="list-row" data-idx="' + rowIdx + '">' +
      thumbHtml +
      '<div class="col col-name" title="' + escapeAttr(nameTip) + '">' +
        primaryHtml + secondaryHtml + '</div>' +
      '<div class="col col-meta" title="' + escapeAttr(mime + ' · ' + formatBytes(size) + ' (' + size + ' bytes)') + '">' +
        escapeHtml(mimeShort(mime)) + ' · ' + formatBytes(size) + '</div>' +
      '<div class="col col-status"><span class="ms-pill ' +
        statusInfo.cls + '">' + escapeHtml(statusInfo.label) + '</span></div>' +
      '<div class="col col-hash"><code title="' + escapeAttr(hash) + '">' +
        escapeHtml(hashShort) + '</code></div>' +
      '<div class="col col-sender" title="' + escapeAttr(senderTip) + '">' +
        senderHtml + '</div>' +
      '<div class="col col-conv" title="' + escapeAttr(convTip) + '">' +
        convHtml + '</div>' +
      '<div class="col col-time" title="' + escapeAttr(dt) + '">' +
        escapeHtml(dt) + '</div>' +
      '<div class="col col-shares' + shareCls + '" title="' +
        (shareN > 1 ? 'This SHA-256 also appears in ' + (shareN - 1) + ' other chat(s)' : 'Unique to this chat') +
        '">×' + shareN + '</div>' +
      '</div>';
  }

  function iconForMime(mime) {
    if (!mime) return '📄';
    if (mime.indexOf('image') === 0) return '🖼';
    if (mime.indexOf('video') === 0) return '🎞';
    if (mime.indexOf('audio') === 0) return '🎵';
    if (mime.indexOf('pdf')   !== -1) return '📕';
    if (mime.indexOf('zip')   !== -1) return '🗜';
    if (mime.indexOf('text')  === 0) return '📃';
    return '📄';
  }
  function mimeShort(mime) {
    if (!mime) return '?';
    var p = mime.split('/');
    return p[1] || mime;
  }

  // ===== Detail flyout ======================================
  var currentDetailIdx = -1;
  function openDetail(rowIdx) {
    currentDetailIdx = rowIdx;
    var d = document.getElementById('detail');
    var body = document.getElementById('detailBody');
    var title = document.getElementById('detailTitle');
    var M = S.M;

    var nm = S.cols.name[rowIdx] || '(no filename)';
    var cap = S.cols.caption[rowIdx];
    var st = M.status[S.cols.statusIdx[rowIdx]];
    var mime = S.cols.mimeIdx[rowIdx] >= 0 ? M.mime[S.cols.mimeIdx[rowIdx]] : '';
    var ext = S.cols.extIdx[rowIdx] >= 0 ? M.ext[S.cols.extIdx[rowIdx]] : '';
    var size = S.cols.size[rowIdx];
    var hashB64 = S.cols.hash[rowIdx];
    var hashHex = S.cols.sha256[rowIdx];
    var hash = hashHex || hashB64;          // primary display
    var encB64 = S.cols.encHash[rowIdx];
    var encHex = S.cols.encSha256[rowIdx];
    var ts = S.cols.ts[rowIdx];
    var w = S.cols.w[rowIdx], h = S.cols.h[rowIdx], dur = S.cols.dur[rowIdx];
    var path = S.cols.path[rowIdx];
    var conv = S.cols.convIdx[rowIdx] >= 0 ? M.conv[S.cols.convIdx[rowIdx]] : null;
    var sender = S.cols.senderIdx[rowIdx] >= 0 ? M.sender[S.cols.senderIdx[rowIdx]] : null;
    var fl = S.cols.flags[rowIdx];
    var thumbId = S.cols.thumbId[rowIdx];
    var url = S.cols.url[rowIdx];
    var recovery = S.cols.recovery[rowIdx];
    var rawStatus = S.cols.rawStatus[rowIdx];
    var assoc = S.cols.assocKind[rowIdx];
    var shareN = S.cols.shareCount[rowIdx];
    var hdTwin = S.cols.hdTwinMsgId[rowIdx];
    var expiry = S.cols.expiry[rowIdx];
    var recoveryTs = S.cols.recoveryTs[rowIdx];
    var fromMe = !!(fl & 1);

    title.textContent = nm;

    // Thumb
    var preview;
    if (thumbId) {
      var tp = M.thumbsBase + thumbId.slice(0, 2) + '/' + thumbId.slice(2, 4) +
               '/' + thumbId + '.' + M.thumbsExt;
      preview = '<img class="detail-thumb" src="' + escapeAttr(tp) + '" alt="">';
    } else {
      preview = '<div class="detail-thumb-fallback">' + iconForMime(mime) + '</div>';
    }

    // Sections
    var html = preview;
    html += '<div class="detail-section"><h4>Status</h4>' +
            '<table class="detail-table">' +
            row('State', '<span class="ms-pill ' + st.cls + '">' + escapeHtml(st.label) + '</span>') +
            row('Raw status', escapeHtml(rawStatus || '—')) +
            row('Recovery method', escapeHtml(recovery || '—')) +
            (recoveryTs ? row('Recovered at', escapeHtml(formatTs(recoveryTs))) : '') +
            (expiry ? row('CDN expiry', escapeHtml(formatTs(expiry > 1e12 ? expiry : expiry * 1000))) : '') +
            row('On disk', (fl & 16) ? 'yes' : 'no') +
            '</table></div>';

    html += '<div class="detail-section"><h4>File</h4>' +
            '<table class="detail-table">' +
            row('Name', escapeHtml(nm)) +
            (cap ? row('Caption', escapeHtml(cap)) : '') +
            row('MIME', '<code>' + escapeHtml(mime || '—') + '</code>') +
            row('Extension', '<code>' + escapeHtml(ext || '—') + '</code>') +
            row('Size', formatBytes(size) + ' (' + formatNum(size) + ' bytes)') +
            (w || h ? row('Dimensions', w + '×' + h) : '') +
            (dur ? row('Duration', formatDuration(dur)) : '') +
            ((fl & 2) ? row('HD twin', 'yes') : '') +
            (assoc ? row('Assoc kind', escapeHtml(assoc)) : '') +
            row('Resolved path', '<code>' + escapeHtml(path || '—') + '</code>') +
            '</table></div>';

    html += '<div class="detail-section"><h4>Hashes</h4>' +
            '<table class="detail-table">' +
            row('SHA-256 (file, hex)',
                hashHex ? '<code>' + escapeHtml(hashHex) + '</code>' : '—') +
            row('SHA-256 (file, base64)',
                hashB64 ? '<code>' + escapeHtml(hashB64) + '</code>'
                        + ' <span style="color:#888;font-size:10px">(WhatsApp DB form)</span>'
                        : '—') +
            row('SHA-256 (encrypted, hex)',
                encHex ? '<code>' + escapeHtml(encHex) + '</code>' : '—') +
            row('SHA-256 (encrypted, base64)',
                encB64 ? '<code>' + escapeHtml(encB64) + '</code>'
                       + ' <span style="color:#888;font-size:10px">(WhatsApp DB form)</span>'
                       : '—') +
            row('Shared in', shareN + (shareN > 1 ? ' chats' : ' chat')) +
            '</table>';
    if (hash) {
      html += '<div class="detail-actions">' +
              (hashHex ? '<button data-act="copy-hash-hex">Copy hex SHA-256</button>' : '') +
              (hashB64 ? '<button data-act="copy-hash-b64">Copy base64 SHA-256</button>' : '') +
              '<button data-act="filter-hash">Filter by this hash</button>' +
              '</div>';
    }
    html += '</div>';

    if (url) {
      html += '<div class="detail-section"><h4>CDN</h4>' +
              '<table class="detail-table">' +
              row('Has decrypt key', (fl & 64) ? 'yes' : 'no') +
              row('URL', '<code>' + escapeHtml(url) + '</code>') +
              '</table></div>';
    }

    var convType = conv && conv.type ? conv.type : '';
    var convTypeTag = convType
      ? ' <span class="conv-type-tag" data-type="' + escapeAttr(convType) + '">' +
        escapeHtml(convType) + '</span>'
      : '';
    html += '<div class="detail-section"><h4>Provenance</h4>' +
            '<table class="detail-table">' +
            row('Conversation', escapeHtml(conv ? conv.name : '—') + convTypeTag) +
            (conv && conv.jid ? row('Conv. JID', '<code>' + escapeHtml(conv.jid) + '</code>') : '') +
            row('Sender', fromMe
                ? '<b style="color:#075e54">You</b> (device owner)'
                : escapeHtml(sender ? sender.name : '—')) +
            (!fromMe && sender && sender.jid
              ? row('Sender JID', '<code>' + escapeHtml(sender.jid) + '</code>') : '') +
            (!fromMe && sender && sender.lid
              ? row('Sender LID', '<code>' + escapeHtml(sender.lid) + '</code>') : '') +
            (fromMe && M.owner.jid
              ? row('Owner JID', '<code>' + escapeHtml(M.owner.jid) + '</code>') : '') +
            row('Timestamp', escapeHtml(formatTs(ts))) +
            '</table></div>';

    if (shareN > 1 && hash) {
      html += '<div class="detail-section"><h4>Where this hash also appears</h4>' +
              '<div class="detail-shares" id="detailSharesList"></div></div>';
    }

    body.innerHTML = html;
    d.style.display = '';

    // Wire detail action buttons
    body.addEventListener('click', function (e) {
      var b = e.target.closest('button[data-act]');
      if (!b) return;
      var act = b.dataset.act;
      function flashCopied(orig) {
        var saved = b.textContent;
        b.textContent = '✓ copied';
        setTimeout(function () { b.textContent = saved; }, 1200);
      }
      if (act === 'copy-hash-hex') {
        navigator.clipboard.writeText(hashHex).then(flashCopied);
      } else if (act === 'copy-hash-b64') {
        navigator.clipboard.writeText(hashB64).then(flashCopied);
      } else if (act === 'filter-hash') {
        S.search = hashHex || hashB64;
        document.getElementById('searchBox').value = S.search;
        applyAndRender();
      }
    }, { once: false });

    if (shareN > 1 && hash) {
      // Find rows with the same hash and list them.  Match on the
      // base64 form because that's always present (the hex form is
      // derived and may be empty for malformed rows).
      var list = document.getElementById('detailSharesList');
      var hashCol = S.cols.hash;
      var matchKey = hashB64 || hashHex;
      var html2 = '';
      var found = 0;
      for (var i = 0; i < S.N && found < 60; i++) {
        if (i === rowIdx) continue;
        if (hashCol[i] !== matchKey) continue;
        found++;
        var c2 = S.cols.convIdx[i] >= 0 ? M.conv[S.cols.convIdx[i]] : null;
        var s2 = S.cols.senderIdx[i] >= 0 ? M.sender[S.cols.senderIdx[i]] : null;
        var st2 = M.status[S.cols.statusIdx[i]];
        var fromMe2 = !!(S.cols.flags[i] & 1);
        // Avatar (real if we have one, else a coloured initial bubble)
        var avtag = c2 && c2.avatar
          ? '<img class="share-av" src="' + escapeAttr(c2.avatar) + '" alt="">'
          : '<span class="share-av initial">' +
              escapeHtml(avatarInitial(c2 ? c2.name : '?')) + '</span>';
        var typeTag = (c2 && c2.type)
          ? '<span class="conv-type-tag" data-type="' + escapeAttr(c2.type) + '">' +
            escapeHtml(c2.type) + '</span>'
          : '';
        var convJid2 = c2 && c2.jid ? c2.jid : '';
        var senderName2 = fromMe2
          ? ((M.owner.name || 'Device Owner') + ' (you)')
          : (s2 ? s2.name : '');
        var senderJid2 = fromMe2
          ? (M.owner.jid || '')
          : (s2 ? (s2.jid || s2.lid || '') : '');
        html2 += '<div class="detail-share-row" data-idx="' + i + '">' +
                 avtag +
                 '<div class="dsr-body">' +
                   '<div class="dsr-head">' +
                     '<span class="conv">' + escapeHtml(c2 ? c2.name : '—') + '</span>' +
                     typeTag +
                     '<span class="ms-pill ' + st2.cls + '">' +
                        escapeHtml(st2.label) + '</span>' +
                   '</div>' +
                   (convJid2
                     ? '<div class="dsr-jid"><code>' + escapeHtml(convJid2) + '</code></div>'
                     : '') +
                   '<div class="meta">' + escapeHtml(senderName2) +
                     (senderJid2 ? ' · <code>' + escapeHtml(senderJid2) + '</code>' : '') +
                     (S.cols.ts[i] ? ' · ' + escapeHtml(formatTs(S.cols.ts[i])) : '') +
                   '</div>' +
                 '</div></div>';
      }
      list.innerHTML = html2 || '<div class="meta">No other matches in current scope.</div>';
      list.onclick = function (e) {
        var r = e.target.closest('.detail-share-row[data-idx]');
        if (r) openDetail(+r.dataset.idx);
      };
    }
  }
  function row(k, v) {
    return '<tr><td>' + k + '</td><td>' + v + '</td></tr>';
  }
  function bindDetailClose() {
    document.getElementById('detailClose').addEventListener('click', function () {
      document.getElementById('detail').style.display = 'none';
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') {
        document.getElementById('detail').style.display = 'none';
        document.getElementById('exportModal').style.display = 'none';
      }
    });
  }

  // ===== Histogram (custom canvas) ===========================
  var hCanvas, hCtx, hSelEl, hTipEl, hWidth, hHeight, hMaxCount;
  function setupHistogram() {
    var host = document.getElementById('histChart');
    host.innerHTML = '';
    hCanvas = document.createElement('canvas');
    hCanvas.className = 'hist-canvas';
    host.appendChild(hCanvas);
    hSelEl = document.createElement('div');
    hSelEl.className = 'hist-selection';
    hSelEl.style.display = 'none';
    host.appendChild(hSelEl);
    hTipEl = document.createElement('div');
    hTipEl.className = 'hist-tooltip';
    hTipEl.style.display = 'none';
    host.appendChild(hTipEl);
    hCtx = hCanvas.getContext('2d');
    var dragging = false, dragStart = -1, dragEnd = -1;

    function pxToBin(x) {
      if (!S.M.hist || !S.M.hist.bins.length) return -1;
      var w = host.clientWidth;
      var n = S.M.hist.bins.length;
      var bw = w / n;
      var b = Math.floor(x / bw);
      return Math.max(0, Math.min(n - 1, b));
    }
    host.addEventListener('mousedown', function (e) {
      var r = host.getBoundingClientRect();
      dragStart = dragEnd = pxToBin(e.clientX - r.left);
      dragging = true;
      drawSelection(dragStart, dragEnd);
    });
    host.addEventListener('mousemove', function (e) {
      var r = host.getBoundingClientRect();
      var b = pxToBin(e.clientX - r.left);
      if (dragging) {
        dragEnd = b;
        drawSelection(dragStart, dragEnd);
      } else {
        // Tooltip
        if (b < 0) { hTipEl.style.display = 'none'; return; }
        var ts = S.M.hist.bins[b];
        var n = S.M.hist.counts[b];
        hTipEl.style.display = '';
        hTipEl.style.left = (e.clientX - r.left + 8) + 'px';
        hTipEl.style.top = '4px';
        hTipEl.textContent = formatDay(ts) + ' · ' + n + ' file' + (n === 1 ? '' : 's');
      }
    });
    host.addEventListener('mouseleave', function () {
      hTipEl.style.display = 'none';
    });
    document.addEventListener('mouseup', function () {
      if (!dragging) return;
      dragging = false;
      var a = Math.min(dragStart, dragEnd), b = Math.max(dragStart, dragEnd);
      // Click without drag selects only that day; treat as zero-width range
      S.dateRange = [a, b];
      applyAndRender();
    });
  }

  function drawSelection(a, b) {
    if (a < 0) { hSelEl.style.display = 'none'; return; }
    var host = document.getElementById('histChart');
    var w = host.clientWidth;
    var n = S.M.hist.bins.length;
    var bw = w / n;
    var lo = Math.min(a, b), hi = Math.max(a, b);
    hSelEl.style.display = '';
    hSelEl.style.left = (lo * bw) + 'px';
    hSelEl.style.width = ((hi - lo + 1) * bw) + 'px';
  }

  function renderHistogram() {
    if (!hCanvas) setupHistogram();
    var host = document.getElementById('histChart');
    var w = host.clientWidth, h = host.clientHeight;
    var dpr = window.devicePixelRatio || 1;
    hCanvas.width = w * dpr; hCanvas.height = h * dpr;
    hCanvas.style.width = w + 'px'; hCanvas.style.height = h + 'px';
    hCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
    hCtx.clearRect(0, 0, w, h);

    if (!S.M.hist || !S.M.hist.bins.length) return;
    // Cascading-style counts: bars reflect "what counts per day would I
    // see if the date filter were CLEARED?" — same flight-fare logic as
    // facet counts.  Without this, the moment the analyst selects a
    // date range every other day's bar disappears, which makes it
    // impossible to see the surrounding context or pick a different
    // range.
    var bins = S.M.hist.bins;
    var n = bins.length;
    var counts = new Int32Array(n);
    var ts = S.cols.ts;
    var dayMs = S.M.hist.dayMs;
    var startMs = bins[0];

    // Build the mask used for histogram bucketing: full activeMask
    // minus the current date filter (S.dateRange).  Cheapest path is
    // to recompute the mask without dateMask().
    var mask;
    if (S.dateRange) {
      mask = bsAllOnes(S.N);
      var FACETS = ['status','mime','ext','conv','sender'];
      for (var fi = 0; fi < FACETS.length; fi++) {
        var u = unionSelected(FACETS[fi]);
        if (u) bsAndInPlace(mask, u);
      }
      var sm = searchMask();   if (sm) bsAndInPlace(mask, sm);
      var zm = sizeMask();     if (zm) bsAndInPlace(mask, zm);
      if (S._stickersHidden && S._stickerExcludeMask) {
        bsAndInPlace(mask, S._stickerExcludeMask);
      }
    } else {
      mask = S.activeMask;
    }

    for (var i = 0; i < S.N; i++) {
      if (!getBit(mask, i)) continue;
      var t = ts[i];
      if (t < startMs) continue;
      var bIdx = Math.floor((t - startMs) / dayMs);
      if (bIdx >= 0 && bIdx < n) counts[bIdx]++;
    }
    var maxC = 0;
    for (var j = 0; j < n; j++) if (counts[j] > maxC) maxC = counts[j];
    hMaxCount = maxC;

    var bw = w / n;
    hCtx.fillStyle = '#075e54';
    for (var k = 0; k < n; k++) {
      if (counts[k] === 0) continue;
      var bh = (counts[k] / Math.max(1, maxC)) * (h - 18);
      var bx = k * bw, by = h - bh - 14;
      hCtx.fillRect(bx, by, Math.max(1, bw - 0.5), bh);
    }
    // Day axis ticks (sparse)
    hCtx.fillStyle = '#9aa3ac';
    hCtx.font = '10px sans-serif';
    hCtx.textAlign = 'center';
    var tickN = Math.min(8, n);
    for (var t2 = 0; t2 < tickN; t2++) {
      var idx = Math.floor((n - 1) * (t2 / Math.max(1, tickN - 1)));
      var label = formatDayShort(bins[idx]);
      hCtx.fillText(label, idx * bw + bw / 2, h - 2);
    }

    // Range pill + clear btn
    var range = document.getElementById('histRange');
    var clearBtn = document.getElementById('histClear');
    if (S.dateRange) {
      var lo = bins[S.dateRange[0]], hi = bins[S.dateRange[1]] + dayMs - 1;
      range.textContent = formatDay(lo) + ' — ' + formatDay(hi);
      clearBtn.classList.add('visible');
      drawSelection(S.dateRange[0], S.dateRange[1]);
    } else {
      range.textContent = formatDay(bins[0]) + ' — ' + formatDay(bins[n - 1]);
      clearBtn.classList.remove('visible');
      hSelEl.style.display = 'none';
    }
  }

  // ===== Most-shared tab =====================================
  function renderSharingTab() {
    var list = document.getElementById('sharingList');
    if (list.childElementCount > 0) return; // already rendered
    // Build hash → first row idx + count + names.  We key the dedup
    // by base64 form (always present when WhatsApp hashed the file)
    // but display the hex form for the forensic-canonical look.
    var byHash = Object.create(null);
    var hashCol = S.cols.hash, hexCol = S.cols.sha256, shareCol = S.cols.shareCount;
    for (var i = 0; i < S.N; i++) {
      var hh = hashCol[i];
      if (!hh) continue;
      if (shareCol[i] < 2) continue;
      if (!byHash[hh]) byHash[hh] = { idx: i, n: shareCol[i] };
    }
    var arr = Object.keys(byHash).map(function (k) {
      return { hash: k, hex: hexCol[byHash[k].idx] || '',
               idx: byHash[k].idx, n: byHash[k].n };
    });
    arr.sort(function (a, b) { return b.n - a.n; });
    var cap = 200;
    var capped = arr.length > cap;
    if (capped) arr = arr.slice(0, cap);
    var html = '';
    for (var i2 = 0; i2 < arr.length; i2++) {
      var it = arr[i2];
      var nm = S.cols.name[it.idx] || '(no filename)';
      var size = S.cols.size[it.idx];
      var mime = S.cols.mimeIdx[it.idx] >= 0 ? S.M.mime[S.cols.mimeIdx[it.idx]] : '';
      // Display the canonical hex form when we have it; fall back to
      // the base64 form for older cases where derivation failed.
      var disp = it.hex || it.hash;
      html += '<div class="share-item" data-hash="' + escapeAttr(disp) + '">' +
              '<div class="head"><span class="hash">' + escapeHtml(disp.slice(0, 18)) + '…' +
              escapeHtml(disp.slice(-8)) + '</span>' +
              '<span class="n">×' + it.n + '</span></div>' +
              '<div class="name">' + escapeHtml(nm) + '</div>' +
              '<div class="stats">' + escapeHtml(mimeShort(mime)) + ' · ' +
              formatBytes(size) + '</div>' +
              '</div>';
    }
    if (capped) html += '<div class="share-item" style="cursor:default;color:#888">' +
                       '…' + (Object.keys(byHash).length - cap) + ' more</div>';
    list.innerHTML = html;
    list.onclick = function (e) {
      var item = e.target.closest('.share-item[data-hash]');
      if (!item) return;
      S.search = item.dataset.hash;
      document.getElementById('searchBox').value = S.search;
      applyAndRender();
      // Switch back to filters tab so analyst sees the result
      var t = document.querySelector('.tab[data-tab="filters"]');
      if (t) t.click();
    };
  }

  // ===== Orphans tab =========================================
  function renderOrphansTab() {
    var orphans = window.ORPHANS || [];
    var btn = document.getElementById('orphansTabBtn');
    if (orphans.length === 0) {
      btn.style.display = 'none';
      document.getElementById('orphanList').innerHTML =
        '<div style="padding:12px;color:#888;font-size:11px">' +
        'No orphan files indexed for this case.</div>';
      return;
    }
    var search = (document.getElementById('orphanSearch').value || '').toLowerCase();
    var matches = [];
    for (var i = 0; i < orphans.length; i++) {
      var o = orphans[i];
      // [id, file_path, file_name, folder, file_size, mime, parsed_ts,
      //  hash, matched_msg_id, matched_conv, source_type, thumbId, w, h, dur]
      if (search) {
        var hay = (o[2] + ' ' + o[3] + ' ' + (o[7] || '')).toLowerCase();
        if (hay.indexOf(search) === -1) continue;
      }
      matches.push(o);
      if (matches.length >= 500) break;
    }
    var list = document.getElementById('orphanList');
    var html = '';
    for (var j = 0; j < matches.length; j++) {
      var oo = matches[j];
      var thumb = oo[11] ? '<img src="' + escapeAttr(S.M.thumbsBase +
        oo[11].slice(0,2) + '/' + oo[11].slice(2,4) + '/' + oo[11] + '.' + S.M.thumbsExt) + '">'
        : '<span style="font-size:14px">' + iconForMime(oo[5]) + '</span>';
      html += '<div class="orphan-item">' +
              '<div class="ot">' + thumb + '</div>' +
              '<div class="ob">' +
                '<div class="nm">' + escapeHtml(oo[2] || '(no name)') + '</div>' +
                '<div class="sub">' + escapeHtml(oo[3] || '') + ' · ' +
                  formatBytes(oo[4]) + (oo[8] ? ' · matched ✓' : '') + '</div>' +
              '</div></div>';
    }
    if (matches.length === 0) {
      html = '<div style="padding:12px;color:#888;font-size:11px">No matches.</div>';
    }
    list.innerHTML = html;
    document.getElementById('orphanSearch').oninput = renderOrphansTab;
  }

  // ===== Exports =============================================
  function gatherSelectedRowsForExport() {
    var idx = S.visibleIndices, M = S.M;
    var out = [];
    for (var i = 0; i < idx.length; i++) {
      var k = idx[i];
      var conv = S.cols.convIdx[k] >= 0 ? M.conv[S.cols.convIdx[k]] : null;
      var sender = S.cols.senderIdx[k] >= 0 ? M.sender[S.cols.senderIdx[k]] : null;
      var st = M.status[S.cols.statusIdx[k]];
      out.push({
        ts: S.cols.ts[k] ? formatTsIso(S.cols.ts[k]) : '',
        conv: conv ? conv.name : '',
        convJid: conv ? (conv.jid || '') : '',
        sender: (S.cols.flags[k] & 1) ? ((M.owner.name || 'Device Owner') + ' (you)')
                                      : (sender ? sender.name : ''),
        senderJid: (S.cols.flags[k] & 1) ? (M.owner.jid || '')
                                         : (sender ? (sender.jid || '') : ''),
        senderLid: sender ? (sender.lid || '') : '',
        name: S.cols.name[k] || '',
        caption: S.cols.caption[k] || '',
        mime: S.cols.mimeIdx[k] >= 0 ? M.mime[S.cols.mimeIdx[k]] : '',
        ext: S.cols.extIdx[k] >= 0 ? M.ext[S.cols.extIdx[k]] : '',
        size: S.cols.size[k] || 0,
        status: st.label,
        statusCode: st.code,
        sha256_hex:    S.cols.sha256[k] || '',     // canonical
        sha256_b64:    S.cols.hash[k]   || '',     // WhatsApp DB form
        encSha256_hex: S.cols.encSha256[k] || '',
        encSha256_b64: S.cols.encHash[k]   || '',
        shareCount: S.cols.shareCount[k] || 1,
        onDisk: !!(S.cols.flags[k] & 16),
        path: S.cols.path[k] || '',
        msgId: S.cols.msgId[k] || 0,
        mediaRowId: S.cols.id[k] || 0,
        hasUrl: !!(S.cols.flags[k] & 32),
        hasKey: !!(S.cols.flags[k] & 64),
      });
    }
    return out;
  }
  function exportCsv() {
    var rows = gatherSelectedRowsForExport();
    if (!rows.length) { alert('Nothing selected to export.'); return; }
    var cols = ['ts','conv','convJid','sender','senderJid','senderLid',
                'name','caption','mime','ext','size','status','statusCode',
                'sha256_hex','sha256_b64','encSha256_hex','encSha256_b64',
                'shareCount','onDisk','hasUrl','hasKey',
                'path','msgId','mediaRowId'];
    var lines = [cols.join(',')];
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i], parts = [];
      for (var c = 0; c < cols.length; c++) {
        parts.push(csvEscape(r[cols[c]]));
      }
      lines.push(parts.join(','));
    }
    // UTF-8 BOM so Excel detects encoding correctly
    var blob = new Blob(['﻿' + lines.join('\n')],
                       { type: 'text/csv;charset=utf-8' });
    triggerDownload(blob, exportFilename('csv'));
  }
  function exportXlsx() {
    // Without vendoring a full XLSX library, we emit a SpreadsheetML 2003
    // XML file with an .xls extension — Excel and LibreOffice open it
    // natively as a real spreadsheet (formatted columns, not CSV).
    var rows = gatherSelectedRowsForExport();
    if (!rows.length) { alert('Nothing selected to export.'); return; }
    var cols = ['ts','conv','convJid','sender','senderJid','senderLid',
                'name','caption','mime','ext','size','status','statusCode',
                'sha256_hex','sha256_b64','encSha256_hex','encSha256_b64',
                'shareCount','onDisk','hasUrl','hasKey',
                'path','msgId','mediaRowId'];
    var sb = ['<?xml version="1.0"?>',
      '<?mso-application progid="Excel.Sheet"?>',
      '<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"',
      '  xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">',
      '<Worksheet ss:Name="Media"><Table>'];
    sb.push('<Row>');
    for (var c = 0; c < cols.length; c++) {
      sb.push('<Cell><Data ss:Type="String">' + xmlEscape(cols[c]) + '</Data></Cell>');
    }
    sb.push('</Row>');
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i];
      sb.push('<Row>');
      for (var k = 0; k < cols.length; k++) {
        var v = r[cols[k]];
        var t = (typeof v === 'number') ? 'Number'
              : (typeof v === 'boolean') ? 'String' : 'String';
        var s = (v === null || v === undefined) ? '' : ('' + v);
        if (typeof v === 'boolean') s = v ? 'true' : 'false';
        sb.push('<Cell><Data ss:Type="' + t + '">' + xmlEscape(s) + '</Data></Cell>');
      }
      sb.push('</Row>');
    }
    sb.push('</Table></Worksheet></Workbook>');
    var blob = new Blob([sb.join('')], { type: 'application/vnd.ms-excel' });
    triggerDownload(blob, exportFilename('xls'));
  }
  function exportHtml() {
    var rows = gatherSelectedRowsForExport();
    if (!rows.length) { alert('Nothing selected to export.'); return; }
    var M = S.M;
    var ownerLine = M.owner.name
      ? '<div><b>Device owner:</b> ' + escapeHtml(M.owner.name) +
        (M.owner.jid ? ' &lt;<code>' + escapeHtml(M.owner.jid) + '</code>&gt;' : '') +
        '</div>' : '';
    var hdr = '<!DOCTYPE html><html><head><meta charset="utf-8"><title>WAInsight Media — filtered selection</title>' +
      '<style>body{font-family:system-ui,sans-serif;font-size:12px;color:#222;padding:24px;}' +
      'h1{color:#075e54;font-size:18px;}table{border-collapse:collapse;width:100%;font-size:11px;}' +
      'th,td{border:1px solid #ddd;padding:4px 6px;text-align:left;vertical-align:top;}' +
      'th{background:#f5f5f5;font-size:10px;text-transform:uppercase;}' +
      'code{background:#f0f4f8;padding:1px 4px;border-radius:3px;color:#128c7e;font-size:10px;word-break:break-all;}' +
      '.ms-pill{display:inline-block;padding:2px 6px;border-radius:10px;font-size:10px;font-weight:600;}' +
      '.ms-original{background:#e8f5e9;color:#1b5e20}.ms-downloaded{background:#e3f2fd;color:#0d47a1}' +
      '.ms-hashlinked{background:#e1bee7;color:#6a1b9a}.ms-hashlinked-del{background:#f3e5f5;color:#7b1fa2}' +
      '.ms-orphan{background:#c8e6c9;color:#1b5e20}.ms-missing-dl{background:#fff3e0;color:#e65100}' +
      '.ms-missing-key{background:#fff8e1;color:#f57f17}.ms-missing-url{background:#ffebee;color:#b71c1c}' +
      '.ms-fail{background:#ffebee;color:#c62828}.ms-expired{background:#ffebee;color:#b71c1c}' +
      '.ms-thumb{background:#e0e0e0;color:#424242}.ms-unknown{background:#eceff1;color:#455a64}' +
      '.banner{background:#fffde7;border:1px solid #fff59d;border-left:6px solid #f57f17;' +
      'padding:12px;margin:0 0 16px;border-radius:6px;}' +
      '</style></head><body>' +
      '<h1>WAInsight — Media (filtered selection)</h1>' +
      '<div class="banner">' +
      '<div><b>Scope:</b> ' + escapeHtml(M.scope.label) + '</div>' +
      '<div><b>Generated:</b> ' + escapeHtml(formatTs(Date.now())) + '</div>' +
      ownerLine +
      '<div><b>Files in selection:</b> ' + rows.length + '</div>' +
      '</div>';

    var th = ['When','Conversation','Sender','File','Caption','Type','Size',
              'Status','SHA-256','×','Path'];
    hdr += '<table><thead><tr>';
    for (var i = 0; i < th.length; i++) hdr += '<th>' + th[i] + '</th>';
    hdr += '</tr></thead><tbody>';
    for (var j = 0; j < rows.length; j++) {
      var r = rows[j];
      var clsMap = {
        'original':'ms-original', 'downloaded':'ms-downloaded',
        'hash_linked':'ms-hashlinked','hash_linked_after_delete':'ms-hashlinked-del',
        'orphan_recovered':'ms-orphan','missing_downloadable':'ms-missing-dl',
        'missing_no_key':'ms-missing-key','missing_no_url':'ms-missing-url',
        'download_failed':'ms-fail','expired':'ms-expired',
        'thumbnail_only':'ms-thumb','unknown':'ms-unknown'
      };
      var hashCell = '';
      if (r.sha256_hex) hashCell += '<code>' + escapeHtml(r.sha256_hex) + '</code>';
      if (r.sha256_b64) hashCell += (hashCell ? '<br>' : '') +
        '<code style="opacity:.6">' + escapeHtml(r.sha256_b64) + '</code>';
      hdr += '<tr>' +
        '<td>' + escapeHtml(r.ts) + '</td>' +
        '<td>' + escapeHtml(r.conv) + (r.convJid ? '<br><code>' + escapeHtml(r.convJid) + '</code>' : '') + '</td>' +
        '<td>' + escapeHtml(r.sender) + (r.senderJid ? '<br><code>' + escapeHtml(r.senderJid) + '</code>' : '') + '</td>' +
        '<td>' + escapeHtml(r.name) + '</td>' +
        '<td>' + escapeHtml(r.caption || '') + '</td>' +
        '<td>' + escapeHtml(r.mime) + '</td>' +
        '<td>' + formatBytes(r.size) + '</td>' +
        '<td><span class="ms-pill ' + (clsMap[r.statusCode] || 'ms-unknown') + '">' +
            escapeHtml(r.status) + '</span></td>' +
        '<td>' + hashCell + '</td>' +
        '<td>' + r.shareCount + '</td>' +
        '<td><code>' + escapeHtml(r.path) + '</code></td>' +
      '</tr>';
    }
    hdr += '</tbody></table></body></html>';
    var blob = new Blob([hdr], { type: 'text/html;charset=utf-8' });
    triggerDownload(blob, exportFilename('html'));
  }
  function copyHashes() {
    // Copy ONE hex SHA-256 per unique file in the current selection.
    // Hex is the canonical form forensic tools / SOPs use; analysts who
    // need the WhatsApp base64 form can read it from the detail flyout.
    var idx = S.visibleIndices, hexCol = S.cols.sha256, b64Col = S.cols.hash;
    var seen = Object.create(null), out = [];
    for (var i = 0; i < idx.length; i++) {
      var h = hexCol[idx[i]] || b64Col[idx[i]];
      if (h && !seen[h]) { seen[h] = 1; out.push(h); }
    }
    if (!out.length) { alert('No SHA-256 hashes in current selection.'); return; }
    navigator.clipboard.writeText(out.join('\n')).then(function () {
      var btn = document.getElementById('copyHashesBtn');
      var orig = btn.textContent;
      btn.textContent = '✓ ' + out.length + ' copied';
      setTimeout(function () { btn.textContent = orig; }, 1500);
    });
  }
  function csvEscape(v) {
    if (v === null || v === undefined) return '';
    var s = ('' + v);
    if (s.indexOf(',') !== -1 || s.indexOf('"') !== -1 ||
        s.indexOf('\n') !== -1 || s.indexOf('\r') !== -1) {
      return '"' + s.replace(/"/g, '""') + '"';
    }
    return s;
  }
  function xmlEscape(s) {
    return ('' + s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
                   .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function exportFilename(ext) {
    var ts = new Date().toISOString().replace(/[:.]/g, '').slice(0, 15);
    return 'wainsight_media_' + ts + '.' + ext;
  }
  function triggerDownload(blob, name) {
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    setTimeout(function () {
      document.body.removeChild(a);
      URL.revokeObjectURL(a.href);
    }, 0);
  }

  // ===== Case info ==========================================
  function renderCaseInfo() {
    var ci = S.M.case || {};
    var owner = S.M.owner || {};
    var bits = [];
    if (ci.case_id) bits.push('<div><b>Case</b> ' + escapeHtml(ci.case_id) + '</div>');
    if (ci.examiner) bits.push('<div><b>Examiner</b> ' + escapeHtml(ci.examiner) + '</div>');
    if (S.M.scope && S.M.scope.label)
      bits.push('<div><b>Scope</b> ' + escapeHtml(S.M.scope.label) + '</div>');
    if (owner.name)
      bits.push('<div><b>Owner</b> ' + escapeHtml(owner.name) +
                (owner.jid ? '<br><code>' + escapeHtml(owner.jid) + '</code>' : '') +
                '</div>');
    document.getElementById('caseInfo').innerHTML = bits.join('');

    document.getElementById('footerMeta').innerHTML =
      '<div>Generated ' + escapeHtml(formatTs(S.M.generatedAt)) + '</div>' +
      '<div>' + formatNum(S.N) + ' rows · ' +
        formatNum(S.M.thumbCount || 0) + ' thumbs · ' +
        formatNum(S.M.orphanCount || 0) + ' orphans</div>' +
      '<div style="margin-top:6px;color:#9aa3ac;font-size:9px">' +
        'WAInsight · file:// offline forensic dashboard</div>';
  }

  // ===== Helpers ============================================
  function escapeHtml(s) {
    if (s === null || s === undefined) return '';
    return ('' + s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
                   .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function escapeAttr(s) { return escapeHtml(s); }
  function formatNum(n) {
    if (n === null || n === undefined) return '0';
    return (+n).toLocaleString();
  }
  function formatBytes(n) {
    if (!n) return '—';
    var units = ['B','KB','MB','GB','TB'];
    var u = 0; var v = +n;
    while (v >= 1024 && u < units.length - 1) { v /= 1024; u++; }
    return (u === 0 ? v.toFixed(0) : v.toFixed(v < 10 ? 1 : 0)) + ' ' + units[u];
  }
  // ----- Timezone-aware formatters --------------------------------
  // S.tz is one of:
  //   "local"   → use the analyst's machine timezone (default)
  //   "utc"     → UTC
  //   "<float>" → fixed offset in hours (e.g. "5.5" = IST, "-5" = ET)
  // The selector at the top of the page mutates S.tz then triggers a
  // full re-render so every visible timestamp picks up the change.
  function _shiftedDate(ms) {
    if (!S.tz || S.tz === 'local') return new Date(ms);
    if (S.tz === 'utc') {
      // Build a date that, when its toLocale*() methods are called
      // with `timeZone:"UTC"`, returns the UTC wall-clock fields.
      return new Date(ms);
    }
    // Fixed offset → produce a date whose UTC fields are the desired
    // wall-clock.  We then format it with timeZone:"UTC" so toLocale
    // methods don't double-shift.
    var off = parseFloat(S.tz);
    if (isNaN(off)) return new Date(ms);
    return new Date(ms + off * 3600 * 1000);
  }
  function _tzOpts() {
    if (S.tz === 'utc') return { timeZone: 'UTC' };
    if (!S.tz || S.tz === 'local') return {};
    return { timeZone: 'UTC' };  // fixed offset → format the shifted date as UTC
  }
  function formatTs(ms) {
    if (!ms) return '—';
    try {
      var d = _shiftedDate(ms);
      var s = d.toLocaleString(undefined, _tzOpts());
      // Append a TZ tag so the display is unambiguous
      if (S.tz && S.tz !== 'local') {
        if (S.tz === 'utc') s += ' UTC';
        else s += ' UTC' + (parseFloat(S.tz) >= 0 ? '+' : '') + S.tz;
      }
      return s;
    } catch (_) { return '—'; }
  }
  function formatTsIso(ms) {
    if (!ms) return '';
    try {
      // CSV/XLSX export → keep ISO 8601 + TZ offset.  When local,
      // toISOString gives UTC; when fixed-offset, we encode it.
      if (!S.tz || S.tz === 'local') return new Date(ms).toISOString();
      if (S.tz === 'utc') return new Date(ms).toISOString();
      var off = parseFloat(S.tz);
      var shifted = new Date(ms + off * 3600 * 1000);
      var pad = function (x, n) { x = '' + x; while (x.length < (n || 2)) x = '0' + x; return x; };
      var h = Math.trunc(off);
      var m = Math.round((Math.abs(off) - Math.abs(h)) * 60);
      var sign = off >= 0 ? '+' : '-';
      return shifted.getUTCFullYear() + '-' +
             pad(shifted.getUTCMonth() + 1) + '-' +
             pad(shifted.getUTCDate()) + 'T' +
             pad(shifted.getUTCHours()) + ':' +
             pad(shifted.getUTCMinutes()) + ':' +
             pad(shifted.getUTCSeconds()) +
             sign + pad(Math.abs(h)) + ':' + pad(m);
    } catch (_) { return ''; }
  }
  function formatDay(ms) {
    if (!ms) return '—';
    try {
      var d = _shiftedDate(ms);
      return d.toLocaleDateString(undefined, _tzOpts());
    } catch (_) { return '—'; }
  }
  function formatDayShort(ms) {
    if (!ms) return '';
    try {
      var d = _shiftedDate(ms);
      var opts = _tzOpts();
      // Use UTC getters when we shifted manually
      var mon = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
      if (opts.timeZone === 'UTC') {
        return mon[d.getUTCMonth()] + ' ' + d.getUTCDate();
      }
      return mon[d.getMonth()] + ' ' + d.getDate();
    } catch (_) { return ''; }
  }
  function formatDuration(ms) {
    var s = Math.floor(ms / 1000);
    var m = Math.floor(s / 60); s %= 60;
    var hPart = Math.floor(m / 60); m %= 60;
    var pad = function (x) { return (x < 10 ? '0' : '') + x; };
    return (hPart ? hPart + ':' : '') + pad(m) + ':' + pad(s);
  }
  function debounce(fn, ms) {
    var t; return function () {
      var a = arguments, c = this;
      clearTimeout(t);
      t = setTimeout(function () { fn.apply(c, a); }, ms);
    };
  }

  // ===== Boot ===============================================
  function boot() {
    S.M = window.__MANIFEST;
    if (!S.M) {
      document.getElementById('overlaySub').textContent =
        'manifest.js failed to load.';
      return;
    }
    var sub = document.getElementById('overlaySub');
    sub.textContent = 'Hydrating ' + (S.M.totals.rows || 0) + ' rows…';

    // Run heavy steps in setTimeout chunks so the spinner gets paint
    setTimeout(function () {
      hydrate();
      sub.textContent = 'Building bitsets…';
      setTimeout(function () {
        buildBitmaps();
        sub.textContent = 'Building UI…';
        setTimeout(function () {
          renderCaseInfo();
          bindFacetClicks();
          bindTopbar();
          bindDetailClose();
          setupVirtualList();
          setupHistogram();
          applyAndRender();
          window.addEventListener('resize', function () {
            renderHistogram(); renderListWindow();
          });
          var ovl = document.getElementById('loadOverlay');
          ovl.style.opacity = '0';
          setTimeout(function () { ovl.style.display = 'none'; }, 200);
        }, 0);
      }, 0);
    }, 0);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
