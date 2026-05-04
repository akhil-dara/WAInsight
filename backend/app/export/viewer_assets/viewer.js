/* WAInsight Bundle Viewer — vanilla JS, no dependencies, works from file://
 *
 * Architecture:
 *   - JSONP-style shard loading (fetch() is blocked on file://)
 *   - Custom virtual scroller with anchored prepend + prefix-sum height map
 *   - Hash routing: #/c/<conv_id>[/m/<msg_id>] + #/search?q=...
 *   - Search palette (Cmd/Ctrl+K or '/'): substring scan across loaded shards
 *   - Full message-type rendering parity with the in-app chat_renderer:
 *     text, image, video, gif, voice note, audio, document, sticker, location,
 *     vcard, poll, call (incl. synthesized voice-chats), system events,
 *     quoted replies, reactions, forwarded, edited, revoked, ghost, view-once.
 */
(function () {
  'use strict';

  // ------------------------------------------------------------------
  // Globals populated by generated shards
  // ------------------------------------------------------------------
  window.__MANIFEST = window.__MANIFEST || { conversations: [] };
  var shardCache = {};          // shardKey -> Promise<messages[]>
  var shardResolvers = {};      // shardKey -> resolve function (for JSONP)

  // JSONP callback — each shard file calls this on load
  window.__SHARD = function (shardKey, messages) {
    shardCache[shardKey] = Promise.resolve(messages);
    var r = shardResolvers[shardKey];
    if (r) { r(messages); delete shardResolvers[shardKey]; }
  };

  function loadShard(shardKey, shardPath) {
    if (shardCache[shardKey]) return shardCache[shardKey];
    shardCache[shardKey] = new Promise(function (resolve) {
      shardResolvers[shardKey] = resolve;
      var s = document.createElement('script');
      s.src = shardPath;
      s.onerror = function () { resolve([]); };
      document.head.appendChild(s);
    });
    return shardCache[shardKey];
  }

  // ------------------------------------------------------------------
  // Utility: HTML escape, linkify, date formatting
  // ------------------------------------------------------------------
  function esc(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function linkify(s) {
    return s.replace(/https?:\/\/[^\s<]+/g,
      function (u) { return '<a href="' + u + '" target="_blank" rel="noopener">' + u + '</a>'; });
  }
  function waMd(s) {
    // Very lean WhatsApp-style markdown: *bold*, _italic_, ~strike~, ```code```.
    return s
      .replace(/```([^`]+)```/g, '<code>$1</code>')
      .replace(/\*([^*\n]+)\*/g, '<b>$1</b>')
      .replace(/_([^_\n]+)_/g, '<i>$1</i>')
      .replace(/~([^~\n]+)~/g, '<s>$1</s>');
  }
  // International phone formatting — module-scope so mentions, receipts,
  // sender headers all format identically.  Returns "+CC AAAAA BBBBB"
  // for known country layouts; falls back to "+<digits>".
  function fmtPhone(digits) {
    if (!digits) return '';
    var d = String(digits).replace(/[^0-9]/g, '');
    if (!d) return '';
    if (d.length === 12 && d.charAt(0) === '9' && d.charAt(1) === '1') {
      return '+91 ' + d.slice(2, 7) + ' ' + d.slice(7);
    }
    if (d.length === 11 && d.charAt(0) === '1') {
      return '+1 ' + d.slice(1, 4) + ' ' + d.slice(4, 7) + ' ' + d.slice(7);
    }
    if (d.length === 11 && d.charAt(0) === '4' && d.charAt(1) === '4') {
      return '+44 ' + d.slice(2, 6) + ' ' + d.slice(6);
    }
    if (d.length === 12 && d.charAt(0) === '9' && d.charAt(1) === '7' && d.charAt(2) === '1') {
      return '+971 ' + d.slice(3, 5) + ' ' + d.slice(5, 8) + ' ' + d.slice(8);
    }
    if (d.length === 12 && d.charAt(0) === '9' && d.charAt(1) === '6' && d.charAt(2) === '6') {
      return '+966 ' + d.slice(3, 5) + ' ' + d.slice(5, 8) + ' ' + d.slice(8);
    }
    return '+' + d;
  }

  function proc(raw, mentions) {
    if (!raw) return '';
    raw = raw.replace(/^[\s\u00A0]+|[\s\u00A0]+$/g, '');
    if (!raw) return '';
    var html = linkify(waMd(esc(raw)));
    if (mentions && mentions.length) {
      // Replace @<10-15 digit number> with @Name (JID) — WhatsApp puts phone or
      // @lid identifier in the text at the mention point.
      html = html.replace(/@(\d{8,16})/g, function (all, digits) {
        for (var i = 0; i < mentions.length; i++) {
          var mm = mentions[i];
          if (!mm) continue;
          var pn = (mm.phone || '').replace(/[^\d]/g, '');
          var jid = (mm.jid || '').replace(/[^\d]/g, '');
          var lid = (mm.lid || '').replace(/[^\d]/g, '');
          // Bot mentions are written by mention_ingester as
          // "DisplayName|<botnum>", so the bot's numeric id can be matched
          // back to a friendly name.  Strip the "|<botnum>" suffix
          // before display so investigators don't see "@Meta AI
          // (867051314767696)" with a leaked internal id.
          var rawName = mm.name || '';
          var pipeIdx = rawName.indexOf('|');
          var botNum = pipeIdx >= 0 ? rawName.slice(pipeIdx + 1) : '';
          var displayName = pipeIdx >= 0 ? rawName.slice(0, pipeIdx) : rawName;
          if (pn === digits || jid === digits || lid === digits || botNum === digits) {
            var isBot = pipeIdx >= 0;
            var tagJid = isBot ? '' : (mm.jid || mm.lid || (pn ? (pn + '@s.whatsapp.net') : ''));
            // Forensic-friendly: ALWAYS show the real phone in the
            // visible "(...)"; only fall back to whatever digits were
            // in the source text (a LID id) when no phone exists.
            // Tooltip carries the LID + JID for full provenance.
            var displayDigits = pn || digits;
            var formattedPhone = pn ? fmtPhone(pn) : displayDigits;
            var tooltip = (displayName || 'Unknown') + (tagJid ? ' \u2022 ' + tagJid : '');
            if (lid && lid !== pn) {
              tooltip += ' \u2022 LID ' + lid;
            }
            return '<span class="mention-tag' + (isBot ? ' bot' : '') + '" '
                 + 'title="' + esc(tooltip) + '">@'
                 + esc(displayName || displayDigits)
                 + (isBot ? '' : (formattedPhone ? ' <span class="mj">(' + esc(formattedPhone) + ')</span>' : ''))
                 + '</span>';
          }
        }
        // No match — keep raw
        return all;
      });
    }
    return html;
  }
  function fmtTime(ts) {
    if (!ts) return '';
    var d = new Date(ts);
    var h = d.getHours(), m = d.getMinutes();
    return (h < 10 ? '0' : '') + h + ':' + (m < 10 ? '0' : '') + m;
  }
  function fmtDate(ts) {
    if (!ts) return '';
    var d = new Date(ts);
    var M = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    return d.getDate() + ' ' + M[d.getMonth()] + ' ' + d.getFullYear();
  }
  function fmtRelTime(ts) {
    if (!ts) return '';
    var now = Date.now();
    var diff = now - ts;
    if (diff < 60000) return 'now';
    if (diff < 3600000) return Math.floor(diff / 60000) + 'm';
    if (diff < 86400000) return Math.floor(diff / 3600000) + 'h';
    var d = new Date(ts), today = new Date();
    if (d.toDateString() === new Date(today - 86400000).toDateString()) return 'Yesterday';
    if (now - ts < 7 * 86400000) return ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'][d.getDay()];
    return fmtDate(ts);
  }
  function fmtDuration(secs) {
    if (!secs || secs < 0) return '';
    var m = Math.floor(secs / 60), s = Math.floor(secs % 60);
    return m + ':' + (s < 10 ? '0' : '') + s;
  }
  function fmtSize(b) {
    if (!b) return '';
    if (b < 1024) return b + ' B';
    if (b < 1048576) return (b / 1024).toFixed(1) + ' KB';
    if (b < 1073741824) return (b / 1048576).toFixed(1) + ' MB';
    return (b / 1073741824).toFixed(2) + ' GB';
  }
  // Convert msgstore.message_media.file_hash (base64-encoded SHA-256)
  // to lowercase hex digest.  WhatsApp stores the 32-byte hash as
  // base64; analysts often need the hex form for VirusTotal /
  // hashlookup / timeline tools.  Same string, two encodings.
  function b64ToHexLower(b64) {
    if (!b64) return '';
    try {
      var raw = atob(b64);
      var hex = '';
      for (var i = 0; i < raw.length; i++) {
        var h = raw.charCodeAt(i).toString(16);
        if (h.length < 2) h = '0' + h;
        hex += h;
      }
      return hex;
    } catch (e) {
      return '';
    }
  }
  function hashColor(s) {
    if (!s) return '#008069';
    var h = 0;
    for (var i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) & 0xffffffff;
    var palette = [
      '#E17076','#EE7AAE','#6EC9CB','#7D9FDD','#65AADD','#E8828D','#FAA774','#B05C91',
      '#4EAEE6','#2E9D6F','#8F6BC4','#2EA884','#DC8578','#6E8BEF','#FFA552','#0BA09C','#9C84B6',
    ];
    return palette[Math.abs(h) % palette.length];
  }

  // ------------------------------------------------------------------
  // Conversation sidebar
  // ------------------------------------------------------------------
  var _activeConvId = null;
  // Lookup: convId → conv object from the manifest.  Built lazily on
  // first access so renderers don't have to scan the conversations
  // array on every paint.  The call-origin pill checks this to know
  // whether the call's source group/multi-person chat is actually
  // included in this export bundle (so it can offer a clickable jump
  // vs. show an informational "not included in this export" pill).
  var _convsById = null;
  function _buildConvIndex() {
    if (_convsById !== null) return _convsById;
    _convsById = {};
    (window.__MANIFEST && window.__MANIFEST.conversations || [])
      .forEach(function (c) { _convsById[c.id] = c; });
    return _convsById;
  }

  function renderSidebar(filter) {
    var list = document.getElementById('convList');
    list.innerHTML = '';
    var f = (filter || '').trim().toLowerCase();
    var convs = window.__MANIFEST.conversations.slice();
    convs.sort(function (a, b) { return (b.lastMessageAt || 0) - (a.lastMessageAt || 0); });

    convs.forEach(function (conv) {
      if (f && conv.title.toLowerCase().indexOf(f) < 0) return;
      var item = document.createElement('div');
      item.className = 'conv-item' + (conv.id === _activeConvId ? ' active' : '');
      var initial = (conv.title || '?').charAt(0).toUpperCase();
      var ctype = conv.type || 'personal';
      var typeBadge = ctype !== 'personal' ?
        '<span class="conv-type-badge">' + esc(ctype) + '</span>' : '';
      var avatarInner = conv.avatar ?
        '<img src="' + esc(conv.avatar) + '" alt="">' :
        esc(initial);
      item.innerHTML =
        '<div class="conv-avatar" style="background:' + hashColor(conv.title) + '">' +
          avatarInner + '</div>' +
        '<div class="conv-body">' +
          '<div class="conv-row1">' +
            '<span class="conv-name">' + esc(conv.title) + '</span>' +
            '<span class="conv-ts">' + fmtRelTime(conv.lastMessageAt) + '</span>' +
          '</div>' +
          '<div class="conv-sub">' + typeBadge +
            esc(conv.messageCount || 0) + ' messages' +
          '</div>' +
          (conv.participantCount > 2 ?
            '<div class="conv-meta">' + conv.participantCount + ' participants</div>' : '') +
        '</div>';
      item.addEventListener('click', function () {
        location.hash = '#/c/' + conv.id;
      });
      list.appendChild(item);
    });
  }

  // ------------------------------------------------------------------
  // Chat view — virtual scroller + message renderer
  // ------------------------------------------------------------------
  var _chat = {
    conv: null,
    messages: [],         // all loaded messages for current conversation
    heights: [],          // measured height per message
    prefixSum: [],        // cumulative heights for O(log n) offset lookup
    defaultH: 60,
    renderedFirst: -1,
    renderedLast: -1,
    rafId: 0,
  };

  function markUserScroll(blockMs) {
    var now = performance.now();
    _chat.lastUserScrollAt = now;
    _chat.userScrollBlockUntil = Math.max(
      _chat.userScrollBlockUntil || 0,
      now + (blockMs || 900)
    );
    _chat.hasUserScrolled = true;
  }

  function isUserScrollActive(now) {
    now = now || performance.now();
    return !!((_chat.lastUserScrollAt && (now - _chat.lastUserScrollAt) < 250) ||
              (_chat.userScrollBlockUntil && now < _chat.userScrollBlockUntil));
  }

  function scheduleRenderVisible() {
    if (_chat.suppressRenderVisible || _chat.rafId) return;
    _chat.rafId = requestAnimationFrame(function () {
      _chat.rafId = 0;
      if (_chat.suppressRenderVisible) return;
      renderVisible();
    });
  }

  function estimateHeight(msg) {
    // CRITICAL: this function must UNDERESTIMATE real bubble heights.
    //
    // The virtual-scroll prefixSum is computed from these estimates
    // until each bubble is actually rendered + measured.  When a
    // bubble's real height is SMALLER than the estimate, the prefix
    // sum SHRINKS as scroll reveals the bubble — which collapses
    // scrollContent.style.height, and the browser clamps scrollTop to
    // fit, producing a visible "jump" (the instability the user
    // reports as "scroll is not stable, try going to the last and
    // scrolling up").
    //
    // Validated via PerformanceObserver scroll instrumentation:
    // 40 scroll steps shrunk scrollHeight by 859px across 7 separate
    // shrink events — each one a user-visible jump.
    //
    // By keeping estimates intentionally LOW, every real measurement
    // either keeps prefixSum the same or GROWS it.  scrollContent.h
    // becomes monotonic-non-decreasing, scrollTop is never clamped,
    // no jumps.  Trade-off: initial scrollbar shows slightly less
    // total content than reality — barely noticeable; smoothness wins.
    if (!msg) return _chat.defaultH;
    if (msg.system) return 28;            // typical 32-42 in practice
    var t = msg.type || 'text';
    if (t === 'image' || t === 'video' || t === 'gif') return 180;   // real ~220-300
    if (t === 'sticker') return 140;      // real ~160-220
    if (t === 'voice' || t === 'ptt') return 60;   // real ~75-90
    if (t === 'audio') return 55;
    if (t === 'document') return 60;
    if (t === 'location' || t === 'live_location') return 80;
    if (t === 'poll') return 90;          // real ~140+ when many opts
    if (t === 'call' || t === 'voice_chat') return 70;
    if (t === 'vcard') return 60;
    if (t === 'view_once_image' || t === 'view_once_video' || t === 'view_once_voice') return 70;
    if (t === 'scheduled_event' || t === 'event') return 80;
    if (t === 'revoked' || t === 'ghost') return 50;
    var text = msg.text || '';
    var lines = 1 + Math.floor(text.length / 60);
    return 36 + lines * 16;               // was 46 + lines*18 — now slightly under real
  }

  function rebuildHeights() {
    _chat.heights = _chat.messages.map(estimateHeight);
    // Track which indices have been MEASURED from real DOM (vs still
    // an estimate).  Once an index is measured we lock its height — see
    // renderVisible's height-update block.  This is the key to stable
    // scrolling: without the lock, every re-render of the visible
    // window re-measures heights with sub-pixel jitter, prefixSum
    // shifts, and the re-anchor logic tugs scrollTop, producing
    // "scrollbar drag doesn't track" and "can't scroll up from bottom"
    // jank.
    _chat.measured = new Array(_chat.messages.length);
    _chat.prefixSum = new Array(_chat.messages.length + 1);
    _chat.prefixSum[0] = 0;
    for (var i = 0; i < _chat.messages.length; i++) {
      _chat.prefixSum[i + 1] = _chat.prefixSum[i] + _chat.heights[i];
    }
    var initialH = (_chat.prefixSum[_chat.prefixSum.length - 1] || 0);
    _chat.maxScrollH = initialH;
    document.getElementById('scrollContent').style.height = initialH + 'px';
  }

  function findItemAt(offset) {
    var lo = 0, hi = _chat.prefixSum.length - 1;
    while (lo < hi - 1) {
      var mid = (lo + hi) >> 1;
      if (_chat.prefixSum[mid] <= offset) lo = mid; else hi = mid;
    }
    return lo;
  }

  // ─── Scroll diagnostics ───────────────────────────────────────────
  // Exposes _chat on window so devtools can inspect the virtualiser
  // state.  Toggle ``window.__scrollDebug = true`` to enable per-tick
  // console logs.  Call ``window.__scrollValidate()`` at any time for
  // a one-shot consistency check (returns {ok, problems}).
  // Use a getter so devtools / __scrollValidate always see the CURRENT
  // _chat object - openConversation reassigns the IIFE variable on
  // every conv switch, but a plain "window._chat = _chat" only captures
  // the initial reference and goes stale.
  Object.defineProperty(window, '_chat', {
    get: function () { return _chat; },
    configurable: true,
  });
  window.__scrollDebug = false;
  window.__scrollValidate = function () {
    var problems = [];
    var area = document.getElementById('chatArea');
    var container = document.getElementById('messages');
    var sc = document.getElementById('scrollContent');
    if (!area || !container || !sc) {
      return { ok: false, problems: ['DOM nodes missing'] };
    }
    if (!_chat.messages.length) {
      return { ok: true, problems: [], note: 'No conversation loaded' };
    }
    var st = area.scrollTop, vh = area.clientHeight;
    var rf = _chat.renderedFirst, rl = _chat.renderedLast;
    var psum = _chat.prefixSum;
    var n = _chat.messages.length;

    // 1. prefixSum sanity
    if (!psum || psum.length !== n + 1) {
      problems.push('prefixSum length=' + (psum && psum.length) + ' but messages=' + n);
    } else {
      if (psum[0] !== 0) problems.push('prefixSum[0] != 0 (got ' + psum[0] + ')');
      for (var i = 1; i < psum.length; i++) {
        if (!isFinite(psum[i]) || psum[i] < psum[i - 1]) {
          problems.push('prefixSum non-monotonic at i=' + i +
            ' (' + psum[i - 1] + ' -> ' + psum[i] + ')');
          break;
        }
      }
    }

    // 2. scrollContent height >= last prefixSum
    var totalH = psum[n] || 0;
    if (sc.offsetHeight + 1 < totalH) {
      problems.push('scrollContent.h(' + sc.offsetHeight +
        ') < prefixSum[n](' + totalH + ')');
    }

    // 3. Rendered window covers the viewport.  Clamp the "expected"
    // visible region to [0, totalH] - the chatArea has padding above
    // and below the content, so scrollTop+vh can legitimately overshoot
    // totalH by a few px without it being a missing-tile bug.
    if (rf < 0 || rl < 0) {
      problems.push('No window rendered yet (rf=' + rf + ', rl=' + rl + ')');
    } else {
      var winStart = psum[rf] || 0;
      var winEnd = psum[rl + 1] || totalH;
      var visibleStart = Math.max(0, st);
      var visibleEnd = Math.min(totalH, st + vh);
      if (winStart > visibleStart + 1) {
        problems.push('Rendered window starts at y=' + winStart +
          ' but viewport top=' + visibleStart +
          ' (gap above viewport — missing tiles!)');
      }
      if (winEnd < visibleEnd - 1) {
        problems.push('Rendered window ends at y=' + winEnd +
          ' but viewport bottom (clamped to content)=' + visibleEnd +
          ' (gap below viewport — missing tiles!)');
      }
    }

    // 4. container.transform matches prefixSum[rf]
    var tform = container.style.transform || '';
    var m = tform.match(/translateY\(([-\d.]+)px\)/);
    var tformY = m ? parseFloat(m[1]) : 0;
    var expectedY = (rf >= 0 && psum[rf]) || 0;
    if (Math.abs(tformY - expectedY) > 0.5) {
      problems.push('container.transform(' + tformY +
        ') != prefixSum[renderedFirst](' + expectedY + ')');
    }

    // 5. Rendered DOM count matches window size
    var domRows = container.querySelectorAll('.msg').length;
    var expectedRows = rf >= 0 && rl >= 0 ? (rl - rf + 1) : 0;
    if (domRows < expectedRows - 2 || domRows > expectedRows + 4) {
      problems.push('DOM .msg rows=' + domRows + ' but window=' + expectedRows);
    }

    return {
      ok: problems.length === 0,
      problems: problems,
      state: {
        scrollTop: st, scrollHeight: area.scrollHeight, clientHeight: vh,
        renderedFirst: rf, renderedLast: rl,
        prefixSum_at_rf: psum[rf], prefixSum_at_rl_plus_1: psum[rl + 1],
        scrollContentH: sc.offsetHeight, totalH: totalH,
        transform: tform, msgCount: n, domRowCount: domRows,
      }
    };
  };

  function renderVisible(force) {
    var area = document.getElementById('chatArea');
    var st = area.scrollTop, vh = area.clientHeight;
    if (!_chat.messages.length) return;

    // Buffer ±10 msgs (was ±6).  Bigger buffer absorbs hard wheel
    // flicks (no blank tile flash) but doesn't bloat each render too
    // much - ±20 ran 28 ms/tick on a 1400-msg chat (jank threshold);
    // ±10 runs ~16 ms (smooth 60fps).
    var first = Math.max(0, findItemAt(st) - 10);
    var last = Math.min(_chat.messages.length - 1, findItemAt(st + vh) + 10);
    if (!force && first === _chat.renderedFirst && last === _chat.renderedLast) {
      if (window.__scrollDebug) {
        console.log('[scroll] renderVisible SHORT-CIRCUIT', {
          scrollTop: st, first: first, last: last,
          transform: document.getElementById('messages').style.transform
        });
      }
      return;
    }
    _chat.renderedFirst = first; _chat.renderedLast = last;

    var container = document.getElementById('messages');
    container.style.transform = 'translateY(' + (_chat.prefixSum[first] || 0) + 'px)';

    if (window.__scrollDebug) {
      console.log('[scroll] renderVisible', {
        scrollTop: st, vh: vh,
        first: first, last: last,
        prefixAtFirst: _chat.prefixSum[first],
        prefixAtLastPlus1: _chat.prefixSum[last + 1],
        scrollContentH: _chat.maxScrollH,
        anchorIdx: findItemAt(st),
      });
    }

    // NOTE: an incremental DOM update was prototyped here but caused
    // long deadlocks during rapid wheel scroll on the live preview
    // (Chrome's HTMLCollection mutation behaviour during back-to-back
    // removeChild/insertBefore inside a single rAF).  Reverted to
    // full innerHTML rewrite — slower per frame but reliable.  The
    // once-and-lock heights + skipReanchor + suppressRenderVisible
    // changes still meaningfully improve the long-frame distribution.
    // ── Per-message HTML cache ──────────────────────────────────────
    // renderMessage is fairly heavy (receipts, quotes, mentions, the
    // forensic info button, etc.) and the same msg gets re-rendered
    // every time the visible window shifts — multiple times per second
    // during fast scroll.  Memoising the string by msg.id collapses the
    // cost to one allocation per msg per session.  data-idx still has
    // to reflect the CURRENT idx so we cache without it and patch the
    // attribute once the chunk lands in the DOM.
    // ── DOM Element cache (was string cache) ──────────────────────
    // Caching the rendered DOM Element instead of the HTML string means
    // parsing happens ONCE per msg per session.  Subsequent renders
    // reuse the same Element via replaceChildren, which is essentially
    // a pointer swap — no re-parse, no image reload, no layout recalc
    // for unchanged tiles.  This collapses scroll-up jank in heavy chats
    // from multi-frame stalls down to near-frame-budget.
    if (!_chat.elCache) _chat.elCache = {};
    if (!_chat.byId) {
      _chat.byId = {};
      _chat.byKey = {};
      for (var bi = 0; bi < _chat.messages.length; bi++) {
        var bm = _chat.messages[bi];
        _chat.byId[bm.id] = bi;
        if (bm.keyId) _chat.byKey[bm.keyId] = bm.id;
      }
    }
    var children = [];
    var prevDate = '';
    var dateSepTpl = document.createElement('template');
    for (var i = first; i <= last; i++) {
      var m = _chat.messages[i];
      if (!m) continue;
      var d = m.ts ? new Date(m.ts).toDateString() : '';
      if (d && d !== prevDate && i > 0) {
        // Date separators are unique per (date, position) so a fresh
        // node each time is fine - they're cheap.
        dateSepTpl.innerHTML =
          '<div class="date-sep"><span>' + esc(fmtDate(m.ts)) + '</span></div>';
        children.push(dateSepTpl.content.firstElementChild);
      }
      prevDate = d;
      var cachedEl = _chat.elCache[m.id];
      if (!cachedEl) {
        var tplEl = document.createElement('template');
        tplEl.innerHTML = renderMessage(m, i);
        cachedEl = tplEl.content.firstElementChild;
        _chat.elCache[m.id] = cachedEl;
      }
      children.push(cachedEl);
    }
    // replaceChildren keeps already-attached nodes in place if their
    // index hasn't changed - effectively a smart diff for the common
    // "scroll moved one tile" case.  Massively cheaper than innerHTML.
    container.replaceChildren.apply(container, children);

    // ── DEFERRED height measurement ─────────────────────────────────
    // Reading offsetHeight forces a synchronous layout flush.  16 of
    // those per scroll tick costs 5-15 ms — the residual 16% long-frame
    // issue.  Defer measurement to requestIdleCallback so the scroll
    // path stays cheap; once the user pauses, measurements catch up
    // and prefix-sum corrections happen out-of-band.
    var anchorIdx = findItemAt(st);
    var anchorBefore = _chat.prefixSum[anchorIdx];
    var anchorOffset = st - anchorBefore;

    if (!_chat._measureScheduled) {
      _chat._measureScheduled = true;
      var doMeasure = function () {
        _chat._measureScheduled = false;
        // Skip the whole measurement pass if the user is still actively
        // scrolling - reading offsetHeight on 20+ rows forces a synchronous
        // layout flush which steals frame budget mid-scroll, producing a
        // sticky scroll feel.  Once scroll settles we'll run again on the
        // next idle.
        if (isUserScrollActive()) {
          // Re-arm a follow-up so heights eventually catch up.
          _chat._measureScheduled = true;
          if (window.requestIdleCallback) {
            window.requestIdleCallback(doMeasure, { timeout: 250 });
          } else {
            setTimeout(doMeasure, 200);
          }
          return;
        }
        var dirty = false;
        var firstChanged = -1;
        var rows = container.querySelectorAll('[data-msg]');
        for (var r = 0; r < rows.length; r++) {
          var msgId = rows[r].getAttribute('data-msg');
          var idx = _chat.byId ? _chat.byId[msgId] : -1;
          if (idx === undefined || idx < 0) continue;
          if (_chat.measured && _chat.measured[idx]) continue;
          var h = rows[r].offsetHeight;
          if (h && Math.abs(h - _chat.heights[idx]) > 6) {
            _chat.heights[idx] = h;
            if (firstChanged < 0 || idx < firstChanged) firstChanged = idx;
            dirty = true;
          }
          if (h) {
            if (!_chat.measured) _chat.measured = [];
            _chat.measured[idx] = true;
          }
        }
        if (!dirty) return;
        var startK = firstChanged > 0 ? firstChanged : 1;
        for (var k = startK; k < _chat.prefixSum.length; k++) {
          _chat.prefixSum[k] = _chat.prefixSum[k - 1] + _chat.heights[k - 1];
        }
        var totalH = _chat.prefixSum[_chat.prefixSum.length - 1];
        _chat.maxScrollH = Math.max(_chat.maxScrollH || 0, totalH);
        document.getElementById('scrollContent').style.height = _chat.maxScrollH + 'px';
        var area2 = document.getElementById('chatArea');

        // CRITICAL FIX (missing-tile bug): the prefix sum just shifted
        // for indices >= firstChanged.  If our currently-rendered window
        // overlaps that range, the messages container's translateY is
        // now STALE - it points at the OLD absolute Y for renderedFirst.
        // Result: tiles render at the old offset, but scrollTop now
        // expects them at the new offset, leaving a visible blank zone
        // in the middle of the viewport (this is the "missing tiles
        // when loading" bug).  Re-pin the transform to the FRESH
        // prefixSum[renderedFirst] before any further scroll math.
        if (_chat.renderedFirst >= 0
            && firstChanged >= 0
            && firstChanged <= _chat.renderedLast) {
          container.style.transform =
            'translateY(' + (_chat.prefixSum[_chat.renderedFirst] || 0) + 'px)';
        }

        // Normal user scroll wins. Height corrections can grow the
        // virtual document, but they must not push scrollTop back down
        // after PageUp/wheel input. Only keep the newest messages pinned
        // while the initial bottom landing is settling.
        var canAutoPinBottom = !_chat.hasUserScrolled
          && _chat.autoStickBottomUntil
          && performance.now() < _chat.autoStickBottomUntil;
        if (canAutoPinBottom) {
          var maxST = Math.max(0, _chat.maxScrollH - area2.clientHeight);
          if (Math.abs(maxST - area2.scrollTop) > 0.5) {
            _chat.expectedScrollTop = maxST;
            area2.scrollTop = maxST;
          }
        }

        // After the re-anchor (or even when the window indices are the
        // same but the prefix sums shifted), the rendered DOM needs a
        // fresh draw so the transform + tile content match the new
        // scrollTop.  Force a re-render unconditionally - the renderer
        // is cheap (per-msg HTML cache) and the alternative is the
        // missing-tile bug.
        renderVisible(true);

        if (window.__scrollDebug) {
          console.log('[scroll] doMeasure', {
            firstChanged: firstChanged,
            renderedFirst: _chat.renderedFirst,
            renderedLast: _chat.renderedLast,
            prefixSumAtFirst: _chat.prefixSum[_chat.renderedFirst],
            transform: container.style.transform,
            scrollTop: area2.scrollTop,
            scrollContentH: _chat.maxScrollH,
          });
        }
      };
      if (window.requestIdleCallback) {
        window.requestIdleCallback(doMeasure, { timeout: 80 });
      } else {
        setTimeout(doMeasure, 60);
      }
    }
    var dirty = false;
    var firstChanged = -1;
    if (dirty) {
      // INCREMENTAL prefix-sum update — only rebuild from the smallest
      // changed index forward.  Rebuilding the full array on every height
      // delta was the worst-case stutter (94 ms frames for a 24K-msg
      // chat).  Now we touch maybe 50-200 entries when scrolling deep
      // into the conversation.
      var startK = firstChanged > 0 ? firstChanged : 1;
      for (var k = startK; k < _chat.prefixSum.length; k++) {
        _chat.prefixSum[k] = _chat.prefixSum[k - 1] + _chat.heights[k - 1];
      }
      var totalH = _chat.prefixSum[_chat.prefixSum.length - 1];
      // CRITICAL: pin scrollContent.height to MAX-ever-seen.  When real
      // bubble measurements come in smaller than the estimates that
      // were used previously, totalH shrinks — that collapses
      // scrollContent and the browser clamps scrollTop downward.  The
      // user feels this as a "scroll jump" mid-scroll (the
      // "scroll is unstable, try going to last and scroll up" bug).
      // Pinning means scrollContent never shrinks during a session;
      // worst case there's a tiny phantom blank below the final bubble
      // until we naturally accumulate measurements that match.
      _chat.maxScrollH = Math.max(_chat.maxScrollH || 0, totalH);
      document.getElementById('scrollContent').style.height = _chat.maxScrollH + 'px';

      // Re-anchor: keep the visually-anchored item under the same
      // viewport offset.  TWO guards prevent this from stealing user
      // scroll input:
      //   1. Only re-anchor when the visible position would shift by
      //      more than 4 px — sub-pixel re-anchors aren't worth the
      //      scroll-input fight they cause.
      //   2. Clamp the new scrollTop to the document edge so the
      //      re-anchor cannot push the user past the bottom (or pull
      //      them off the top), which used to make scrolling up from
      //      the bottom feel impossible.
      var anchorAfter = _chat.prefixSum[anchorIdx];
      var delta = anchorAfter - anchorBefore;
      // Skip re-anchor when an explicit scrollTo* is in flight — the
      // caller will issue its own follow-up renderVisible after the
      // heights have settled, and re-anchoring during a deliberate
      // jump pulls scrollTop back to its old position (the
      // "scrollToMessageId lands off-screen" bug).
      if (_chat.allowScrollReanchor && Math.abs(delta) > 4 && !_chat.skipReanchor) {
        var maxST = Math.max(0, totalH - area.clientHeight);
        var newST = Math.max(0, Math.min(maxST, anchorAfter + anchorOffset));
        if (Math.abs(newST - area.scrollTop) > 0.5) {
          _chat.expectedScrollTop = newST;
          area.scrollTop = newST;
        }
      }
    }

    updateStickyDate();
  }

  function updateStickyDate() {
    var el = document.getElementById('stickyDate');
    if (!el) return;
    var area = document.getElementById('chatArea');
    var first = findItemAt(area.scrollTop);
    var m = _chat.messages[first];
    if (!m || !m.ts) { el.classList.remove('visible'); return; }
    el.textContent = fmtDate(m.ts);
    el.classList.add('visible');
    clearTimeout(el._t);
    el._t = setTimeout(function () { el.classList.remove('visible'); }, 1500);
  }

  // ------------------------------------------------------------------
  // Message rendering (per-type)
  // ------------------------------------------------------------------
  function renderMessage(m, idx) {
    // Compaction marker \u2014 synthetic row inserted by the bundle
    // exporter when a tagged-message export drops surrounding
    // messages.  Designed to be VISUALLY DISTINCT from a system
    // event: amber palette (vs grey), explicit "EXPORT CONTEXT"
    // header, and a per-kind breakdown so the analyst sees what
    // type of activity was hidden (normal chat / system events /
    // calls) \u2014 never confusable with a real WhatsApp system msg.
    if (m.type === '__compaction__') {
      var n = m.skipped || 0;
      var kinds = [];
      if (m.skipped_normal) kinds.push(m.skipped_normal.toLocaleString() + ' chat');
      if (m.skipped_system) kinds.push(m.skipped_system.toLocaleString() + ' system event' + (m.skipped_system === 1 ? '' : 's'));
      if (m.skipped_call)   kinds.push(m.skipped_call.toLocaleString()   + ' call' + (m.skipped_call === 1 ? '' : 's'));
      var kindStr = kinds.length ? kinds.join(' \u00b7 ') : (n.toLocaleString() + ' messages');
      var rangeBits = [];
      if (m.skipped_from) rangeBits.push(fmtDate(m.skipped_from) + ' ' + fmtTime(m.skipped_from));
      if (m.skipped_to && m.skipped_to !== m.skipped_from)
        rangeBits.push(fmtDate(m.skipped_to) + ' ' + fmtTime(m.skipped_to));
      var rangeStr = rangeBits.length ? rangeBits.join(' \u2014 ') : '';
      return '<div class="msg compaction-marker" data-idx="' + idx + '" data-msg="' + m.id + '">' +
        '<div class="compaction-card">' +
          '<div class="compaction-header">\u25bc EXPORT CONTEXT \u25bc</div>' +
          '<div class="compaction-text">' +
            '<strong>' + n.toLocaleString() + '</strong> ' +
            (n === 1 ? 'message' : 'messages') + ' hidden ' +
            '<span class="compaction-kinds">(' + esc(kindStr) + ')</span>' +
            (rangeStr ? '<div class="compaction-range">' + esc(rangeStr) + '</div>' : '') +
          '</div>' +
          '<div class="compaction-foot">not a WhatsApp system message \u2014 inserted by WAInsight to mark a gap in the tagged-message export</div>' +
        '</div></div>';
    }
    // System event
    if (m.system || m.type === 'system') {
      var body = m.system_text || m.text || (m.type_label || 'system');
      var ts = m.ts ? '<div class="system-ts">' + esc(fmtTime(m.ts) + ' \u2022 ' + fmtDate(m.ts)) + '</div>' : '';
      return '<div class="msg system" data-idx="' + idx + '" data-msg="' + m.id + '">' +
        '<div>' +
          '<div class="system-text">' + proc(body) + '</div>' + ts +
        '</div></div>';
    }

    var isSent = !!m.fromMe;
    var cls = 'msg ' + (isSent ? 'sent' : 'received');
    if (m.type === 'sticker') cls += ' sticker-msg';
    if (m.starred) cls += ' starred';

    var bubbleInner = [];

    // Sender name (for group chats, received side only) — always show phone/JID
    // and the group-member nickname when set (e.g. "~ Zankanotachi").
    if (!isSent && _chat.conv && (_chat.conv.type === 'group' || _chat.conv.type === 'community' ||
                                    _chat.conv.type === 'channel')) {
      if (m.senderName) {
        var color = hashColor(m.senderJid || m.senderName);
        var idBit = '';
        if (m.senderPhone) {
          idBit = ' <span class="sender-phone">(' + esc(fmtPhone(m.senderPhone)) + ')</span>';
        } else if (m.senderJid) {
          idBit = ' <span class="sender-phone">(' + esc(m.senderJid) + ')</span>';
        } else if (m.senderLid) {
          idBit = ' <span class="sender-phone">(' + esc(m.senderLid) + ')</span>';
        }
        // Group-member nickname (label inside this conv's group_member
        // table — e.g. "Zankanotachi" for Gupta).  Comes from the
        // exporter as m.senderGroupLabel / m.memberLabel.
        var labelStr = m.senderGroupLabel || m.memberLabel || '';
        var labelBit = labelStr
          ? ' <span class="sender-grouplabel">~ ' + esc(labelStr) + '</span>'
          : '';
        bubbleInner.push('<div class="sender" style="color:' + color + '">' +
          esc(m.senderName) + idBit + labelBit + '</div>');
      }
    }

    // Forwarded badge with hop count
    if (m.forwardScore && m.forwardScore > 0) {
      bubbleInner.push('<div class="fwd">\u21B7 Forwarded' +
        (m.forwardScore >= 5 ? ' many times' : '') +
        '<span class="fwd-score" title="Forward hop count">x' +
        m.forwardScore + '</span></div>');
    }

    // Quoted reply — clickable to jump to parent message.  Resolve
    // parent id with a 3-step fallback: (1) m.replyToMsgId (set by
    // exporter when in-set), (2) m.quoted.parentId (same), (3) lookup
    // via parentKey through the byKey index built at first render.  This
    // last fallback covers ghost-recovered messages where the original
    // msg row is gone but a deletion-announcement row carries the same
    // keyId.
    var qPar = m.replyToMsgId || (m.quoted && m.quoted.parentId);
    if (m.quoted && !qPar && m.quoted.parentKey && _chat.byKey) {
      qPar = _chat.byKey[m.quoted.parentKey];
    }
    if (m.quoted) {
      bubbleInner.push(renderQuote(m.quoted, qPar));
    }

    // Body by type
    bubbleInner.push(renderBody(m));

    // Link previews
    if (m.linkPreview) {
      bubbleInner.push(renderLink(m.linkPreview));
    }

    // Meta row (time + ticks)
    var metaBits = [];
    // Don't surface the "edited" badge on bot messages.  AI assistants
    // (Meta AI etc.) stream their reply as a series of WhatsApp message
    // edits — every chunk arrives as `edit_count++` on the same key.
    // Treating those streaming chunks as "user intentionally edited
    // their message" is misleading; the investigator only cares about
    // the final response.
    if (m.edited && !m.isBot) metaBits.push('<span class="edited">edited</span>');
    if (m.starred) metaBits.push('<span class="star">\u2605</span>');
    metaBits.push('<span class="time">' + esc(fmtTime(m.ts)) + '</span>');
    if (isSent && m.status) {
      var tickCls = 'ticks';
      var tick = '\u2713';
      if (m.status === 'delivered' || m.status === 'read' || m.status === 'played') tick = '\u2713\u2713';
      if (m.status === 'read' || m.status === 'played') tickCls += ' read';
      metaBits.push('<span class="' + tickCls + '">' + tick + '</span>');
    }
    bubbleInner.push('<div class="meta-row">' + metaBits.join('') + '</div>');

    // Reactions — click to expand detail rows (who reacted, when)
    if (m.reactions && m.reactions.length) {
      var pills = m.reactions.map(function (r) {
        return '<span class="reaction-pill">' + esc(r.emoji) +
          '<span class="r-count">' + (r.count || (r.from && r.from.length) || 1) + '</span></span>';
      }).join('');
      var detailRows = '';
      m.reactions.forEach(function (r) {
        (r.detail || []).forEach(function (d) {
          detailRows += '<div class="reaction-row">' +
            '<span class="r-em">' + esc(r.emoji) + '</span>' +
            '<span class="r-name">' + esc(d.name || 'Unknown') + '</span>' +
            '<span class="r-ts">' + esc(fmtTime(d.ts) || '') + '</span>' +
            '</div>';
        });
      });
      bubbleInner.push('<div class="reactions" data-rx="' + idx + '">' + pills + '</div>' +
        (detailRows ? '<div class="reaction-expand" data-rx-body="' + idx + '">' +
          detailRows + '</div>' : ''));
    }

    // Receipts (outgoing messages only) — collapsible per-recipient detail
    // Forensic correction: WhatsApp's msgstore.receipt_user does NOT
    // always store an explicit "delivered" row when the read event
    // happens soon after delivery (or when the recipient was offline at
    // delivery and the device flushed only the most recent receipt
    // later).  As a result, ``delivered.length`` is often LOWER than
    // ``read.length`` in the raw source — which produces the
    // nonsensical "Delivered to 2, Read by 4" tile.  Anyone who READ
    // the message must have RECEIVED it first, so the displayed
    // delivered count is at least the deduplicated union of
    // delivered ∪ read ∪ played.
    if (m.receipts && ((m.receipts.delivered && m.receipts.delivered.length) ||
                       (m.receipts.read && m.receipts.read.length) ||
                       (m.receipts.played && m.receipts.played.length))) {
      var rcptKey = function (r) { return (r && (r.name || '') + '|' + (r.jid || '')); };
      var deliveredSet = {};
      (m.receipts.delivered || []).forEach(function (r) { deliveredSet[rcptKey(r)] = true; });
      (m.receipts.read      || []).forEach(function (r) { deliveredSet[rcptKey(r)] = true; });
      (m.receipts.played    || []).forEach(function (r) { deliveredSet[rcptKey(r)] = true; });
      var delCnt = 0;
      for (var _rk in deliveredSet) { if (deliveredSet.hasOwnProperty(_rk)) delCnt++; }
      var rawDelCnt = (m.receipts.delivered || []).length;
      var readCnt = (m.receipts.read || []).length;
      var playedCnt = (m.receipts.played || []).length;
      var receiptBits = [];
      if (delCnt) receiptBits.push('\u2713\u2713 Delivered to ' + delCnt);
      if (readCnt) receiptBits.push(
        '<span style="color:var(--tick-read)">\u2713\u2713 Read by ' + readCnt + '</span>');
      if (playedCnt) receiptBits.push('\u25B6 Played by ' + playedCnt);
      // (fmtPhone is now defined at module scope — see top of file.)
      var receiptGroup = function (label, rows) {
        if (!rows || !rows.length) return '';
        return '<div class="receipts-group"><h5>' + label + '</h5>' +
          rows.map(function (r) {
            // Show the full international phone alongside the name.  Saved
            // contacts render as "Display Name  +CC NNNNN NNNNN"; unsaved
            // entries are already prefixed with "~" in the data.
            var phoneFmt = fmtPhone(r.phone);
            var nameHtml = '<span class="rr-name">' + esc(r.name) + '</span>';
            var phoneHtml = phoneFmt
              ? '<span class="rr-phone">' + esc(phoneFmt) + '</span>'
              : '';
            var saved = r.isSaved
              ? ''
              : '<span class="rr-unsaved" title="Number not in saved contacts">unsaved</span>';
            return '<div class="receipt-row">' +
              nameHtml + phoneHtml + saved +
              '<span class="rr-ts">' + esc(fmtDate(r.ts) + ' ' + fmtTime(r.ts)) + '</span>' +
            '</div>';
          }).join('') + '</div>';
      };
      bubbleInner.push(
        '<div class="receipts-bar" data-rcpt="' + idx + '">' +
          receiptBits.join(' &middot; ') + ' &nbsp;&middot; tap for detail</div>' +
        '<div class="receipts-expand" data-rcpt-body="' + idx + '">' +
          receiptGroup('Delivered', m.receipts.delivered) +
          receiptGroup('Read', m.receipts.read) +
          receiptGroup('Played', m.receipts.played) +
        '</div>'
      );
    }

    // Forensic info button — compact "ℹ" that opens the full provenance panel.
    // The click handler resolves the message via its parent .msg's
    // data-msg attribute (msg.id, stable across renders) -> _chat.byId
    // map -> _chat.messages[idx].  No more idx baked into the cached
    // HTML (which would otherwise need a regex-replace every render).
    bubbleInner.push('<span class="msg-info-btn" title="Full forensic detail">i</span>');

    // Avatar — show the WhatsApp DP for any received message, INCLUDING
    // personal chats so business / brand display pictures render in
    // 1:1 chats with verified business accounts.
    // Group rules unchanged: still show per-message sender avatar so
    // the reader can tell speakers apart at a glance.
    var avatarHtml = '';
    if (!isSent) {
      if (m.senderAvatar) {
        avatarHtml = '<img class="avatar avatar-img" src="' + esc(m.senderAvatar) +
          '" alt="' + esc(m.senderName || '') + '">';
      } else if (m.senderName) {
        var cc = hashColor(m.senderJid || m.senderName);
        avatarHtml = '<div class="avatar" style="background:' + cc + '">' +
          esc(m.senderName.charAt(0).toUpperCase()) + '</div>';
      } else {
        avatarHtml = '<div class="avatar-spacer"></div>';
      }
    }

    return '<div class="' + cls + '" data-idx="' + idx + '" data-msg="' + (m.id || '') + '">' +
      avatarHtml +
      '<div class="bubble">' + bubbleInner.join('') + '</div>' +
    '</div>';
  }

  function renderQuote(q, parentMsgId) {
    if (!q) return '';
    var icon = '';
    if (q.type === 'image') icon = '\uD83D\uDCF7 ';
    else if (q.type === 'video') icon = '\uD83C\uDFAC ';
    else if (q.type === 'voice' || q.type === 'ptt') icon = '\uD83C\uDFA4 ';
    else if (q.type === 'audio') icon = '\uD83C\uDFB5 ';
    else if (q.type === 'document') icon = '\uD83D\uDCC4 ';
    else if (q.type === 'location' || q.type === 'live_location') icon = '\uD83D\uDCCD ';
    else if (q.type === 'sticker') icon = '\uD83C\uDF6D ';
    else if (q.type === 'vcard') icon = '\uD83D\uDC64 ';
    var body = '';
    if (q.preview) {
      body = '<span class="q-text"><span class="q-type-icon">' + icon + '</span>' +
             proc(q.preview) + '</span>';
    } else {
      var typeText = q.type === 'image' ? 'Photo' :
                     q.type === 'video' ? 'Video' :
                     q.type === 'voice' || q.type === 'ptt' ? 'Voice message' :
                     q.type === 'audio' ? 'Audio' :
                     q.type === 'document' ? 'Document' :
                     q.type === 'location' ? 'Location' :
                     q.type === 'live_location' ? 'Live location' :
                     q.type === 'sticker' ? 'Sticker' :
                     q.type === 'vcard' ? 'Contact' :
                     '(media)';
      body = '<span class="q-type">' + icon + typeText + '</span>';
    }
    var sender = q.from || q.senderName || '';
    var navAttr = parentMsgId ? ' data-quote-parent="' + parentMsgId + '"' : '';
    return '<div class="quote"' + navAttr + '>' +
      (sender ? '<span class="q-sender">' + esc(sender) + '</span>' : '') +
      body +
    '</div>';
  }

  function dlPill(path, name) {
    if (!path) return '';
    var dl = name ? ' download="' + esc(name) + '"' : ' download';
    return '<a class="dl-pill" href="' + esc(path) + '"' + dl +
           ' title="Download to disk' + (name ? ' as \u201C' + esc(name) + '\u201D' : '') + '">' +
           '\u21E9 <span class="dl-name">' + esc(name || 'Download') + '</span></a>';
  }

  // Render a media block (image/video/thumbnail-fallback) for cases
  // where the message *type* does not itself imply image/video - e.g.
  // business-interactive templates that ship a brand image alongside
  // the list/button payload, or any other case where we want a uniform
  // "show whatever we have" widget.
  //
  // Behaviour:
  //   * path on disk + video mime/extension -> full <video> with poster.
  //   * thumbnail only + video mime/extension -> still image + play
  //     overlay and a "preview only" tag (original file not on disk).
  //   * path on disk + image mime -> full image.
  //   * thumbnail only + image mime -> thumbnail with "preview only" tag.
  //   * nothing renderable -> empty string (caller decides fallback).
  function renderAttachedMedia(media) {
    if (!media) return '';
    var path = media.path || '';
    var thumb = media.thumbnail || '';
    if (!path && !thumb) return '';

    var mime = (media.mime || '').toLowerCase();
    var probe = (path || thumb || '').toLowerCase();
    var isVideo = mime.indexOf('video') >= 0 ||
                  /\.(mp4|webm|mov|3gp|m4v|avi|mkv)(\?|$)/i.test(probe);

    // Video on disk: real <video> element so the user can play it.
    if (isVideo && path) {
      return '<div class="video-container biz-media">' +
        '<video class="media-thumb" src="' + esc(path) + '" controls preload="metadata"' +
          (thumb ? ' poster="' + esc(thumb) + '"' : '') + '></video>' +
      '</div>';
    }

    // Video file missing but the device kept the thumbnail - show the
    // poster image with a play-glyph overlay so the investigator still
    // sees what the recipient saw.
    if (isVideo && thumb) {
      return '<div class="image-container biz-media video-thumb-only">' +
        '<img class="media-thumb" src="' + esc(thumb) +
          '" loading="lazy" decoding="async" alt="video thumbnail">' +
        '<span class="video-play-overlay" aria-hidden="true">▶</span>' +
        '<span class="thumb-only-badge">preview only</span>' +
      '</div>';
    }

    // Image (path on disk or thumbnail-only fallback).
    var src = path || thumb;
    return '<div class="image-container biz-media' +
        (path ? '' : ' thumb-only') + '">' +
      '<img class="media-thumb" src="' + esc(src) +
        '" loading="lazy" decoding="async" alt="image">' +
      (path ? '' : '<span class="thumb-only-badge">preview only</span>') +
    '</div>';
  }

  function renderBody(m) {
    var t = m.type || 'text';
    var media = m.media || {};

    // Business / interactive message templates (types 25/26/27/49/55/62)
    // ALL ship as type='text' from the exporter (because their primary
    // body is the rendered text payload), so this branch MUST run
    // before the generic text branch - otherwise the brand image and
    // template chrome get dropped, and the bubble shows plain text only.
    if (m.typeLabel === 'list_message'   || m.typeLabel === 'list'
     || m.typeLabel === 'button_message' || m.typeLabel === 'cta_button'
     || m.typeLabel === 'carousel'       || m.typeLabel === 'product'
     || m.typeLabel === 'list_reply') {
      var bizIcon = (m.typeLabel === 'list_message' || m.typeLabel === 'list') ? '📋'
                  : (m.typeLabel === 'list_reply')   ? '✅'
                  : (m.typeLabel === 'button_message') ? '🔘'
                  : (m.typeLabel === 'cta_button')   ? '🔗'
                  : (m.typeLabel === 'carousel')     ? '🎠'
                  : (m.typeLabel === 'product')      ? '🏷'
                  : '🛠';
      var bizMedia = renderAttachedMedia(media);
      return '<div class="biz-template' + (bizMedia ? ' biz-with-media' : '') + '">'
        + bizMedia
        + '<div class="biz-header">' + bizIcon + ' '
        +     '<span class="biz-type">'
        +       esc(m.typeLabel.replace(/_/g, ' ')) + '</span>'
        + '</div>'
        + (m.text
            ? '<div class="biz-body">' + proc(m.text, m.mentions) + '</div>'
            : (bizMedia ? ''
              : '<div class="biz-body biz-empty">'
                + '[interactive payload — buttons / list options]</div>'))
        + '</div>';
    }

    // ── Album (multi-photo / multi-video post, message_type=99) ─────
    // Children pre-loaded by exporter into m.album.children with full
    // media metadata each.  Render as a responsive grid; clicking a
    // cell opens the lightbox via the same data-msg/data-attach hook
    // the standalone media tiles use.
    if (t === 'album') {
      var alb = m.album || {};
      var kids = alb.children || [];
      if (!kids.length) {
        return '<div class="media-badge"><span class="icon">🖼</span>Album (no children in case)</div>';
      }
      var n = kids.length;
      // Visible grid cap = 9 cells.  If more, last cell shows +N overlay.
      var visibleMax = 9;
      var cellsToShow = Math.min(n, visibleMax);
      var hiddenCount = Math.max(0, n - visibleMax);
      // Grid columns: 1->1, 2->2, 3->3, 4->2x2, 5-6->3, 7-9->3, 10+->3
      var cols = (n === 1) ? 1
               : (n === 2) ? 2
               : (n === 4) ? 2
               : 3;
      var cells = [];
      for (var i = 0; i < cellsToShow; i++) {
        var c = kids[i];
        var cMedia = c.media || {};
        var cThumb = cMedia.thumbnail || '';
        var cPath = cMedia.path || '';
        var cSrc = cPath || cThumb;
        var ctype = c.type || '';
        var isVid = ctype === 'video' || ctype === 'gif';
        var inner = '';
        if (cSrc) {
          // For the grid we render <img> for both images and videos.
          // The lightbox needs the full file path to play the video, so
          // we set src to the path when available; the in-grid <img> just
          // shows the static frame (browsers happily render the first
          // frame of a video file in an <img> tag for many codecs, and
          // when they can't we fall back to thumbnail).
          var gridSrc = cPath || cThumb;
          inner = '<img class="album-thumb" src="' + esc(cThumb || gridSrc) +
                    '" data-fullsrc="' + esc(gridSrc) + '"' +
                    ' loading="lazy" decoding="async" alt="' +
                    (isVid ? 'video' : 'image') + '">';
          if (isVid) inner += '<span class="album-vid-overlay" aria-hidden="true">▶</span>';
        } else {
          // No file on disk and no thumbnail
          inner = '<span class="album-missing">' +
            (isVid ? '🎬' : '📷') + '<br>(not on disk)</span>';
        }
        // +N more overlay on the last visible cell when there are more
        if (i === cellsToShow - 1 && hiddenCount > 0) {
          inner += '<span class="album-more-overlay">+' + hiddenCount + '</span>';
        }
        cells.push('<div class="album-cell" data-msg="' + c.id +
                   '" data-attach="0" data-album-child="1">' + inner + '</div>');
      }
      // Forensic note (e.g. "expected 7, only 5 present")
      var noteBit = alb.note
        ? '<div class="album-note" title="forensic note">⚠ ' + esc(alb.note) + '</div>'
        : '';
      // Album header counter ("🖼 7 photos · 2 videos")
      var partsHdr = [];
      if (alb.imageCount) partsHdr.push(alb.imageCount + ' photo' + (alb.imageCount === 1 ? '' : 's'));
      if (alb.videoCount) partsHdr.push(alb.videoCount + ' video' + (alb.videoCount === 1 ? '' : 's'));
      var hdr = partsHdr.length
        ? '<div class="album-header">🖼 ' + esc(partsHdr.join(' · ')) + '</div>'
        : '';
      // Caption (from first child's text_content; bubbled by exporter)
      var capText = alb.caption || m.text || '';
      var capBit = capText
        ? '<div class="caption text">' + proc(capText, m.mentions) + '</div>'
        : '';
      return hdr + noteBit +
        '<div class="album-grid" data-album-cols="' + cols + '">' +
          cells.join('') +
        '</div>' + capBit;
    }

    if (t === 'text') {
      return '<div class="text">' + proc(m.text, m.mentions) + '</div>';
    }

    // ── Scheduled event (type: scheduled_event) ─────────────────────
    // Renders an event card with title, start/end time, location +
    // map link, optional join link, and a cancellation badge.  The
    // bundle previously had no branch for this type, so events showed
    // up as plain text "Event join" — the placeholder string in
    // ``message.text_content``.
    if (t === 'scheduled_event' || t === 'event') {
      var ev = m.event || {};
      var name = ev.name || m.text || 'Scheduled event';
      var ts = ev.startTs;
      var endTs = ev.endTs;
      var fmtEvTime = function (msEpoch) {
        if (!msEpoch) return '';
        try { return fmtDate(msEpoch) + ' ' + fmtTime(msEpoch); } catch (e) { return ''; }
      };
      var pieces = [];
      pieces.push('<div class="event-name">' + (ev.isCall ? '📞 ' : '📅 ') + esc(name) + '</div>');
      if (ts) {
        var when = fmtEvTime(ts);
        if (endTs && endTs !== ts) when += ' → ' + fmtEvTime(endTs);
        pieces.push('<div class="event-when">🕐 ' + esc(when) + '</div>');
      }
      var loc = (ev.locationName || ev.locationAddr || '').trim();
      if (loc) {
        pieces.push('<div class="event-loc">📍 ' + esc(loc) + '</div>');
      }
      if (ev.joinLink) {
        pieces.push('<div class="event-link"><a href="' + esc(ev.joinLink) +
          '" target="_blank" rel="noopener">🔗 Join</a></div>');
      }
      if (ev.description) {
        pieces.push('<div class="event-desc">' + proc(ev.description) + '</div>');
      }
      if (ev.isCanceled) {
        pieces.push('<div class="event-canceled">CANCELLED</div>');
      }
      return '<div class="event-card' + (ev.isCanceled ? ' canceled' : '') + '">' +
             pieces.join('') + '</div>';
    }

    // ── View-once media (type: view_once_image / view_once_video / view_once_voice) ──
    // Even when the underlying file isn't on disk (typical for
    // view-once content the device wiped after viewing), the bundle
    // previously fell through to the text fallback and showed nothing.
    // Render a clear "👁 View once" card with the state badge so the
    // investigator sees that a view-once message existed at this
    // position.  When media IS present, we still show the actual
    // image/video below the badge.
    if (t === 'view_once_image' || t === 'view_once_video' || t === 'view_once_voice') {
      var voState = m.viewOnceState;
      // 0=not opened, 1=opened/seen, 2=played (voice/video)
      var stateLabel = voState === 1 ? '👁 Opened'
                     : voState === 2 ? '✅ Played'
                     : voState === 0 ? '🟠 Not opened'
                     : '👁 View once';
      var icon = t === 'view_once_image' ? '📷'
               : t === 'view_once_video' ? '📹'
               : '🎤';
      var label = t === 'view_once_image' ? 'View-once photo'
                : t === 'view_once_video' ? 'View-once video'
                : 'View-once voice note';
      // Duration string for video / voice (image has none)
      var durStr = '';
      if (t !== 'view_once_image' && media.duration) {
        durStr = fmtDuration(media.duration);
      }
      var hasFile = media.path || media.thumbnail;
      var pieces2 = [];
      pieces2.push(
        '<div class="view-once-card">'
        + '<div class="vo-icon">' + icon + '</div>'
        + '<div class="vo-info">'
        +   '<div class="vo-label">' + esc(label)
        +     (durStr ? ' <span class="vo-dur">• ' + esc(durStr) + '</span>' : '')
        +   '</div>'
        +   '<div class="vo-state">' + stateLabel + '</div>'
        +   (hasFile ? '' : '<div class="vo-missing">Media not on disk</div>')
        + '</div>'
        + '</div>'
      );
      // Render image preview when present (still on disk OR thumbnail)
      if (hasFile && t === 'view_once_image') {
        pieces2.push(
          '<div class="image-container vo-media">'
          + '<img src="' + esc(media.path || media.thumbnail) + '" loading="lazy" />'
          + '<span class="view-once-badge">👁 View once</span>'
          + '</div>'
        );
      }
      // Voice / video: render audio player or video poster when on disk
      if (hasFile && t === 'view_once_voice' && media.path) {
        pieces2.push(
          '<div class="audio-card vo-media">'
          + '<span class="play-icon">🎤</span>'
          + '<audio src="' + esc(media.path) + '" controls preload="none" style="flex:1;height:30px"></audio>'
          + (durStr ? '<span class="duration">' + esc(durStr) + '</span>' : '')
          + '</div>'
        );
      } else if (hasFile && t === 'view_once_video' && media.path) {
        pieces2.push(
          '<div class="video-container vo-media">'
          + '<video class="media-thumb" src="' + esc(media.path) + '" controls preload="metadata"'
          + (media.thumbnail ? ' poster="' + esc(media.thumbnail) + '"' : '') + '></video>'
          + '<span class="view-once-badge">👁 View once</span>'
          + '</div>'
        );
      } else if (hasFile && t === 'view_once_video' && media.thumbnail) {
        // Thumbnail-only fallback (file gone but thumb preserved)
        pieces2.push(
          '<div class="image-container vo-media video-thumb-only">'
          + '<img class="media-thumb" src="' + esc(media.thumbnail) + '" loading="lazy">'
          + '<span class="video-play-overlay" aria-hidden="true">▶</span>'
          + '<span class="view-once-badge">👁 View once</span>'
          + '<span class="thumb-only-badge">preview only'
          +   (durStr ? ' • ' + esc(durStr) : '') + '</span>'
          + '</div>'
        );
      }
      return pieces2.join('');
    }

    if (t === 'image') {
      var thumb = media.thumbnail || '';
      var path = media.path || '';
      var caption = m.text || media.caption || '';
      var src = path || thumb;
      if (!src) return '<div class="media-badge"><span class="icon">\uD83D\uDCF7</span>Image (not on disk)</div>';
      // When only the thumbnail is available (full file missing
      // from disk), badge it so the user knows what they're seeing.
      var thumbOnly = !path && !!thumb;
      // ``data-fb`` / onerror fallback: if the disk-relative path
      // fails to load (broken bundle, missing file under media/),
      // the <img> swaps to the embedded base64 thumbnail so the
      // analyst still sees something \u2014 instead of a broken-image
      // glyph in the bubble.  Only wired when both are available.
      var fbAttrs = (path && thumb && thumb !== path)
        ? ' data-fb="' + esc(thumb) +
          '" onerror="if(this.dataset.fb && this.src!==this.dataset.fb){' +
          'this.src=this.dataset.fb;' +
          'this.parentNode.classList.add(\'thumb-only\');}"'
        : '';
      return '<div class="image-container' + (thumbOnly ? ' thumb-only' : '') + '">' +
        '<img class="media-thumb" src="' + esc(src) + '"' + fbAttrs +
          ' loading="lazy" decoding="async" alt="image">' +
        (media.isViewOnce ? '<span class="view-once-badge">\uD83D\uDC41 View once</span>' : '') +
        (thumbOnly ? '<span class="thumb-only-badge">preview only</span>' : '') +
        '</div>' +
        (caption ? '<div class="caption text">' + proc(caption, m.mentions) + '</div>' : '') +
        (path ? '<div>' + dlPill(path, media.downloadName) + '</div>' : '');
    }

    if (t === 'video') {
      var caption2 = m.text || media.caption || '';
      var thumb2 = media.thumbnail || '';
      if (media.path) {
        return '<div class="video-container">' +
          '<video class="media-thumb" src="' + esc(media.path) + '" controls preload="metadata"' +
            (thumb2 ? ' poster="' + esc(thumb2) + '"' : '') + '></video>' +
          '<span class="video-play-overlay" aria-hidden="true">\u25B6</span>' +
          '</div>' +
          (caption2 ? '<div class="caption text">' + proc(caption2, m.mentions) + '</div>' : '') +
          '<div>' + dlPill(media.path, media.downloadName) + '</div>';
      }
      // No file on disk - if WhatsApp kept the thumbnail, show that
      // with a play-icon overlay so the user still sees what was sent.
      if (thumb2) {
        return '<div class="image-container video-thumb-only">' +
          '<img class="media-thumb" src="' + esc(thumb2) +
            '" loading="lazy" decoding="async" alt="video thumbnail">' +
          '<span class="video-play-overlay" aria-hidden="true">\u25B6</span>' +
          '<span class="thumb-only-badge">preview only' +
            (media.duration ? ' \u2022 ' + fmtDuration(media.duration) : '') +
          '</span>' +
        '</div>' +
        (caption2 ? '<div class="caption text">' + proc(caption2, m.mentions) + '</div>' : '');
      }
      return '<div class="media-badge"><span class="icon">\uD83C\uDFAC</span>Video' +
        (media.duration ? ' \u2022 ' + fmtDuration(media.duration) : '') + ' (not on disk)</div>' +
        (caption2 ? '<div class="caption text">' + proc(caption2, m.mentions) + '</div>' : '');
    }

    if (t === 'gif') {
      if (media.path) {
        return '<div class="gif-container">' +
          '<video class="media-thumb" src="' + esc(media.path) +
            '" autoplay loop muted playsinline></video>' +
          '<span class="gif-badge">GIF</span></div>';
      }
      return '<div class="media-badge"><span class="icon">\uD83C\uDFAC</span>GIF (not on disk)</div>';
    }

    if (t === 'voice' || t === 'ptt') {
      var src2 = media.path || '';
      if (!src2) {
        return '<div class="audio-card"><span class="play-icon">\uD83C\uDFA4</span>' +
          '<div style="flex:1;color:var(--text-meta);font-size:12px">Voice note (not on disk)</div>' +
          '<span class="duration">' + fmtDuration(media.duration) + '</span></div>';
      }
      return '<div class="audio-card">' +
        '<span class="play-icon">\uD83C\uDFA4</span>' +
        '<audio src="' + esc(src2) + '" controls preload="none" style="flex:1;height:30px"></audio>' +
        '<span class="duration">' + fmtDuration(media.duration) + '</span>' +
        '</div>' + dlPill(src2, media.downloadName);
    }

    if (t === 'audio') {
      var src3 = media.path || '';
      return '<div class="audio-card">' +
        '<span class="play-icon">\uD83C\uDFB5</span>' +
        (src3 ? '<audio src="' + esc(src3) + '" controls preload="none" style="flex:1;height:30px"></audio>' :
                '<div style="flex:1;color:var(--text-meta);font-size:12px">' +
                   esc(media.name || 'Audio') + ' (not on disk)</div>') +
        '<span class="duration">' + fmtDuration(media.duration) + '</span>' +
        '</div>' + (src3 ? dlPill(src3, media.downloadName) : '');
    }

    if (t === 'document') {
      var mime = media.mime || '';
      var icon = '\uD83D\uDCC4';
      if (mime.indexOf('pdf') >= 0) icon = '\uD83D\uDCD5';
      else if (mime.indexOf('word') >= 0 || mime.indexOf('document') >= 0) icon = '\uD83D\uDCDD';
      else if (mime.indexOf('sheet') >= 0 || mime.indexOf('excel') >= 0) icon = '\uD83D\uDCCA';
      else if (mime.indexOf('zip') >= 0 || mime.indexOf('archive') >= 0) icon = '\uD83D\uDCE6';
      else if (mime.indexOf('android') >= 0) icon = '\uD83E\uDD16';
      var docName = media.name || media.downloadName || m.text || 'Document';
      var metaParts = [];
      if (media.size) metaParts.push(fmtSize(media.size));
      if (media.pages) metaParts.push(media.pages + ' pages');
      if (mime) metaParts.push(mime.split('/').pop().toUpperCase());
      if (!media.path) {
        return '<div class="doc-card">' +
          '<div class="doc-icon-wrap"><span class="doc-icon">' + icon + '</span></div>' +
          '<div class="doc-info">' +
            '<div class="doc-name">' + esc(docName) + '</div>' +
            (metaParts.length ? '<div class="doc-meta">' + esc(metaParts.join(' \u2022 ')) +
                                ' \u2022 not on disk</div>' : '') +
          '</div></div>';
      }
      // Inline PDF preview for PDFs (browsers render PDFs natively via <embed>)
      var isPdf = mime.indexOf('pdf') >= 0;
      var preview = isPdf ?
        '<embed src="' + esc(media.path) + '" type="application/pdf"' +
        ' style="width:100%;max-width:340px;height:220px;border-radius:8px 8px 0 0;display:block;margin-bottom:-2px">' : '';
      return preview +
        '<a class="doc-card" href="' + esc(media.path) + '"' +
          ' download="' + esc(media.downloadName || docName) + '"' +
          ' style="text-decoration:none;color:inherit' +
          (isPdf ? ';border-top-left-radius:0;border-top-right-radius:0' : '') + '">' +
        '<div class="doc-icon-wrap"><span class="doc-icon">' + icon + '</span></div>' +
        '<div class="doc-info">' +
          '<div class="doc-name">' + esc(docName) + '</div>' +
          (metaParts.length ? '<div class="doc-meta">' + esc(metaParts.join(' \u2022 ')) + '</div>' : '') +
        '</div>' +
        '<span style="flex-shrink:0;color:var(--accent);font-size:18px;padding:4px 8px">&#x21E9;</span>' +
      '</a>';
    }

    if (t === 'sticker') {
      var sp = media.path || media.thumbnail || '';
      if (sp) return '<img class="sticker-img" src="' + esc(sp) + '" alt="sticker">';
      return '<div class="media-badge">\uD83C\uDF6D Sticker (not on disk)</div>';
    }

    if (t === 'location' || t === 'live_location') {
      var loc = m.location || {};
      var coords = loc.lat && loc.lng ?
        loc.lat.toFixed(5) + ', ' + loc.lng.toFixed(5) : '';
      var mapLink = loc.lat && loc.lng ?
        'https://www.google.com/maps?q=' + loc.lat + ',' + loc.lng : '';
      var live = (t === 'live_location' || loc.live) ?
        '<div class="loc-live">\uD83D\uDD34 Live Location</div>' : '';
      return '<div class="loc-card">' +
        '<span class="loc-icon">\uD83D\uDCCD</span>' +
        '<div class="loc-info">' +
          (loc.name ? '<div class="loc-place">' + esc(loc.name) + '</div>' : '') +
          (loc.address ? '<div class="loc-addr">' + esc(loc.address) + '</div>' : '') +
          (coords ? '<div class="loc-coords">' +
            (mapLink ? '<a href="' + mapLink + '" target="_blank">' + coords + '</a>' : coords) +
            '</div>' : '') +
          live +
        '</div></div>';
    }

    if (t === 'vcard') {
      // m.vcards (plural array) is canonical - vcard_list ships 20+
      // contacts.  m.vcard (singular) is a legacy fallback.  Each
      // contact gets a card row with phones + Save (.vcf) button.
      var entries = [];
      if (Array.isArray(m.vcards) && m.vcards.length) {
        entries = m.vcards;
      } else if (m.vcard && (m.vcard.name || (m.vcard.phones && m.vcard.phones.length))) {
        entries = [m.vcard];
      } else {
        entries = [{ name: m.text || 'Contact', phones: [] }];
      }
      var rows = entries.map(function (vc, i) {
        var nm = vc.name || ('Contact ' + (i + 1));
        var phs = (vc.phones || []).filter(Boolean);
        var phoneHtml = phs.length
          ? phs.map(function (p) {
              return '<div class="vcard-phone">\uD83D\uDCDE ' + esc(p) + '</div>';
            }).join('')
          : '<div class="vcard-sub">Shared contact</div>';
        var saveBtn =
          '<button class="vcard-dl-btn" title="Save as .vcf"' +
          ' data-vcard-name="' + esc(nm) + '"' +
          ' data-vcard-phones="' + esc(phs.join('|')) + '">\uD83D\uDCBE</button>';
        return '<div class="vcard-card">' +
          '<div class="vcard-avatar">\uD83D\uDC64</div>' +
          '<div class="vcard-info">' +
            '<div class="vcard-name">' + esc(nm) + '</div>' +
            phoneHtml +
          '</div>' +
          saveBtn +
        '</div>';
      });
      var header = '';
      if (entries.length > 1) {
        header = '<div class="vcard-list-header">\uD83D\uDC65 '
          + entries.length + ' shared contacts</div>';
      }
      return '<div class="vcard-list">' + header + rows.join('') + '</div>';
    }

    if (t === 'poll') {
      var p = m.poll || {};
      var total = p.totalVotes || 0;
      var opts = (p.options || []).map(function (o) {
        var pct = total > 0 ? Math.round((o.votes || 0) / total * 100) : 0;
        return '<div class="poll-option">' +
          '<div class="poll-bar-bg">' +
            '<div class="poll-bar-fill" style="width:' + pct + '%"></div>' +
            '<div class="poll-opt-text">' + esc(o.text) + '</div>' +
            '<div class="poll-opt-count">' + (o.votes || 0) + '</div>' +
          '</div></div>';
      }).join('');
      return '<div class="poll-card">' +
        '<div class="poll-q">\uD83D\uDCCA ' + esc(p.question || '(poll)') + '</div>' +
        opts +
        '<div class="poll-footer">' + total + ' vote' + (total === 1 ? '' : 's') +
        (p.multi ? ' \u2022 multiple choice' : '') + '</div>' +
      '</div>';
    }

    if (t === 'call' || t === 'voice_chat') {
      var c = m.call || {};
      var icon2 = c.video ? '\uD83D\uDCF9' : '\uD83D\uDCDE';
      // Type label \u2014 match the in-app names so analysts switching
      // between the Qt UI and the exported viewer see consistent
      // copy.  Voice chats / group calls / multi-person calls all
      // get explicit category labels.
      var cat = c.category || (c.group ? 'group_call' : 'personal');
      var typeStr;
      if (cat === 'voice_chat') {
        typeStr = 'Voice Chat';
        icon2 = '\uD83C\uDFA4';
      } else if (cat === 'group_call') {
        typeStr = (c.video ? 'Group Video Call' : 'Group Voice Call');
      } else if (cat === 'multi_person') {
        typeStr = (c.video ? 'Multi-person Video' : 'Multi-person Voice');
      } else {
        typeStr = (c.video ? 'Video Call' : 'Voice Call');
      }
      var direction = m.fromMe ? 'Outgoing' : 'Incoming';
      var resultCls = '';
      var resultLabel = c.result || '';
      var rl = resultLabel.toLowerCase();
      if (rl === 'answered' || rl === 'accepted' || rl === 'connected'
          || rl === 'disconnected' || rl === 'completed' || rl === 'joined_voice_chat') {
        resultCls = 'call-accepted';
        resultLabel = direction;
      } else if (rl === 'missed') {
        resultCls = 'call-missed';
        resultLabel = (cat === 'voice_chat') ? 'Not Joined' : 'Missed';
      } else if (rl === 'rejected') {
        resultCls = 'call-missed';
        resultLabel = 'Declined';
      } else if (rl === 'unavailable') {
        resultCls = 'call-cancelled';
        resultLabel = 'Unavailable';
      } else if (rl === 'cancelled') {
        resultCls = 'call-cancelled';
        resultLabel = m.fromMe ? 'Cancelled' : 'Missed';
      }
      var synth = m.isSynthesized || t === 'voice_chat';
      var cls2 = 'call-card' + (synth ? ' call-synthesized' : '');
      var badge = synth ? '<div class="call-synthetic-badge">\u26A0 RECONSTRUCTED</div>' : '';
      // Origin pill \u2014 when this call's home chat differs from the
      // chat we're viewing (group / multi-person call echoed into
      // each participant's 1:1).  Two render paths:
      //
      //   1. The origin chat IS included in this export bundle \u2014
      //      render a clickable "Go to original \u2192" button that
      //      jumps to the source group/multi-person chat at the
      //      original call message.
      //   2. The origin chat is NOT in this bundle (the analyst
      //      exported the personal chat but not the group) \u2014
      //      render an informational pill explaining where the
      //      call actually happened.  Avoids leaving a button
      //      that would silently do nothing on click.
      var originPill = '';
      if (c.originConvId && c.originConvId !== _activeConvId) {
        var bundleHasOrigin = !!(_convsById && _convsById[c.originConvId]);
        var originLabelHtml =
          '<span class="call-origin-icon">\uD83D\uDC65</span>' +
          '<span class="call-origin-label">' +
            (bundleHasOrigin ? 'from' : 'originally in') + '</span> ' +
          '<span class="call-origin-name">' +
            esc(c.originName || 'group chat') + '</span>';
        if (bundleHasOrigin) {
          originPill =
            '<div class="call-origin-row">' + originLabelHtml +
              '<button class="call-origin-jump" ' +
                'onclick="event.stopPropagation();location.hash=\'#/c/' +
                esc(c.originConvId) + (c.originMsgId ? '/m/' + c.originMsgId : '') +
                '\';">Go to original \u2192</button>' +
            '</div>';
        } else {
          // Origin chat wasn't included in this export \u2014 make that
          // explicit so the analyst doesn't expect a clickable jump.
          originPill =
            '<div class="call-origin-row call-origin-row-orphan">' + originLabelHtml +
              '<span class="call-origin-note">' +
                'not included in this export' +
              '</span>' +
            '</div>';
        }
      }
      var creatorBit = c.creator
        ? ' \u2022 by <span class="call-creator">' + esc(c.creator) + '</span>'
        : '';
      var partCountBit = (c.participants && c.participants.length)
        ? ' \u2022 ' + c.participants.length + ' participant' +
          (c.participants.length === 1 ? '' : 's')
        : '';
      var durBit = c.duration ? ' \u2022 ' + fmtDuration(c.duration) : '';
      var dirCls = m.fromMe ? 'call-dir-out' : 'call-dir-in';
      var parts = c.participants || [];
      // Participants may be legacy string list or rich objects
      var preview = '';
      var expandList = '';
      if (parts.length && typeof parts[0] === 'string') {
        preview = '<div class="call-parts-preview">' + esc(parts.slice(0, 4).join(', ')) +
            (parts.length > 4 ? ' +' + (parts.length - 4) + ' more' : '') + '</div>';
      } else if (parts.length) {
        // Helper: render "Name (+phone)" so the investigator sees who
        // the participant actually is even when the device's contact
        // book had only a saved name (or only a number).  Strips any
        // phone already concatenated by the analyzer to avoid doubles.
        var fmtPart = function (p) {
          var nm = (p.name || '').replace(/\s*\(\+?\d[\d\s\-]*\)\s*$/, '').trim() || (p.phone ? '+' + p.phone : 'Unknown');
          return p.phone ? nm + ' (+' + p.phone + ')' : nm;
        };
        preview = '<div class="call-parts-preview">' +
          esc(parts.slice(0, 3).map(fmtPart).join(', ')) +
          (parts.length > 3 ? ' +' + (parts.length - 3) + ' more' : '') + '</div>';
        var statusGlyph = { joined: '\u2713', missed: '\u2716', declined: '\u2717' };
        // ``renderBody(m)`` does NOT have access to the per-message ``idx``
        // (that's a parameter of renderMessage).  Use ``m.id`` for the
        // toggle/list pairing key — it's unique per message anyway, and
        // the click handler at the bottom only does
        // ``querySelector('[data-call-list="' + ci + '"]')`` so any
        // stable string works.
        expandList = '<div class="call-expand-link" data-call-toggle="' + m.id +
          '">Show ' + parts.length + ' participant' + (parts.length === 1 ? '' : 's') +
          ' &#x25BE;</div>' +
          '<div class="call-parts-list" data-call-list="' + m.id + '">' +
          parts.map(function (p) {
            var st = (p.status || '').toLowerCase();
            var glyph = statusGlyph[st] || '\u2022';
            var cls = st === 'joined' ? 's-joined' :
                      st === 'missed' ? 's-missed' :
                      st === 'declined' ? 's-declined' : '';
            var dur = (p.joinTs && p.leaveTs) ?
              fmtDuration((p.leaveTs - p.joinTs) / 1000) : '';
            return '<div class="call-part-row">' +
              '<span class="cp-status ' + cls + '">' + glyph + '</span>' +
              '<span class="cp-name">' + esc(fmtPart(p)) + '</span>' +
              (dur ? '<span class="cp-dur">' + esc(dur) + '</span>' : '') +
            '</div>';
          }).join('') +
          '</div>';
      }
      return '<div class="' + cls2 + '">' +
        '<div class="call-icon-wrap">' +
          '<span class="call-icon-emoji">' + icon2 + '</span>' +
          '<span class="call-dir-badge ' + dirCls + '">' + direction + '</span>' +
        '</div>' +
        '<div style="flex:1;min-width:0">' +
          badge +
          originPill +
          '<div class="call-summary"><span class="call-type">' + esc(typeStr) + '</span>' +
            creatorBit + partCountBit + durBit +
          '</div>' +
          (resultLabel ? '<div class="call-result ' + resultCls + '">' +
            esc(resultLabel) + '</div>' : '') +
          preview + expandList +
        '</div></div>';
    }

    if ((t === 'revoked' || m.deleted || m.revoked) && m.ghost && m.ghost.recoveredText) {
      // Ghost-recovered: deleted-for-everyone, original text reconstructed
      // from someone's quoted reply.  Surface the recovered content
      // explicitly so the investigator can see what was deleted.
      var gh = m.ghost;
      // Forge a clickable jump-link to the message that quoted this one
      // (the source of the recovered text).  Forensics-friendly: lets
      // the investigator follow the chain of evidence in one click.
      var srcLink = gh.fromMsgId
        ? '<a class="ghost-source" data-quote-parent="' + esc(String(gh.fromMsgId))
          + '" title="Open the message that preserved this text">\u21AA View source quote</a>'
        : '';
      return '<div class="ghost-card">'
        + '<div class="ghost-header">'
        +   '<span class="ghost-badge">GHOST RECOVERED</span>'
        +   '<span class="ghost-method" title="Recovery method">'
        +     esc((gh.recoveryMethod || '').replace(/_/g, ' ')) + '</span>'
        + '</div>'
        + (gh.originalSender && gh.originalSender !== 'Unknown'
            ? '<div class="ghost-sender">Original sender: <b>'
              + esc(gh.originalSender) + '</b></div>'
            : '')
        + '<div class="ghost-text">' + proc(gh.recoveredText) + '</div>'
        + '<div class="ghost-footnote">Reconstructed from a quoted reply '
        +   '— the sender deleted this but the quote preserved it.'
        +   srcLink
        + '</div>'
        + '</div>';
    }

    if (t === 'revoked' || m.deleted) {
      return '<div class="msg-unavailable">\uD83D\uDEAB This message was deleted</div>';
    }

    if (t === 'ghost' || m.ghost) {
      return '<div class="msg-unavailable"><span class="ghost-badge">GHOST</span>' +
        proc(m.text || '(revoked content recovered from quoted reply)') + '</div>';
    }

    // Fallback
    return '<div class="text">' + proc(m.text || ('[' + t + ']'), m.mentions) + '</div>';
  }

  function renderLink(lp) {
    if (!lp) return '';
    return '<a class="link-card" href="' + esc(lp.url || '#') + '" target="_blank">' +
      (lp.image ? '<div class="link-thumb"><img src="' + esc(lp.image) + '"></div>' : '') +
      '<div class="link-info">' +
        (lp.domain ? '<div class="link-domain">' + esc(lp.domain) + '</div>' : '') +
        (lp.title ? '<div class="link-title">' + esc(lp.title) + '</div>' : '') +
        (lp.desc ? '<div class="link-desc">' + esc(lp.desc) + '</div>' : '') +
      '</div></a>';
  }

  // ------------------------------------------------------------------
  // Load & display a conversation
  // ------------------------------------------------------------------
  function openConversation(convId, targetMsgId) {
    var conv = window.__MANIFEST.conversations.filter(function (c) { return c.id === convId; })[0];
    if (!conv) {
      document.getElementById('emptyState').style.display = 'flex';
      document.getElementById('main').querySelector('#chatHeader').style.display = 'none';
      document.getElementById('chatArea').style.display = 'none';
      return;
    }

    _activeConvId = convId;
    renderSidebar(document.getElementById('sidebarSearchInput').value);
    document.getElementById('emptyState').style.display = 'none';
    document.getElementById('chatHeader').style.display = 'flex';
    document.getElementById('chatArea').style.display = 'block';

    // Header (type chips filled after messages arrive)
    var initial = (conv.title || '?').charAt(0).toUpperCase();
    var hdrAvatar = conv.avatar ?
      '<img class="h-avatar avatar-img" style="width:40px;height:40px;border-radius:50%;object-fit:cover" src="' +
        esc(conv.avatar) + '" alt="">' :
      '<div class="h-avatar" style="background:' + hashColor(conv.title) + '">' +
        esc(initial) + '</div>';
    document.getElementById('chatHeader').innerHTML =
      hdrAvatar +
      '<div class="h-info">' +
        '<div class="h-title">' + esc(conv.title) + '</div>' +
        '<div class="h-sub">' + esc(conv.type || 'personal') +
        (conv.jid ? ' \u2022 <code style="font-size:10px;color:var(--text-meta)">' +
           esc(conv.jid) + '</code>' : '') +
        (conv.participantCount ? ' \u2022 ' + conv.participantCount + ' participants' : '') +
        ' \u2022 ' + (conv.messageCount || 0) + ' messages' +
        '<span id="hdrTypeChips"></span>' +
        '</div>' +
      '</div>' +
      '<button class="h-btn" id="hdrConvSearch" title="Search in this chat (Ctrl+F)">\ud83d\udd0d</button>' +
      '<button class="h-btn" onclick="location.hash=\'\'">\u2715 Close</button>';
    var hsBtn = document.getElementById('hdrConvSearch');
    if (hsBtn) {
      hsBtn.addEventListener('click', function () {
        if (typeof window.openConvSearch === 'function') window.openConvSearch();
      });
    }

    _chat = { conv: conv, messages: [], heights: [], prefixSum: [0],
              defaultH: 60, renderedFirst: -1, renderedLast: -1, rafId: 0 };
    document.getElementById('messages').innerHTML = '';
    document.getElementById('scrollContent').style.height = '0px';
    document.getElementById('chatArea').scrollTop = 0;

    // Load all shards in order (small conversations) or all upfront for now
    var shards = conv.shards || [];
    if (!shards.length) {
      document.getElementById('messages').innerHTML =
        '<div class="system-text" style="margin:40px auto;display:block">(No messages in this conversation)</div>';
      return;
    }

    Promise.all(shards.map(function (s) {
      return loadShard(conv.id + '/' + s.key, s.path);
    })).then(function (batches) {
      var all = [];
      for (var i = 0; i < batches.length; i++) {
        for (var j = 0; j < batches[i].length; j++) all.push(batches[i][j]);
      }
      _chat.messages = all;
      // Fill in header type chips now that we know the composition
      var chipsEl = document.getElementById('hdrTypeChips');
      if (chipsEl) chipsEl.innerHTML = ' &middot; ' + buildTypeChips(all);
      rebuildHeights();
      renderVisible();
      if (targetMsgId) {
        scrollToMessageId(targetMsgId);
      } else {
        // Land at bottom (most-recent)
        var area = document.getElementById('chatArea');
        _chat.hasUserScrolled = false;
        _chat.autoStickBottomUntil = performance.now() + 1200;
        _chat.expectedScrollTop = area.scrollHeight;
        area.scrollTop = area.scrollHeight;
        renderVisible();
      }
      if (window.__updateScrollFabs) window.__updateScrollFabs();
      // Pre-warm elCache (DOM Elements) during idle so scroll never
      // hits a cold cache - each tile would otherwise be ~5-15 ms of
      // template parsing on first render.  Building a detached Element
      // costs the parse once; reusing it later is essentially free.
      if (!_chat.elCache) _chat.elCache = {};
      var prewarmIdx = 0;
      var prewarmTpl = document.createElement('template');
      var prewarm = function (deadline) {
        while (prewarmIdx < _chat.messages.length
               && (!deadline || deadline.timeRemaining() > 1)) {
          var pm = _chat.messages[prewarmIdx];
          if (pm && !_chat.elCache[pm.id]) {
            prewarmTpl.innerHTML = renderMessage(pm, prewarmIdx);
            _chat.elCache[pm.id] = prewarmTpl.content.firstElementChild;
          }
          prewarmIdx++;
        }
        if (prewarmIdx < _chat.messages.length) {
          if (window.requestIdleCallback) {
            window.requestIdleCallback(prewarm, { timeout: 500 });
          } else {
            setTimeout(prewarm, 50);
          }
        }
      };
      if (window.requestIdleCallback) {
        window.requestIdleCallback(prewarm, { timeout: 500 });
      } else {
        setTimeout(prewarm, 200);
      }
    });
  }

  function scrollToMessageId(msgId) {
    for (var i = 0; i < _chat.messages.length; i++) {
      if (String(_chat.messages[i].id) === String(msgId)) {
        var area = document.getElementById('chatArea');
        var targetIdx = i;

        // Mid-jump suppression: prevent the scroll-listener / re-anchor
        // chain from rebuilding #messages while we're trying to centre a
        // bubble.  Without this, every scrollTop we set fires a scroll
        // event that schedules a renderVisible, which rebuilds the
        // messages container's innerHTML AND changes its translateY —
        // moving our centred bubble visually off-screen by hundreds-
        // to-thousands of px.  Cleared by fineTuneAndPulse's setTimeout
        // (2.6s after the pulse, after the user has had time to see it).
        _chat.skipReanchor = true;
        _chat.suppressRenderVisible = true;

        var maxPasses = 10;
        var ensureRendered = function () {
          var el = document.querySelector('[data-msg="' + msgId + '"]');
          if (el) { fineTuneAndPulse(el); return; }
          if (--maxPasses <= 0) {
            _chat.skipReanchor = false;
            _chat.suppressRenderVisible = false;
            return;
          }
          var t = (_chat.prefixSum[targetIdx] || 0)
                  - area.clientHeight / 2
                  + (_chat.heights[targetIdx] || 0) / 2;
          area.scrollTop = Math.max(0, t);
          renderVisible();   // direct call — bypasses suppressed listener path
          requestAnimationFrame(ensureRendered);
        };

        var fineTuneAndPulse = function (el) {
          el.classList.remove('highlight-pulse');
          void el.offsetWidth;        // restart animation on repeat clicks
          el.classList.add('highlight-pulse');
          // Native scrollIntoView is pixel-accurate; suppressRenderVisible
          // remains true so the synthetic scroll event it fires can't
          // trigger a renderVisible that would rebuild #messages and
          // displace the bubble.
          el.scrollIntoView({ block: 'center', behavior: 'instant' });
          setTimeout(function () {
            _chat.suppressRenderVisible = false;
            var pulsed = document.querySelector('[data-msg="' + msgId + '"]');
            if (pulsed) pulsed.classList.remove('highlight-pulse');
          }, 2600);
          _chat.skipReanchor = false;
        };

        // Coarse scroll first so renderVisible paints the target's
        // approximate area, then iterate until the bubble is in the DOM.
        var coarse = (_chat.prefixSum[targetIdx] || 0)
                     - area.clientHeight / 2
                     + (_chat.heights[targetIdx] || 0) / 2;
        area.scrollTop = Math.max(0, coarse);
        renderVisible();
        requestAnimationFrame(ensureRendered);
        return;
      }
    }
  }

  // ------------------------------------------------------------------
  // Search palette (Cmd/Ctrl+K or '/')
  // ------------------------------------------------------------------
  function openSearchPalette() {
    document.getElementById('searchPalette').classList.add('visible');
    var inp = document.getElementById('paletteInput');
    inp.value = ''; inp.focus();
    document.getElementById('paletteResults').innerHTML = '';
  }
  function closeSearchPalette() {
    document.getElementById('searchPalette').classList.remove('visible');
  }
  function runSearch(q) {
    q = (q || '').trim();
    var res = document.getElementById('paletteResults');
    if (!q || q.length < 2) {
      res.innerHTML = '<div style="padding:20px;text-align:center;color:var(--text-meta)">' +
        'Type at least 2 characters to search across all conversations.</div>';
      return;
    }
    res.innerHTML = '<div style="padding:20px;text-align:center;color:var(--text-meta)">' +
      'Scanning loaded shards\u2026</div>';
    var needle = q.toLowerCase();
    var matches = [];
    var convs = window.__MANIFEST.conversations.slice();
    var pending = convs.length;
    if (pending === 0) { render([]); return; }
    convs.forEach(function (conv) {
      var shards = conv.shards || [];
      if (!shards.length) { if (--pending === 0) render(matches); return; }
      Promise.all(shards.map(function (s) {
        return loadShard(conv.id + '/' + s.key, s.path);
      })).then(function (batches) {
        batches.forEach(function (batch) {
          for (var i = 0; i < batch.length; i++) {
            var m = batch[i];
            // Search across:
            //   - the visible body text + caption + sender name
            //   - ghost-recovered text (so deleted msgs are findable)
            //   - quote previews (so a reply's quoted text is findable)
            //   - event names + descriptions (scheduled events)
            //   - vcard names (so contact-card sends are findable)
            //   - poll questions + option labels
            //   - link card title/description/domain
            //   - document filenames
            var hay = (m.text || '')
                    + ' ' + (m.media && m.media.caption || '')
                    + ' ' + (m.media && m.media.name || '')
                    + ' ' + (m.senderName || '')
                    + ' ' + (m.ghost && m.ghost.recoveredText || '')
                    + ' ' + (m.quoted && m.quoted.preview || '')
                    + ' ' + (m.event && (m.event.name || '') + ' ' + (m.event.description || '') || '')
                    + ' ' + (m.vcard && m.vcard.name || '')
                    + ' ' + (m.linkPreview && [m.linkPreview.title, m.linkPreview.desc, m.linkPreview.domain].filter(Boolean).join(' ') || '');
            if (m.poll) {
              hay += ' ' + (m.poll.question || '');
              if (m.poll.options) {
                for (var po = 0; po < m.poll.options.length; po++) {
                  hay += ' ' + (m.poll.options[po].text || '');
                }
              }
            }
            if (hay.toLowerCase().indexOf(needle) >= 0) {
              matches.push({ m: m, conv: conv });
              if (matches.length > 500) break;
            }
          }
        });
        if (--pending === 0) render(matches);
      });
    });

    function render(items) {
      if (!items.length) {
        res.innerHTML = '<div style="padding:20px;text-align:center;color:var(--text-meta)">' +
          'No matches for <b>' + esc(q) + '</b></div>';
        return;
      }
      // Group by conversation
      var byConv = {};
      items.forEach(function (h) {
        (byConv[h.conv.id] = byConv[h.conv.id] || { conv: h.conv, hits: [] }).hits.push(h.m);
      });
      var html = '';
      Object.keys(byConv).forEach(function (cid) {
        var g = byConv[cid];
        html += '<div class="palette-hit" data-conv="' + cid + '">' +
          '<div class="h-conv">' + esc(g.conv.title) + ' \u00B7 ' +
            g.hits.length + ' match' + (g.hits.length === 1 ? '' : 'es') + '</div>' +
          '</div>';
        g.hits.slice(0, 3).forEach(function (m) {
          // Pick the FIELD where the needle actually hit so the rendered
          // snippet shows the matched text - not the (often empty) body
          // for deleted msgs / vcards / polls / etc.
          var fields = [
            { src: m.text || '',                                       tag: '' },
            { src: (m.media && m.media.caption) || '',                 tag: 'caption' },
            { src: (m.ghost && m.ghost.recoveredText) || '',           tag: 'ghost' },
            { src: (m.quoted && m.quoted.preview) || '',               tag: 'quoted' },
            { src: (m.event && [m.event.name, m.event.description].filter(Boolean).join(' \u2022 ')) || '', tag: 'event' },
            { src: (m.vcard && m.vcard.name) || '',                    tag: 'contact' },
            { src: (m.poll && m.poll.question) || '',                  tag: 'poll' },
            { src: (m.linkPreview && [m.linkPreview.title, m.linkPreview.desc].filter(Boolean).join(' \u2022 ')) || '', tag: 'link' },
            { src: (m.media && m.media.name) || '',                    tag: 'file' },
          ];
          var picked = fields[0];
          for (var f = 0; f < fields.length; f++) {
            if (fields[f].src && fields[f].src.toLowerCase().indexOf(needle) >= 0) {
              picked = fields[f]; break;
            }
          }
          var snip = snippet(picked.src, needle);
          var tagBadge = picked.tag
            ? ' <span class="h-tag h-tag-' + picked.tag + '">' + picked.tag + '</span>'
            : '';
          html += '<div class="palette-hit" data-conv="' + cid + '" data-msg="' + m.id + '">' +
            '<div class="h-text">' + snip + tagBadge + '</div>' +
            '<div class="h-meta">' + esc(m.senderName || (m.fromMe ? 'You' : '')) + ' \u00B7 ' +
              esc(fmtDate(m.ts)) + '</div>' +
          '</div>';
        });
      });
      res.innerHTML = html;
      res.querySelectorAll('.palette-hit').forEach(function (el) {
        el.addEventListener('click', function () {
          var cid = el.getAttribute('data-conv');
          var mid = el.getAttribute('data-msg');
          closeSearchPalette();
          location.hash = '#/c/' + cid + (mid ? '/m/' + mid : '');
        });
      });
    }
  }

  function snippet(text, needle) {
    if (!text) return '';
    var idx = text.toLowerCase().indexOf(needle);
    if (idx < 0) return esc(text.substring(0, 80));
    var start = Math.max(0, idx - 30);
    var end = Math.min(text.length, idx + needle.length + 60);
    var pre = (start > 0 ? '\u2026' : '') + text.substring(start, idx);
    var hit = text.substring(idx, idx + needle.length);
    var post = text.substring(idx + needle.length, end) + (end < text.length ? '\u2026' : '');
    return esc(pre) + '<mark>' + esc(hit) + '</mark>' + esc(post);
  }

  // ------------------------------------------------------------------
  // Hash routing
  // ------------------------------------------------------------------
  function handleRoute() {
    var h = location.hash.replace(/^#\/?/, '');
    if (!h) {
      _activeConvId = null;
      renderSidebar(document.getElementById('sidebarSearchInput').value);
      document.getElementById('emptyState').style.display = 'flex';
      document.getElementById('chatHeader').style.display = 'none';
      document.getElementById('chatArea').style.display = 'none';
      return;
    }
    var parts = h.split('/');
    if (parts[0] === 'c' && parts[1]) {
      var cid = parts[1];
      var mid = (parts[2] === 'm' && parts[3]) ? parts[3] : null;
      openConversation(cid, mid);
    } else if (parts[0] === 'search') {
      openSearchPalette();
    }
  }

  // ------------------------------------------------------------------
  // Forensic info panel — full evidence-grade detail on demand
  // ------------------------------------------------------------------
  function openForensicPanel(msg) {
    if (!msg) return;
    var case_ = (window.__MANIFEST.caseInfo) || {};
    var conv = _chat.conv || {};

    function row(k, v) {
      if (v == null || v === '' || v === false) return '';
      return '<div class="fp-row"><div class="fp-key">' + esc(k) + '</div>' +
             '<div class="fp-val">' + v + '</div></div>';
    }
    function codeRow(k, v) {
      if (!v && v !== 0) return '';
      return row(k, '<code>' + esc(String(v)) + '</code>');
    }

    var media = msg.media || {};
    var loc = msg.location || {};
    var vcard = msg.vcard || {};
    var call = msg.call || {};
    var poll = msg.poll || {};
    var quoted = msg.quoted || null;

    var sections = [];

    // Message identity
    sections.push('<div class="fp-section"><h4>Message Identity</h4>' +
      codeRow('Analysis ID', msg.id) +
      codeRow('Source _id (msgstore)', msg.sourceMsgId) +
      codeRow('Source key_id', msg.keyId) +
      row('Type', esc(msg.type) + (msg.typeLabel && msg.typeLabel !== msg.type ?
           ' <code>' + esc(msg.typeLabel) + '</code>' : '')) +
      (msg.eventLabel ? row('Event label', '<code>' + esc(msg.eventLabel) + '</code>') : '') +
      (msg.isSynthesized ? row('Note', '&#x26A0; This row is <b>reconstructed</b> ' +
           'from adjacent evidence (e.g. voice-chat synthesized from call_participant).') : '') +
      '</div>');

    // Participants
    var sentBy = msg.fromMe ? ('You (Owner)' + (case_.owner_phone ? ' &middot; +' +
                      esc(case_.owner_phone) : '')) :
                 esc(msg.senderName || 'Unknown');
    sections.push('<div class="fp-section"><h4>Sender / Direction</h4>' +
      row('Direction', msg.fromMe ? '&#x2191; Outgoing (from this device)' : '&#x2193; Incoming') +
      row('Sender name', sentBy) +
      (msg.senderJid ? codeRow('Sender JID', msg.senderJid) : '') +
      (msg.senderPhone ? codeRow('Sender phone', '+' + msg.senderPhone) : '') +
      '</div>');

    // Timing
    sections.push('<div class="fp-section"><h4>Timing</h4>' +
      row('Message timestamp', msg.ts ? (fmtDate(msg.ts) + ' ' + fmtTime(msg.ts) +
          ' <code>' + new Date(msg.ts).toISOString() + '</code>') : '&mdash;') +
      (msg.lastEditTs ? row('Last edit', fmtDate(msg.lastEditTs) + ' ' + fmtTime(msg.lastEditTs)) : '') +
      (msg.editCount ? row('Edits', msg.editCount + ' revision(s)') : '') +
      '</div>');

    // Flags
    var flagRows = [];
    if (msg.starred) flagRows.push(row('Starred', '&#x2B50; Yes'));
    if (msg.revoked) flagRows.push(row('Revoked (deleted for everyone)', 'Yes'));
    if (msg.viewOnce) flagRows.push(row('View-once', 'Yes'));
    if (msg.ephemeral) flagRows.push(row('Disappearing', 'Yes'));
    if (msg.forwardScore) flagRows.push(row('Forwarded', msg.forwardScore +
        ' hop(s)' + (msg.forwardScore >= 5 ? ' &mdash; many times' : '')));
    if (msg.status) flagRows.push(row('Delivery status', esc(msg.status)));
    if (flagRows.length) {
      sections.push('<div class="fp-section"><h4>Flags</h4>' + flagRows.join('') + '</div>');
    }

    // Body
    if (msg.text) {
      sections.push('<div class="fp-section"><h4>Text body</h4>' +
        '<div class="fp-row"><div class="fp-val" style="white-space:pre-wrap">' +
        esc(msg.text) + '</div></div></div>');
    }

    // Quoted reply
    if (quoted) {
      sections.push('<div class="fp-section"><h4>Reply context</h4>' +
        codeRow('Parent message id', quoted.parentId) +
        row('Quoted type', esc(quoted.type || '')) +
        (quoted.preview ? row('Preview', '<span style="white-space:pre-wrap">' +
            esc(quoted.preview) + '</span>') : '') +
      '</div>');
    }

    // Media
    if (media && (media.path || media.mime || media.size)) {
      // Provenance classification - critical for forensic accuracy.
      // hash_linked means THIS message was never received as media on
      // the device; we are showing a content-equivalent file from
      // another chat that has the same SHA-256.  Without flagging
      // this, an analyst could mistake "Bundle path" for proof of
      // receipt for this specific message.
      var _isHashLinked      = media.recoveryMethod === 'hash_linked';
      var _isHashDeleted     = media.recoveryMethod === 'hash_linked_after_delete';
      var _isOrphanRecovered = media.recoveryMethod === 'orphan_recovered';
      var _isDownloaded      = media.recoveryMethod === 'downloaded';
      var _isOriginal        = media.fileExists && !media.recoveryMethod;

      var _provLine = '';
      if (_isOriginal) _provLine = '<span style="color:#2e7d32">\u{1F7E2} Received in this chat \u2014 file was on the phone at extraction</span>';
      else if (_isDownloaded) _provLine = '<span style="color:#00897b">\u2B07 Recovered by tool \u2014 downloaded from WhatsApp CDN after extraction</span>';
      else if (_isOrphanRecovered) _provLine = '<span style="color:#2e7d32">\uD83D\uDCBE Rescued from orphaned file \u2014 chat record was lost (cleared/reinstalled) but the file with this SHA-256 was still in the WhatsApp media folder</span>';
      else if (_isHashDeleted) _provLine = '<span style="color:#e65100">\u26A0 Originally received in this chat, but the local file was later deleted \u2014 same SHA-256 found in another message; that file is shown</span>';
      else if (_isHashLinked) _provLine = '<span style="color:#7b1fa2">\u{1F517} Hash-linked \u2014 same SHA-256 received in another message, NOT here</span>';
      else if (!media.fileExists && media.cdnUrl && media.hasKey) _provLine = '<span style="color:#1565c0">\u{1F535} Not downloaded \u2014 still available on CDN</span>';
      else if (!media.fileExists) _provLine = '<span style="color:#c62828">\u274C Missing \u2014 no file, no URL</span>';

      // Path label adjusts to the provenance so the row never misleads.
      var _pathLbl = 'Bundle path';
      var _pathExtra = '';
      if (_isHashDeleted) {
        _pathLbl = 'Bundle path (hash-linked, original was deleted)';
        _pathExtra = '<div style="color:#e65100;font-size:10px;margin-top:2px">\u26A0 Originally received in this chat, but the local file was deleted. The bundled file is from another message with the same SHA-256.</div>';
      } else if (_isOrphanRecovered) {
        _pathLbl = 'Bundle path (rescued from orphaned file)';
        _pathExtra = '<div style="color:#2e7d32;font-size:10px;margin-top:2px">\uD83D\uDCBE The chat record was lost (cleared chat / reinstall) but the file with this SHA-256 was still in the WhatsApp media folder. The bundled file is that orphaned copy \u2014 same original bytes.</div>';
      } else if (_isHashLinked) {
        _pathLbl = 'Bundle path (hash-linked)';
        _pathExtra = '<div style="color:#7b1fa2;font-size:10px;margin-top:2px">This message had no downloaded file on the device. The bundled file is from a DIFFERENT message with the same SHA-256.</div>';
      } else if (_isDownloaded) {
        _pathLbl = 'Bundle path (recovered by tool)';
      }

      var _hashRows = '';
      if (media.fileHash) {
        try {
          var _hexh = b64ToHexLower(media.fileHash);
          if (_hexh) _hashRows += row('SHA-256', '<code style="font-size:10px;word-break:break-all">' + esc(_hexh) + '</code>');
        } catch (e) {}
      }

      var _cdnRows = '';
      if (media.cdnUrl) {
        var _host = media.cdnUrl.match(/https?:\/\/([^\/]+)/);
        var _oeMatch = media.cdnUrl.match(/oe=([0-9A-Fa-f]+)/);
        var _expTs = _oeMatch ? parseInt(_oeMatch[1], 16) : 0;
        var _expDate = _expTs ? new Date(_expTs * 1000) : null;
        _cdnRows += row('CDN host', _host ? esc(_host[1]) : '&mdash;');
        // Full URL, not truncated - analyst needs the oe= and _nc_sid
        // tail to prove URL validity period.
        _cdnRows += row('CDN URL',
          '<code style="font-size:9px;word-break:break-all;display:block;max-height:140px;overflow:auto;background:rgba(127,127,127,0.07);padding:4px;border-radius:3px">'
          + esc(media.cdnUrl) + '</code>');
        if (_expDate) {
          var _expIso = _expDate.toISOString().replace('T', ' ').substring(0, 19) + ' UTC';
          var _expired = _expDate.getTime() < Date.now();
          var _daysLeft = ((_expDate.getTime() - Date.now()) / 86400000).toFixed(1);
          _cdnRows += row('URL expires',
            '<span style="color:' + (_expired ? '#e53935' : '#2e7d32') + ';font-weight:600">'
            + _expIso + (_expired ? ' (EXPIRED ' + Math.abs(_daysLeft) + ' days ago)' : ' (' + _daysLeft + ' days left)') + '</span>');
        }
      }

      sections.push('<div class="fp-section"><h4>Media</h4>' +
        row('MIME type', esc(media.mime || '&mdash;')) +
        row('Size', media.size ? fmtSize(media.size) : '&mdash;') +
        (media.width ? row('Dimensions', media.width + ' \u00D7 ' + media.height) : '') +
        (media.duration ? row('Duration', fmtDuration(media.duration)) : '') +
        (media.pages ? row('Pages', String(media.pages)) : '') +
        (media.name ? row('Original filename', esc(media.name)) : '') +
        (_provLine ? row('Provenance', _provLine) : '') +
        (media.path ? row(_pathLbl, '<code>' + esc(media.path) + '</code>' + _pathExtra) : '') +
        _hashRows +
        _cdnRows +
      '</div>');
    }

    // Location
    if (loc.lat != null) {
      sections.push('<div class="fp-section"><h4>Location</h4>' +
        row('Latitude, Longitude',
            '<code>' + loc.lat + ', ' + loc.lng + '</code>' +
            ' &middot; <a href="https://www.google.com/maps?q=' + loc.lat + ',' + loc.lng +
            '" target="_blank">open in maps</a>') +
        (loc.name ? row('Place', esc(loc.name)) : '') +
        (loc.address ? row('Address', esc(loc.address)) : '') +
        (loc.live ? row('Live', 'Yes') : '') +
      '</div>');
    }

    // Call
    if (call.duration != null || call.video != null || call.participants) {
      sections.push('<div class="fp-section"><h4>Call metadata</h4>' +
        row('Kind', (call.video ? 'Video' : 'Voice') +
            (call.group ? ' group call' : ' call')) +
        row('Duration', fmtDuration(call.duration || 0)) +
        row('Result', esc(call.result || '&mdash;')) +
        (call.participants && call.participants.length ?
          row('Participants', esc(call.participants.join(', '))) : '') +
      '</div>');
    }

    // Poll
    if (poll.question) {
      var optBits = (poll.options || []).map(function (o) {
        return esc(o.text) + ' &mdash; ' + (o.votes || 0) + ' vote(s)';
      }).join('<br>');
      sections.push('<div class="fp-section"><h4>Poll</h4>' +
        row('Question', esc(poll.question)) +
        row('Mode', poll.multi ? 'Multiple choice' : 'Single choice') +
        row('Total votes', poll.totalVotes || 0) +
        (optBits ? row('Options', optBits) : '') +
      '</div>');
    }

    // vCard
    if (vcard.name) {
      sections.push('<div class="fp-section"><h4>Shared contact</h4>' +
        row('Name', esc(vcard.name)) +
        (vcard.phones && vcard.phones.length ?
          row('Phone number(s)', esc(vcard.phones.join(', '))) : '') +
      '</div>');
    }

    // Reactions
    if (msg.reactions && msg.reactions.length) {
      var rBits = msg.reactions.map(function (r) {
        return esc(r.emoji) + ' \u00D7 ' + r.count +
          (r.from ? ' <span style="color:var(--text-meta)">(' +
              esc(r.from.slice(0, 5).join(', ')) +
              (r.from.length > 5 ? ', +' + (r.from.length - 5) + ' more' : '') +
          ')</span>' : '');
      }).join('<br>');
      sections.push('<div class="fp-section"><h4>Reactions</h4>' +
        '<div class="fp-row"><div class="fp-val">' + rBits + '</div></div></div>');
    }

    // Mentions
    if (msg.mentions && msg.mentions.length) {
      sections.push('<div class="fp-section"><h4>Mentions</h4>' +
        '<div class="fp-row"><div class="fp-val">' + esc(msg.mentions.join(', ')) +
        '</div></div></div>');
    }

    // Provenance — where this came from
    sections.push('<div class="fp-section"><h4>Provenance</h4>' +
      row('Conversation', esc(conv.title || '') +
          (conv.id ? ' <code>' + esc(conv.id) + '</code>' : '')) +
      row('Chat type', esc(conv.type || '')) +
      (conv.jid ? codeRow('Conversation JID', conv.jid) : '') +
      (case_.case_id ? row('Case ID', esc(case_.case_id)) : '') +
      (case_.examiner ? row('Examiner', esc(case_.examiner)) : '') +
      (case_.analysis_db ? row('Analysis DB', '<code>' + esc(case_.analysis_db) + '</code>') : '') +
      (case_.analysis_db_sha256 ? codeRow('analysis.db SHA-256',
           case_.analysis_db_sha256) : '') +
      (case_.source_msgstore_path ? row('Source msgstore',
           '<code>' + esc(case_.source_msgstore_path) + '</code>') : '') +
      (case_.source_msgstore_sha256 ? codeRow('msgstore SHA-256',
           case_.source_msgstore_sha256) : '') +
      row('Exported by', 'WAInsight Viewer v2 &middot; bundle built ' +
          fmtDate(window.__MANIFEST.exportedAt || 0)) +
    '</div>');

    document.getElementById('fpBody').innerHTML = sections.join('');
    document.getElementById('forensicPanel').classList.add('visible');
  }

  function closeForensicPanel() {
    document.getElementById('forensicPanel').classList.remove('visible');
  }

  // ------------------------------------------------------------------
  // Chat header type breakdown (counts per type: image / video / voice / poll / call\u2026)
  // ------------------------------------------------------------------
  function buildTypeChips(messages) {
    var counts = {};
    for (var i = 0; i < messages.length; i++) {
      var t = messages[i].type || 'text';
      if (t === 'system' || t === 'text') continue;
      counts[t] = (counts[t] || 0) + 1;
    }
    var ICONS = {
      image: '\uD83D\uDCF7', video: '\uD83C\uDFAC', gif: '\uD83C\uDFAC',
      voice: '\uD83C\uDFA4', ptt: '\uD83C\uDFA4', audio: '\uD83C\uDFB5',
      document: '\uD83D\uDCC4', sticker: '\uD83C\uDF6D',
      location: '\uD83D\uDCCD', live_location: '\uD83D\uDCCD',
      vcard: '\uD83D\uDC64', poll: '\uD83D\uDCCA',
      call: '\uD83D\uDCDE', voice_chat: '\uD83D\uDCDE',
      revoked: '\uD83D\uDEAB', ghost: '\uD83D\uDC7B',
    };
    var ORDER = ['image','video','gif','voice','ptt','audio','document','sticker',
                 'location','live_location','vcard','poll','call','voice_chat',
                 'revoked','ghost'];
    var bits = [];
    ORDER.forEach(function (k) {
      if (counts[k]) bits.push('<span class="hdr-type-chip">' +
        (ICONS[k] || '') + ' ' + counts[k] + '</span>');
    });
    return bits.join('');
  }

  // ------------------------------------------------------------------
  // Media lightbox — pan + zoom + keyboard nav + download
  // ------------------------------------------------------------------
  var _lb = {
    items: [],     // [{ path, name, mime, isVideo, size, width, height, ts, senderName }]
    index: 0,
    zoom: 1.0,
    tx: 0, ty: 0,
    isPanning: false,
    panStartX: 0, panStartY: 0,
  };

  function collectMediaItems() {
    /* Build the lightbox playlist from the current conversation's loaded messages.
       Only images + videos (not documents/audio — those have their own controls).
       Album children live on m.album.children (not in _chat.messages directly,
       since the exporter strips them from the main stream and pushes them under
       the parent), so we walk into the children list and emit each as its own
       lightbox item with senderName/ts inherited from the album parent. */
    var items = [];
    var pushAsItem = function (parent, child, idxParent) {
      var t = child.type;
      var media = child.media || {};
      if (!media.path) return;
      if (t !== 'image' && t !== 'video' && t !== 'gif') return;
      items.push({
        msgIdx: idxParent, msgId: child.id,
        path: media.path,
        name: media.downloadName || media.name || ('whatsapp_' + child.id),
        mime: media.mime || '',
        isVideo: t === 'video' || t === 'gif',
        size: media.size || 0,
        width: media.width || 0,
        height: media.height || 0,
        // Inherit timing + sender attribution from the album parent so
        // the lightbox header reads consistently across all children.
        ts: parent.ts,
        senderName: parent.senderName || (parent.fromMe ? 'You' : ''),
        senderJid: parent.senderJid || parent.senderLid || '',
        senderPhone: parent.senderPhone || '',
        fromMe: !!parent.fromMe,
        albumParentId: parent.id,
      });
    };
    for (var i = 0; i < _chat.messages.length; i++) {
      var m = _chat.messages[i];
      if (!m) continue;
      // Album parent: walk its children
      if (m.type === 'album' && m.album && m.album.children) {
        for (var j = 0; j < m.album.children.length; j++) {
          pushAsItem(m, m.album.children[j], i);
        }
        continue;
      }
      // Plain media message
      if (!m.media || !m.media.path) continue;
      var t = m.type;
      if (t === 'image' || t === 'video' || t === 'gif') {
        items.push({
          msgIdx: i, msgId: m.id,
          path: m.media.path,
          name: m.media.downloadName || m.media.name || ('whatsapp_' + m.id),
          mime: m.media.mime || '',
          isVideo: t === 'video' || t === 'gif',
          size: m.media.size || 0,
          width: m.media.width || 0,
          height: m.media.height || 0,
          ts: m.ts,
          senderName: m.senderName || (m.fromMe ? 'You' : ''),
          senderJid: m.senderJid || m.senderLid || '',
          senderPhone: m.senderPhone || '',
          fromMe: !!m.fromMe,
        });
      }
    }
    return items;
  }

  function openLightbox(srcPath) {
    _lb.items = collectMediaItems();
    _lb.index = Math.max(0, _lb.items.findIndex(function (x) { return x.path === srcPath; }));
    if (_lb.items.length === 0) return;
    document.getElementById('lightbox').classList.add('visible');
    renderLightbox();
  }
  function closeLightbox() {
    document.getElementById('lightbox').classList.remove('visible');
    // Pause any playing video
    var vid = document.querySelector('#lbStage video');
    if (vid) { try { vid.pause(); } catch (e) {} }
  }
  function renderLightbox() {
    var item = _lb.items[_lb.index];
    if (!item) return;
    _lb.zoom = 1; _lb.tx = 0; _lb.ty = 0;
    var stage = document.getElementById('lbStage');
    // Preserve the nav buttons / zoom bar; swap only the media element
    var old = stage.querySelector('.lb-img, .lb-video');
    if (old) old.remove();
    if (item.isVideo) {
      var v = document.createElement('video');
      v.className = 'lb-video'; v.src = item.path; v.controls = true; v.autoplay = true;
      stage.insertBefore(v, stage.firstChild);
    } else {
      var img = document.createElement('img');
      img.className = 'lb-img'; img.src = item.path; img.alt = item.name;
      stage.insertBefore(img, stage.firstChild);
    }
    document.getElementById('lbName').textContent = item.name;
    var metaBits = [];
    if (item.width && item.height) metaBits.push(item.width + '\u00D7' + item.height);
    if (item.size) metaBits.push(fmtSize(item.size));
    // Sender + formatted phone + JID — investigators need the JID
    // alongside the human name, since names can change but the JID is
    // authoritative.
    if (item.senderName) metaBits.push(item.senderName);
    if (item.senderPhone) {
      var d = String(item.senderPhone).replace(/[^0-9]/g, '');
      if (d.length === 12 && d.indexOf('91') === 0)        metaBits.push('+91 ' + d.slice(2,7) + ' ' + d.slice(7));
      else if (d.length === 11 && d.indexOf('1') === 0)    metaBits.push('+1 '  + d.slice(1,4) + ' ' + d.slice(4,7) + ' ' + d.slice(7));
      else if (d)                                          metaBits.push('+' + d);
    }
    if (item.senderJid) metaBits.push(item.senderJid);
    if (item.ts) metaBits.push(fmtDate(item.ts) + ' ' + fmtTime(item.ts));
    document.getElementById('lbMeta').textContent = metaBits.join(' \u00B7 ');
    var dl = document.getElementById('lbDownload');
    dl.href = item.path; dl.setAttribute('download', item.name);
    // Wire the "Go to msg" button — its click handler is registered
    // once in wireLightbox; we only stash the current msg id here so
    // the click navigates to whichever item is currently being viewed.
    var gotoBtn = document.getElementById('lbGoto');
    if (gotoBtn) gotoBtn.setAttribute('data-msg-id', String(item.msgId || ''));
    document.getElementById('lbZoomLevel').textContent = '100%';
    // Hide nav arrows if only one item
    document.getElementById('lbPrev').style.display = _lb.items.length > 1 ? '' : 'none';
    document.getElementById('lbNext').style.display = _lb.items.length > 1 ? '' : 'none';
  }
  function applyZoom() {
    var img = document.querySelector('#lbStage .lb-img');
    if (!img) return;
    img.style.transform = 'translate(' + _lb.tx + 'px,' + _lb.ty + 'px) scale(' + _lb.zoom + ')';
    document.getElementById('lbZoomLevel').textContent = Math.round(_lb.zoom * 100) + '%';
  }
  function zoomBy(factor) { _lb.zoom = Math.max(0.25, Math.min(10, _lb.zoom * factor)); applyZoom(); }
  function resetZoom() { _lb.zoom = 1; _lb.tx = 0; _lb.ty = 0; applyZoom(); }
  function lbNav(dir) {
    _lb.index = (_lb.index + dir + _lb.items.length) % _lb.items.length;
    renderLightbox();
  }

  function wireLightbox() {
    document.getElementById('lbClose').addEventListener('click', closeLightbox);
    document.getElementById('lbPrev').addEventListener('click', function () { lbNav(-1); });
    document.getElementById('lbNext').addEventListener('click', function () { lbNav(+1); });
    // "Go to msg" — close the lightbox and jump the chat to the message
    // this media belongs to.  scrollToMessageId already handles the
    // pulse-highlight + multi-frame settle so the user lands on the
    // exact bubble.
    var gotoBtn = document.getElementById('lbGoto');
    if (gotoBtn) {
      gotoBtn.addEventListener('click', function () {
        var mid = gotoBtn.getAttribute('data-msg-id');
        if (!mid) return;
        closeLightbox();
        // scrollToMessageId accepts a string id (matches against
        // _chat.messages[i].id which can be int or "vc:N"); coerce
        // back to number when applicable so the comparison succeeds.
        var asNum = Number(mid);
        scrollToMessageId(isNaN(asNum) ? mid : asNum);
      });
    }
    document.getElementById('lbZoomIn').addEventListener('click', function () { zoomBy(1.25); });
    document.getElementById('lbZoomOut').addEventListener('click', function () { zoomBy(0.8); });
    document.getElementById('lbZoomFit').addEventListener('click', resetZoom);

    // Wheel = zoom
    document.getElementById('lbStage').addEventListener('wheel', function (e) {
      if (!document.getElementById('lightbox').classList.contains('visible')) return;
      e.preventDefault();
      zoomBy(e.deltaY > 0 ? 0.88 : 1.14);
    }, { passive: false });

    // Mouse drag to pan when zoomed
    document.getElementById('lbStage').addEventListener('mousedown', function (e) {
      var img = document.querySelector('#lbStage .lb-img');
      if (!img || _lb.zoom <= 1) return;
      _lb.isPanning = true;
      _lb.panStartX = e.clientX - _lb.tx; _lb.panStartY = e.clientY - _lb.ty;
      img.classList.add('panning');
    });
    window.addEventListener('mouseup', function () {
      _lb.isPanning = false;
      var img = document.querySelector('#lbStage .lb-img');
      if (img) img.classList.remove('panning');
    });
    window.addEventListener('mousemove', function (e) {
      if (!_lb.isPanning) return;
      _lb.tx = e.clientX - _lb.panStartX; _lb.ty = e.clientY - _lb.panStartY;
      applyZoom();
    });

    // Click-on-image (not zoomed) advances; double-click toggles zoom
    document.getElementById('lbStage').addEventListener('dblclick', function (e) {
      if (e.target.classList && e.target.classList.contains('lb-img')) {
        if (_lb.zoom === 1) { _lb.zoom = 2; applyZoom(); } else resetZoom();
      }
    });
  }

  // Delegate image/video clicks in the chat area to the lightbox
  function wireMediaClicks() {
    document.getElementById('chatArea').addEventListener('click', function (e) {
      if (e.target.closest('.msg-info-btn')) return;   // info button handled elsewhere
      if (e.target.closest('.dl-pill')) return;         // download pill: let browser handle
      if (e.target.closest('.doc-card')) return;        // document download: let browser handle
      // Album cell: prefer the data-fullsrc (full path) so the lightbox
      // playlist findIndex() matches; fall back to img src.
      var albumCell = e.target.closest('.album-cell');
      if (albumCell) {
        e.preventDefault();
        var albImg = albumCell.querySelector('img.album-thumb');
        if (albImg) {
          var fullsrc = albImg.getAttribute('data-fullsrc') || albImg.getAttribute('src');
          openLightbox(fullsrc);
        }
        return;
      }
      var img = e.target.closest('.msg img.media-thumb');
      if (img) {
        e.preventDefault();
        openLightbox(img.getAttribute('src'));
        return;
      }
      var vid = e.target.closest('.msg video.media-thumb');
      if (vid) {
        // Don't hijack play/pause on in-bubble video — only open lightbox if double-click
        return;
      }
      var stk = e.target.closest('.sticker-img');
      if (stk) { openLightbox(stk.getAttribute('src')); return; }
    });
  }

  // ------------------------------------------------------------------
  // Theme toggle
  // ------------------------------------------------------------------
  function toggleTheme() {
    document.body.classList.toggle('dark');
    try { localStorage.setItem('wag-theme', document.body.classList.contains('dark') ? 'dark' : 'light'); } catch (e) {}
    renderVisible();
  }

  function isTypingTarget(el) {
    if (!el) return false;
    var tag = (el.tagName || '').toUpperCase();
    return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable;
  }

  function handleChatKeyboardScroll(e) {
    if (isTypingTarget(document.activeElement)) return false;
    if (e.metaKey || e.ctrlKey || e.altKey) return false;
    var area = document.getElementById('chatArea');
    var sc = document.getElementById('scrollContent');
    if (!area || !sc || area.style.display === 'none') return false;

    var page = Math.max(120, area.clientHeight - 96);
    var target = null;
    if (e.key === 'PageUp') target = area.scrollTop - page;
    else if (e.key === 'PageDown') target = area.scrollTop + page;
    else if (e.key === 'Home') target = 0;
    else if (e.key === 'End') target = sc.offsetHeight;
    else if (e.key === 'ArrowUp') target = area.scrollTop - 48;
    else if (e.key === 'ArrowDown') target = area.scrollTop + 48;
    else if (e.key === ' ' || e.key === 'Spacebar') {
      target = area.scrollTop + (e.shiftKey ? -page : page);
    } else {
      return false;
    }

    markUserScroll(1200);
    var maxST = Math.max(0, sc.offsetHeight - area.clientHeight);
    area.scrollTop = Math.max(0, Math.min(maxST, target));
    scheduleRenderVisible();
    e.preventDefault();
    return true;
  }

  // ------------------------------------------------------------------
  // Boot
  // ------------------------------------------------------------------
  document.addEventListener('DOMContentLoaded', function () {
    try {
      if (localStorage.getItem('wag-theme') === 'dark') document.body.classList.add('dark');
    } catch (e) {}

    var area = document.getElementById('chatArea');
    area.setAttribute('tabindex', '0');
    area.addEventListener('wheel', function () {
      markUserScroll(900);
    }, { passive: true });
    area.addEventListener('scroll', function () {
      // Skip ONLY the synthetic scroll event we fired ourselves during
      // a height re-anchor.  Match by exact value so user scrolls
      // aren't silently swallowed (the missing-tile bug).
      if (_chat.expectedScrollTop != null
          && Math.abs(area.scrollTop - _chat.expectedScrollTop) < 2) {
        _chat.expectedScrollTop = null;
        return;
      }
      _chat.expectedScrollTop = null;

      // Stamp every USER (non-synthetic) scroll so doMeasure can see
      // whether the user is actively scrolling and skip the re-anchor.
      // Without this, deferred height measurements yank scrollTop in
      // the WRONG direction during fast scroll - the "I scroll up but
      // it ends up going down" bug.
      markUserScroll(900);

      scheduleRenderVisible();
    }, { passive: true });

    window.addEventListener('resize', function () {
      _chat.renderedFirst = -1; renderVisible();
    });

    // ── Scroll-to-top / scroll-to-bottom FABs ──────────────────────
    // The FABs are anchored to the chatArea viewport (CSS position:
    // fixed) so they stay in place during scroll.  We toggle a .show
    // class based on scroll position: top FAB visible whenever we're
    // away from the very top, bottom FAB visible whenever we're away
    // from the very bottom.  Click smoothly scrolls to the requested
    // edge — uses scrollTo with behavior:'auto' so it's instant on
    // huge chats (1400+ msgs) instead of janky over many seconds.
    var topFab = document.getElementById('scrollTopFab');
    var botFab = document.getElementById('scrollBottomFab');
    if (topFab && botFab) {
      topFab.addEventListener('click', function () {
        var area2 = document.getElementById('chatArea');
        area2.scrollTop = 0;
      });
      botFab.addEventListener('click', function () {
        var area2 = document.getElementById('chatArea');
        area2.scrollTop = area2.scrollHeight;
      });
      // Update visibility on scroll
      var updateFabs = function () {
        var area2 = document.getElementById('chatArea');
        if (!area2 || area2.style.display === 'none') {
          topFab.classList.remove('show');
          botFab.classList.remove('show');
          return;
        }
        var st = area2.scrollTop;
        var max = area2.scrollHeight - area2.clientHeight;
        // Show top FAB after scrolling 200 px down (don't clutter the
        // top when there's nothing to jump back to).
        if (st > 200) topFab.classList.add('show');
        else topFab.classList.remove('show');
        // Show bottom FAB when not at the bottom.
        if (max - st > 200) botFab.classList.add('show');
        else botFab.classList.remove('show');
      };
      area.addEventListener('scroll', updateFabs, { passive: true });
      // Refresh after conversation loads
      window.__updateScrollFabs = updateFabs;
    }

    document.getElementById('sidebarSearchInput').addEventListener('input', function (e) {
      renderSidebar(e.target.value);
    });

    document.getElementById('themeToggle').addEventListener('click', toggleTheme);
    document.getElementById('openSearchBtn').addEventListener('click', openSearchPalette);

    document.getElementById('paletteInput').addEventListener('input', function (e) {
      clearTimeout(this._t);
      var v = e.target.value;
      this._t = setTimeout(function () { runSearch(v); }, 180);
    });
    document.getElementById('searchPalette').addEventListener('click', function (e) {
      if (e.target === this) closeSearchPalette();
    });

    document.addEventListener('keydown', function (e) {
      var lbOpen = document.getElementById('lightbox').classList.contains('visible');
      if (lbOpen) {
        if (e.key === 'Escape') { closeLightbox(); e.preventDefault(); return; }
        if (e.key === 'ArrowLeft') { lbNav(-1); e.preventDefault(); return; }
        if (e.key === 'ArrowRight') { lbNav(+1); e.preventDefault(); return; }
        if (e.key === '+' || e.key === '=') { zoomBy(1.25); e.preventDefault(); return; }
        if (e.key === '-' || e.key === '_') { zoomBy(0.8); e.preventDefault(); return; }
        if (e.key === '0') { resetZoom(); e.preventDefault(); return; }
      }
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault(); openSearchPalette();
      } else if (e.key === 'Escape') {
        closeSearchPalette(); closeForensicPanel();
      } else if (handleChatKeyboardScroll(e)) {
        return;
      } else if (e.key === '/' && document.activeElement.tagName !== 'INPUT' &&
                 document.activeElement.tagName !== 'TEXTAREA') {
        e.preventDefault(); openSearchPalette();
      }
    });

    // Close lightbox when clicking outside media
    document.getElementById('lightbox').addEventListener('click', function (e) {
      if (e.target === this || e.target.id === 'lbStage') closeLightbox();
    });
    wireLightbox();
    wireMediaClicks();

    // Delegate clicks inside chat area: info button + reaction/receipt/call expanders
    document.getElementById('chatArea').addEventListener('click', function (e) {
      // Vcard Save button - download a real .vcf file built from the
      // contact's name + phones.  Done here in the delegate so cached
      // tile HTML works without per-render handlers.
      var vcBtn = e.target.closest('.vcard-dl-btn');
      if (vcBtn) {
        e.stopPropagation();
        var vname = vcBtn.getAttribute('data-vcard-name') || 'Contact';
        var phones = (vcBtn.getAttribute('data-vcard-phones') || '').split('|').filter(Boolean);
        var lines = ['BEGIN:VCARD', 'VERSION:3.0', 'FN:' + vname, 'N:' + vname + ';;;;'];
        phones.forEach(function (p) {
          lines.push('TEL;TYPE=CELL:' + p);
        });
        lines.push('END:VCARD');
        var blob = new Blob([lines.join('\r\n') + '\r\n'], { type: 'text/vcard' });
        var url = URL.createObjectURL(blob);
        var a = document.createElement('a');
        a.href = url;
        a.download = vname.replace(/[^a-zA-Z0-9_\-]+/g, '_') + '.vcf';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
        return;
      }
      var btn = e.target.closest('.msg-info-btn');
      if (btn) {
        e.stopPropagation();
        // Resolve the message via the parent .msg's data-msg (msg.id),
        // looking it up through the byId index. data-info-idx is gone
        // because cached HTML can't carry a fresh idx without regex.
        var msgEl = btn.closest('[data-msg]');
        var msgId = msgEl && msgEl.getAttribute('data-msg');
        var idx = (msgId && _chat.byId) ? _chat.byId[msgId] : -1;
        if (idx >= 0 && _chat.messages[idx]) openForensicPanel(_chat.messages[idx]);
        return;
      }
      var rxGroup = e.target.closest('.reactions[data-rx]');
      if (rxGroup) {
        var rxIdx = rxGroup.getAttribute('data-rx');
        var body = document.querySelector('[data-rx-body="' + rxIdx + '"]');
        if (body) body.classList.toggle('open');
        return;
      }
      var rcptBar = e.target.closest('.receipts-bar[data-rcpt]');
      if (rcptBar) {
        var rcIdx = rcptBar.getAttribute('data-rcpt');
        var rcBody = document.querySelector('[data-rcpt-body="' + rcIdx + '"]');
        if (rcBody) rcBody.classList.toggle('open');
        return;
      }
      var callToggle = e.target.closest('.call-expand-link[data-call-toggle]');
      if (callToggle) {
        var ci = callToggle.getAttribute('data-call-toggle');
        var cList = document.querySelector('[data-call-list="' + ci + '"]');
        if (cList) {
          var nowOpen = !cList.classList.contains('open');
          cList.classList.toggle('open', nowOpen);
          callToggle.innerHTML = (nowOpen ? 'Hide participants &#x25B4;' :
            'Show ' + cList.querySelectorAll('.call-part-row').length +
              ' participants &#x25BE;');
        }
        return;
      }
      // Quote-reply click OR ghost-source jump-link → navigate to
      // the parent/source message.  Selector is now any element with
      // data-quote-parent so the same click-handler covers both quote
      // pills and ghost-card 'View source quote' anchors.
      var quoteEl = e.target.closest('[data-quote-parent]');
      if (quoteEl) {
        var parentId = quoteEl.getAttribute('data-quote-parent');
        if (parentId) scrollToMessageId(parentId);
        return;
      }
    });
    document.getElementById('fpClose').addEventListener('click', closeForensicPanel);
    document.getElementById('forensicPanel').addEventListener('click', function (e) {
      if (e.target === this) closeForensicPanel();
    });

    window.addEventListener('hashchange', handleRoute);
    _buildConvIndex();   // populate the convId → conv lookup once
    renderSidebar('');
    handleRoute();

    // Manifest meta
    var m = window.__MANIFEST;
    var meta = document.getElementById('manifestMeta');
    if (meta && m) {
      meta.textContent = (m.conversations.length || 0) + ' chats \u00B7 ' +
        (m.totalMessages || '?') + ' msgs' +
        (m.exportedAt ? ' \u00B7 exported ' + fmtDate(m.exportedAt) : '');
    }

    // \u2500\u2500 Tagged-messages tab + sidebar list \u2500\u2500
    // Only shown for tagged-message exports.  Lets the analyst see
    // every tagged message at a glance and click to jump straight to
    // it inside the right conversation.
    setupTaggedTab();

    // \u2500\u2500 Per-conversation in-chat search \u2500\u2500
    setupConvSearch();

    // \u2500\u2500 Case-info card on the empty / home state \u2500\u2500
    renderEmptyCaseCard();
  });

  // ------------------------------------------------------------------
  // Tagged messages \u2014 sidebar tab + click-to-jump
  // ------------------------------------------------------------------
  function setupTaggedTab() {
    var m = window.__MANIFEST || {};
    var tagged = m.taggedMessages || [];
    var tabTagged = document.getElementById('tabTagged');
    if (!tabTagged) return;
    if (!m.isTaggedExport || !tagged.length) {
      tabTagged.style.display = 'none';
      return;
    }
    tabTagged.style.display = '';
    var cnt = document.getElementById('taggedCount');
    if (cnt) cnt.textContent = '(' + tagged.length + ')';

    // Tab switching
    var tabs = document.querySelectorAll('#sidebarTabs .tab');
    tabs.forEach(function (t) {
      t.addEventListener('click', function () {
        tabs.forEach(function (x) { x.classList.remove('active'); });
        t.classList.add('active');
        var which = t.getAttribute('data-tab');
        var convList = document.getElementById('convList');
        var tagList = document.getElementById('taggedList');
        var search = document.getElementById('sidebarSearchInput');
        if (which === 'tagged') {
          convList.style.display = 'none';
          tagList.style.display = '';
          search.placeholder = 'Filter tagged messages\u2026';
          renderTaggedList(search.value);
        } else {
          convList.style.display = '';
          tagList.style.display = 'none';
          search.placeholder = 'Filter conversations\u2026';
          renderSidebar(search.value);
        }
      });
    });

    // Hook into the existing search input so it also filters tagged list
    var searchEl = document.getElementById('sidebarSearchInput');
    if (searchEl) {
      searchEl.addEventListener('input', function (e) {
        var activeTab = document.querySelector('#sidebarTabs .tab.active');
        if (activeTab && activeTab.getAttribute('data-tab') === 'tagged') {
          renderTaggedList(e.target.value);
        }
      });
    }
  }

  function renderTaggedList(filter) {
    var list = document.getElementById('taggedList');
    if (!list) return;
    var tagged = (window.__MANIFEST || {}).taggedMessages || [];
    var convsById = {};
    (window.__MANIFEST.conversations || []).forEach(function (c) {
      convsById[c.id] = c;
    });
    var f = (filter || '').trim().toLowerCase();
    list.innerHTML = '';
    tagged.forEach(function (t) {
      var hay = ((t.preview || '') + ' ' + (t.tag || '') + ' ' +
                 (t.note || '') + ' ' + (t.sender || '') + ' ' +
                 ((convsById[t.convId] || {}).title || '')).toLowerCase();
      if (f && hay.indexOf(f) < 0) return;
      var conv = convsById[t.convId] || {};
      var preview = (t.preview || '').trim() || '[' + (typeNameForCode(t.type) || 'media') + ']';
      var item = document.createElement('div');
      item.className = 'tagged-item';
      item.innerHTML =
        '<div class="tagged-row1">' +
          '<span class="tagged-conv">' + esc(conv.title || '?') + '</span>' +
          '<span class="tagged-ts">' + esc(fmtRelTime(t.ts)) + '</span>' +
        '</div>' +
        (t.tag ?
          '<div class="tagged-tag">\uD83C\uDFF7 ' + esc(t.tag) + '</div>' : '') +
        '<div class="tagged-sender">' + esc(t.sender || '') + '</div>' +
        '<div class="tagged-preview">' + esc(preview.length > 100 ? preview.slice(0, 100) + '\u2026' : preview) + '</div>' +
        (t.note ?
          '<div class="tagged-note">\uD83D\uDCDD ' + esc(t.note) + '</div>' : '');
      item.addEventListener('click', function () {
        location.hash = '#/c/' + t.convId + '/m/' + t.msgId;
      });
      list.appendChild(item);
    });
    if (!list.children.length) {
      list.innerHTML =
        '<div style="padding:24px 12px;text-align:center;color:var(--text-meta);font-size:12px;">' +
        (f ? 'No tagged messages matching the filter.' : 'No tagged messages.') +
        '</div>';
    }
  }

  // Tiny helper used by the tagged sidebar's preview fallback
  function typeNameForCode(t) {
    var map = { 0: 'text', 1: 'image', 2: 'audio', 3: 'video',
                5: 'location', 7: 'system', 9: 'document', 13: 'gif',
                16: 'live location', 20: 'sticker', 90: 'call' };
    return map[t] || '';
  }

  // ------------------------------------------------------------------
  // Per-conversation in-chat search (filters / highlights / jumps)
  // ------------------------------------------------------------------
  var _convSearch = { matches: [], cursor: -1, query: '' };

  function setupConvSearch() {
    var bar = document.getElementById('convSearchBar');
    var input = document.getElementById('convSearchInput');
    var prev = document.getElementById('convSearchPrev');
    var next = document.getElementById('convSearchNext');
    var close = document.getElementById('convSearchClose');
    if (!bar || !input) return;

    function open() {
      bar.style.display = 'flex';
      input.focus();
      input.select();
    }
    function closeBar() {
      bar.style.display = 'none';
      _convSearch = { matches: [], cursor: -1, query: '' };
      // Clear highlights from rendered cells
      document.querySelectorAll('#messages .conv-search-hit, #messages .conv-search-current')
        .forEach(function (el) {
          el.classList.remove('conv-search-hit', 'conv-search-current');
        });
      input.value = '';
      var c = document.getElementById('convSearchCount');
      if (c) c.textContent = '';
    }

    // Open with Ctrl+F when a conversation is open
    document.addEventListener('keydown', function (e) {
      if ((e.ctrlKey || e.metaKey) && e.key && e.key.toLowerCase() === 'f' && _activeConvId) {
        e.preventDefault();
        open();
      } else if (e.key === 'Escape' && bar.style.display !== 'none') {
        closeBar();
      }
    });

    input.addEventListener('input', function () {
      runConvSearch(input.value);
    });
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') {
        e.preventDefault();
        if (e.shiftKey) navConvMatch(-1);
        else navConvMatch(1);
      }
    });
    if (prev) prev.addEventListener('click', function () { navConvMatch(-1); });
    if (next) next.addEventListener('click', function () { navConvMatch(1); });
    if (close) close.addEventListener('click', closeBar);

    // Expose a tiny helper so the chat header can wire its own
    // search button to open this bar.
    window.openConvSearch = open;
  }

  function runConvSearch(q) {
    q = (q || '').trim().toLowerCase();
    _convSearch.query = q;
    _convSearch.matches = [];
    _convSearch.cursor = -1;
    if (!q || !_chat || !_chat.messages) {
      var c = document.getElementById('convSearchCount');
      if (c) c.textContent = '';
      return;
    }
    for (var i = 0; i < _chat.messages.length; i++) {
      var m = _chat.messages[i];
      var hay = (m.text || '') + ' ' +
                (m.system_text || '') + ' ' +
                (m.senderName || '') + ' ' +
                ((m.media && m.media.caption) || '') + ' ' +
                ((m.media && m.media.name) || '');
      if (hay.toLowerCase().indexOf(q) >= 0) {
        _convSearch.matches.push(i);
      }
    }
    var cnt = document.getElementById('convSearchCount');
    if (cnt) {
      cnt.textContent = _convSearch.matches.length
        ? '0 / ' + _convSearch.matches.length
        : 'no match';
    }
    if (_convSearch.matches.length) {
      navConvMatch(1);
    }
  }

  function navConvMatch(direction) {
    if (!_convSearch.matches.length) return;
    var n = _convSearch.matches.length;
    if (_convSearch.cursor < 0) {
      _convSearch.cursor = direction > 0 ? 0 : n - 1;
    } else {
      _convSearch.cursor = (_convSearch.cursor + direction + n) % n;
    }
    var idx = _convSearch.matches[_convSearch.cursor];
    var msg = _chat.messages[idx];
    if (msg && msg.id != null && typeof scrollToMessageId === 'function') {
      scrollToMessageId(msg.id);
    }
    // Update counter
    var cnt = document.getElementById('convSearchCount');
    if (cnt) cnt.textContent = (_convSearch.cursor + 1) + ' / ' + n;
    // Highlight current match after scroll settles
    setTimeout(function () {
      document.querySelectorAll('#messages .conv-search-current')
        .forEach(function (el) { el.classList.remove('conv-search-current'); });
      var sel = '[data-msg="' + (msg.id != null ? String(msg.id).replace(/"/g, '\\"') : '') + '"]';
      var el = document.querySelector('#messages ' + sel);
      if (el) el.classList.add('conv-search-current');
    }, 80);
  }

  // ------------------------------------------------------------------
  // Empty / home state \u2014 case info card
  // ------------------------------------------------------------------
  function renderEmptyCaseCard() {
    var card = document.getElementById('emptyCaseCard');
    if (!card) return;
    var m = window.__MANIFEST || {};
    var ci = m.caseInfo || {};
    var rows = [];
    function add(label, val) {
      if (!val) return;
      rows.push('<tr><td>' + esc(label) + '</td><td><strong>' + esc(val) + '</strong></td></tr>');
    }
    add('Case ID', ci.case_id);
    add('Examiner', ci.examiner);
    add('Case Created', ci.created);
    add('Notes', ci.notes);
    add('Source msgstore.db', ci.source_msgstore || ci.analysis_db);
    if (m.exportedAt) {
      try {
        add('Bundle exported', new Date(m.exportedAt).toLocaleString());
      } catch (e) {}
    }
    add('Conversations', (m.conversations || []).length.toLocaleString());
    add('Total messages', (m.totalMessages || 0).toLocaleString());
    if (m.isTaggedExport && (m.taggedMessages || []).length) {
      add('Tagged messages', (m.taggedMessages || []).length.toLocaleString());
    }
    if (!rows.length) {
      card.style.display = 'none';
      return;
    }
    card.innerHTML =
      '<div class="ec-title">\uD83D\uDCCB Case &amp; export details</div>' +
      '<table class="ec-table">' + rows.join('') + '</table>';
    card.style.display = '';
  }
})();
