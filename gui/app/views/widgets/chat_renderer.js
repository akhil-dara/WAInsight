/**
 * WAInsight — Chat Renderer v8
 *
 * PRODUCTION VIRTUAL SCROLL — Based on Telegram Web K / Element BACAT / TanStack Virtual
 *
 * Architecture:
 * - HeightMap: prefix-sum array with per-type estimates + measured cache
 * - Binary search: O(log n) position lookups for any message index
 * - Manual scroll anchoring: save anchor ID+offset → mutate DOM → restore
 * - CSS overflow-anchor: none (disabled — we manage anchoring ourselves)
 * - ResizeObserver: single instance watches all rendered messages
 * - Scroll-idle tile loading: tiles only requested after scroll settles
 * - Pre-sized media: width/height/min-height set on all images before load
 * - scrollToMessage: iterative correction (1-3 passes) for pixel-perfect jumps
 */

// ---- Constants ----
var TILE_SIZE = 100;       // Messages per tile (matches Python BATCH_SIZE) — smaller = less DOM churn per swap
var MAX_DATA = 30000;      // Max messages in memory before eviction (raised for preload-all mode)
var BUFFER = 25;           // Extra messages above/below viewport. Larger values cause
                           // scroll drift during fast scroll (reconciliation overshoot) —
                           // keep conservative and trust the browser's own scroll anchoring.
var SCROLL_IDLE_MS = 80;   // ms of scroll inactivity before loading tiles
var PREFETCH_TILES = 3;    // Number of tiles to prefetch ahead of scroll direction

// FLAT RENDER MODE (simple): below this total, render EVERY .msg element at
// once. Browser handles scrolling natively.  For small chats (≤ 5K) this is
// trivially fast and architecturally can't produce blank frames.
var FLAT_RENDER_MAX = 5000;

// WINDOWED FLAT for large chats: keep a sliding window of ~WINDOW_SIZE real
// .msg elements around the viewport, with top/bottom SPACERS that represent
// the unrendered regions at a constant AVG_MSG_H px each. scrollContent's
// total height = totalCount * AVG_MSG_H, so scrollTop maps predictably to
// global index (scrollTop / AVG_MSG_H). When the viewport drifts beyond the
// window, we re-render the window centered on the new viewport index.
//
// Rationale: the simple "render all 285K .msg elements" approach made initial
// paint + subsequent scrolling sluggish (Chrome was maintaining 285K DOM
// nodes). Windowed keeps DOM tiny (~600 nodes) so everything stays snappy,
// while spacers preserve the browser's native scroll range.
var AVG_MSG_H = 48;
// Window = ±250 around the viewport centre (500 total in DOM). User asked for
// this so scrolling never hits a tombstone inside the visible area.
var WINDOW_SIZE = 500;
// Shift window when viewport centre drifts this far from window centre.
// Must be < WINDOW_SIZE/2 so we shift BEFORE viewport reaches window edge.
// 200 here leaves 50 msgs of "already-loaded" buffer on the approaching side
// at the moment we shift (re-renders around the new centre).  Bumped from
// 150→200 to dampen the "scrollbar drag thrashes the window" pattern the
// user hit on a 47k-msg chat: every wheel tick was triggering a shift,
// and the in-flight tile patches couldn't keep up.
var SHIFT_THRESHOLD = 200;
// Prefetch radius: request tiles covering WINDOW ± this many msgs on each
// side. Means tiles are fetched BEFORE the user scrolls there, so window
// shifts render real content instantly (no tombstone flash).
var PREFETCH_RADIUS = 500;
// Cooldown after a shift — prevents the oscillation the user described as
// "dragged the scrollbar, released, and it keeps moving up and down".
// A shift is an innerHTML replace; the resulting layout adjustments cause
// scrollTop to settle over a frame or two, and if another shift fires
// during that window the scroll thrashes.  Bumped from 250→400 ms after
// the user reported a long-chat case where rapid scroll triggered five
// successive far-off shifts (window=[47k] → [0] → [45k] → [43k] → [26k])
// and the screen stayed blank because tile patches couldn't keep up.
var SHIFT_COOLDOWN_MS = 400;
// Maximum time we'll honour the active scroll-derived window before
// forcing a DOM-recalibration sweep.  Catches the edge case where the
// window has shifted but RO measurements have changed enough heights
// that the "AVG_MSG_H estimate" is now off by hundreds of indices.
var FORCE_RECAL_MS = 1500;
var _lastWindowShiftAt = 0;
// Tracks the last time the user performed a REAL input (wheel/mousedown/
// touchstart/keydown) on the scroller. Overflow-anchor auto-adjustments
// during tile patches ALSO fire scroll events, and those are NOT the user
// scrolling — we must not cancel bottom-stick or scroll-settle based on
// those. We only interpret a scroll event as "user scrolled away" when
// this timestamp is recent (< 500 ms ago).
var _lastUserInputAt = 0;

var _flatRender = false;     // true when this chat uses flat (small)
var _flatWindowed = false;   // true when this chat uses windowed-flat (large)
var _flatRendered = false;   // true after first paint
var _windowFirst = -1;
var _windowLast = -1;

// ---- State ----
var totalCount = 0;
var isGroup = false;
var bridge = null;
var ownerLabel = '';
var _loadGeneration = 0;  // Python conversation generation; rejects stale JS calls

// Tile-based data store
var tileMap = {};          // tileIndex → array of messages (sparse)
var tileAccessOrder = [];  // LRU order
var totalDataCount = 0;

// Lookup indices
var idToGlobal = {};
var keyToGlobal = {};

// ---- Scroll-position calibration anchor ----
// Set by scrollToMessage after we've successfully landed on a message.
// Used by _wFlatMaybeShift to convert future scrollTop deltas into an
// accurate global-index prediction WITHOUT re-using the inaccurate
// `vpCenter / AVG_MSG_H` formula (which compounds estimate error and
// makes the window shift to a wrong center as soon as the user starts
// scrolling — the "go to message → scroll → jumps 200-300 indices"
// bug).  When this anchor is fresh, _wFlatMaybeShift uses
//   centerGi = anchorGi + (currentScrollTop - anchorTop) / AVG_MSG_H
// so the absolute calibration error cancels out and only the relative
// scroll motion drives the prediction — matching what the user sees.
var _scrollAnchorGi = null;     // global index we last scrolled to
var _scrollAnchorTop = 0;       // chatArea.scrollTop AFTER landing
var _scrollAnchorAt = 0;        // Date.now() — used to expire the anchor
var SCROLL_ANCHOR_TTL_MS = 30000;
// Big jump invalidation: if user scrolls more than ANCHOR_INVALIDATE_PX
// from the anchor (way past the rendered window), discard it — at that
// point we're far from the original landing point and the absolute
// formula is fine.
var ANCHOR_INVALIDATE_PX = 200000;

// Pending scroll-to-message: set when scrollToMessage is called but the
// target msg_id isn't loaded yet. Cleared on success or clearMessages.
var _pendingScrollMsgId = null;

// ---- HeightMap: prefix-sum with per-type estimates + measured cache ----
// Per-type height estimates (pixels) — used for unmeasured messages
var TYPE_HEIGHT = {
  text: 52, image: 280, video: 300, gif: 260, animated_gif: 260,
  sticker: 180, voice: 72, audio: 72, document: 90,
  location: 140, live_location: 140, poll: 320, poll_vote: 60,
  vcard: 100, vcard_list: 120, call_log: 100, album: 300,
  view_once_image: 120, view_once_video: 120, view_once_voice: 72,
  ai_message: 200, newsletter: 80, system: 40, '': 52
};
var DEFAULT_EST_H = 64;    // fallback for unknown types
var measuredHeights = {};   // globalIndex → measured px height
var heightPositions = null; // Float64Array: prefix-sum positions[i] = start offset of item i
var heightTotal = 0;        // total scroll height
var heightDirty = true;     // needs rebuild?

// Estimate-to-reality calibration. Raw type-based estimates assume 52-64 px
// text messages, but real chats average 40-50 px when users send mostly
// short replies. Over 90K messages that 20 % gap compounds to ~800 K pixels,
// making `scrollContent.style.height` (= heightTotal) disagree with the
// actual bottom of rendered content. Then findItemAtOffset(scrollTop near
// bottom) returns the wrong global index and scrollToMessage renders the
// wrong range. `calibrateEstimateScale()` adjusts this based on measured
// heights vs raw estimates and rebuilds the prefix-sum globally.
var _estScale = 1.0;

function estimateHeight(gi) {
  if (measuredHeights[gi] !== undefined) return measuredHeights[gi];
  var msg = getMsg(gi);
  if (!msg) return Math.round(DEFAULT_EST_H * _estScale);
  // Album children are hidden (rendered by parent) — zero height
  if (msg.album_parent_id) return 0;
  var tl = msg.type_label || '';
  var base = TYPE_HEIGHT[tl] !== undefined ? TYPE_HEIGHT[tl] : DEFAULT_EST_H;
  // Adjust for quoted messages
  if (msg.quoted_row_id || msg.quoted_text) base += 50;
  // Adjust for text length
  if (msg.text && msg.text.length > 200) base += Math.min(120, Math.floor(msg.text.length / 50) * 10);
  // Adjust for known media dimensions
  var mw = msg.media_w || msg.media_width || 0;
  var mh = msg.media_h || msg.media_height || 0;
  if (mw > 0 && mh > 0) {
    var scale = Math.min(1, 330 / mw);
    base = Math.max(base, Math.min(330, Math.round(mh * scale)) + 60);
  }
  return Math.max(20, Math.round(base * _estScale));
}

// Recompute _estScale from currently-measured messages. Returns true if
// the scale changed enough to warrant a full rebuildHeights.
function calibrateEstimateScale() {
  var measuredCount = 0;
  var rawEstSum = 0;
  var measuredSum = 0;
  for (var giStr in measuredHeights) {
    var gi = parseInt(giStr, 10);
    if (isNaN(gi) || gi < 0 || gi >= totalCount) continue;
    var actual = measuredHeights[gi];
    if (!(actual > 0)) continue;
    // Compute the RAW estimate (what estimateHeight WOULD return without
    // the measurement override AND without _estScale) — so we can solve
    // for the scale that best fits observations.
    var msg = getMsg(gi);
    if (!msg || msg.album_parent_id) continue;
    var tl = msg.type_label || '';
    var base = TYPE_HEIGHT[tl] !== undefined ? TYPE_HEIGHT[tl] : DEFAULT_EST_H;
    if (msg.quoted_row_id || msg.quoted_text) base += 50;
    if (msg.text && msg.text.length > 200) base += Math.min(120, Math.floor(msg.text.length / 50) * 10);
    var mw = msg.media_w || msg.media_width || 0;
    var mh = msg.media_h || msg.media_height || 0;
    if (mw > 0 && mh > 0) {
      var sc = Math.min(1, 330 / mw);
      base = Math.max(base, Math.min(330, Math.round(mh * sc)) + 60);
    }
    rawEstSum += base;
    measuredSum += actual;
    measuredCount++;
  }
  if (measuredCount < 10 || rawEstSum <= 0) return false;
  var newScale = measuredSum / rawEstSum;
  // Clamp to sane range — avoid runaway if a handful of very large/small
  // messages skew the sample.
  if (newScale < 0.4) newScale = 0.4;
  if (newScale > 1.8) newScale = 1.8;
  // Only update if the shift is meaningful (>5 %)
  if (Math.abs(newScale - _estScale) < 0.05) return false;
  _estScale = newScale;
  return true;
}

function rebuildHeights(fromIndex) {
  if (totalCount === 0) { heightTotal = 0; return; }
  if (!heightPositions || heightPositions.length < totalCount) {
    var newArr = new Float64Array(totalCount);
    if (heightPositions) {
      for (var c = 0; c < Math.min(heightPositions.length, totalCount); c++) newArr[c] = heightPositions[c];
    }
    heightPositions = newArr;
  }
  var start = fromIndex;
  if (start < 0) start = 0;
  var offset = start === 0 ? 0 : (heightPositions[start - 1] + estimateHeight(start - 1));
  for (var i = start; i < totalCount; i++) {
    heightPositions[i] = offset;
    offset += estimateHeight(i);
  }
  heightTotal = offset;
  heightDirty = false;
}

function getItemTop(gi) {
  if (!heightPositions || gi < 0 || gi >= totalCount) return 0;
  return heightPositions[gi];
}

function getItemHeight(gi) {
  return estimateHeight(gi);
}

// Binary search: find item index at a given scroll offset — O(log n)
function findItemAtOffset(scrollOffset) {
  if (!heightPositions || totalCount === 0) return 0;
  var lo = 0, hi = totalCount - 1;
  while (lo <= hi) {
    var mid = (lo + hi) >>> 1;
    var itemTop = heightPositions[mid];
    var itemBottom = itemTop + estimateHeight(mid);
    if (itemBottom <= scrollOffset) lo = mid + 1;
    else if (itemTop > scrollOffset) hi = mid - 1;
    else return mid;
  }
  return Math.max(0, Math.min(totalCount - 1, lo));
}

// Virtual scroll state
var renderedFirst = -1;
var renderedLast = -1;
var rafId = 0;
var initialScrollDone = false;
var initialScrollGuard = false;
var _stayAtBottom = false;      // Set by doScroll: tells ResizeObserver to re-snap immediately
var _stayAtBottomUntil = 0;     // Timestamp: _stayAtBottom active until this time
var _userScrolledAway = false;  // Set when user scrolls away from bottom during _stayAtBottom
var _suppressAnchor = false;    // Suppress scroll anchoring during inline expand/collapse (e.g. call participants)
var _suppressAnchorTimer = 0;   // Timer ID to auto-clear _suppressAnchor
// _programmaticScroll is a SELF-HEALING flag used to suppress scroll-handler
// side-effects during scrollIntoView / doScroll / scrollToMessage. It used to
// be a plain boolean, but the ~15 set-sites across doScroll / scrollToMessage
// / _flushResizeObserver / stability loops made it easy for a single missed
// clear (early return on an error path, race with a new conversation load,
// etc.) to leave the scroll handler EARLY-RETURNING FOREVER — renderVisible
// never ran, viewport went BLANK, user could scroll but nothing updated.
//
// The new implementation: call _beginProgScroll() to enter programmatic mode.
// It returns a "token" setTimeout that auto-expires after PROG_SCROLL_MAX_MS.
// Call _endProgScroll() to clear early. Reads go through _isProgScroll() which
// checks the expiration deadline. No state can leak past PROG_SCROLL_MAX_MS.
var PROG_SCROLL_MAX_MS = 1500;
var _progScrollUntil = 0;
function _beginProgScroll() { _progScrollUntil = Date.now() + PROG_SCROLL_MAX_MS; }
function _endProgScroll()   { _progScrollUntil = 0; }
function _isProgScroll()    { return Date.now() < _progScrollUntil; }

// Sticky highlight: the msg_id + deadline we want pulsing. renderVisible()
// re-applies .highlight-pulse to the live DOM element every render cycle
// so the pulse survives the 4-5 render waves that follow a scrollToMessage.
var _highlightTargetMsgId = null;
var _highlightUntil = 0;
// Scroll-settle: after a scrollToMessage call, keep the target centered for
// this long even if tile arrivals shift layout around it. Without this the
// "search go-to-msg went there, then scrolled back".
var _scrollSettleUntil = 0;
var _scrollSettleTargetId = null;
// Placement requested by the scroll-to-message that armed the watchdog.
// 'center' (default) keeps the target at ~1/3 from top; 'start' pins it
// at the very top of the viewport (used for first-unread auto-jump on
// chat open — without this, scroll-settle would re-center the unread
// divider every time an older tile arrives, which the user perceives as
// "the chat scrolled to a random place above the unread line").
var _scrollSettlePlacement = 'center';
// Bottom-stick: after chat open, keep scrollTop pinned to scrollHeight for
// this long even as tombstone→real patches above the viewport grow the
// scrollContent. overflow-anchor: auto handles this for in-place resizes,
// but our _flatPatchRange swaps entire nodes via replaceChild — Chrome drops
// the anchor across that. Explicit bottom-pin is the reliable fix.
// Cancelled as soon as user scrolls away from bottom (_userScrolledAway).
var _bottomStickUntil = 0;

// Scroll-idle tile loading
var scrollIdleTimer = 0;
var pendingTileRequests = {};
var lastScrollPrefetchTime = 0;
var SCROLL_PREFETCH_INTERVAL = 200;
var tileLoadingCount = 0;        // number of tiles currently being fetched
var MAX_CONCURRENT_TILES = 3;    // max tiles to fetch simultaneously
var tileRequestTimestamps = {};  // tile index -> request timestamp for stall detection
var TILE_STALL_MS = 3500;        // 3.5 s — tile considered stuck; slot freed so re-request can fire
var _tileWatchdogTimer = 0;      // periodic sweep regardless of user scroll

// Scroll-active tracking
var isUserScrolling = false;
var scrollActiveTimer = 0;
var SCROLL_ACTIVE_MS = 300;

// Audio state
var activeAudioId = 0;
var activeAudioProgress = 0;

// Return-to-reply navigation state
var _returnToMsgId = null;
var _returnBtnTimer = 0;

// Forensic provenance
var provenanceCache = {};

// Sticky date indicator state
var _stickyDateEl = null;
var _stickyDateTimer = 0;
var _lastStickyDate = '';
var pollVotersModal = document.getElementById('pollVotersModal');
var reactionsModal = document.getElementById('reactionsModal');
var forensicPanel = document.getElementById('forensicPanel');

// _scrollScale was intended to shrink scrollContent when heightTotal > 15 M px
// (Chromium's historical scroll limit) by dividing/multiplying every scrollTop
// read/write. It fought every DOM-native scroll API (`scrollIntoView`,
// ResizeObserver measurements, element.getBoundingClientRect) because those
// use PHYSICAL pixels while the _scrollScale system uses LOGICAL.
//
// Observed failure on 2026-04-14 at 285 K msgs: doScroll logged
// `scrollTop=22,190,547 remaining=-3,946,553` (negative) and the scroll area
// let the user scroll past the last message into a blank region. Disabling
// the scale entirely gives unified physical coordinates everywhere — modern
// Chromium desktop handles 30+ M px scroll containers without issue.
var _scrollScale = 1.0;
var CHROMIUM_MAX_HEIGHT = Number.POSITIVE_INFINITY;

// Highest "real rendered bottom" ever observed while the last message was in
// the DOM. Used by _updateScrollHeight as a FLOOR so mid-scroll renders — which
// position the container at a different translateY and contain a different
// subset of messages — never shrink scrollContent below a size we've already
// proven is correct. Without this, scrollContent oscillates between the true
// end-of-chat bottom and a smaller "estimate-based" value, and scrollTop gets
// clamped short by 2-4K px when user scrolls up then back to end.
// Reset on clearMessages() and when totalCount shrinks.
var _maxRealBottom = 0;

function _getScrollTop() {
  if (!chatArea) return 0;
  return chatArea.scrollTop;
}

function _setScrollTop(val) {
  if (!chatArea) return;
  chatArea.scrollTop = val;
}

var scrollContent = document.getElementById('scrollContent');
var container = document.getElementById('messages');
var chatArea = document.getElementById('chatArea');

// CRITICAL: Disable browser scroll anchoring — we manage it manually
chatArea.style.overflowAnchor = 'none';

// ---- ResizeObserver: single instance for all rendered message elements ----
// Debounced to prevent cascade: collect changes, apply once after 200ms idle.
// During scroll: deferred to 600ms so it always fires AFTER scroll settles
// (SCROLL_ACTIVE_MS=300 + margin). During initial load: no anchor restore.
var _roTimer = 0;
var _roPending = {};  // globalIdx → height
var _lastScrollTime = 0;  // timestamp of last scroll event
var _resizeObserver = new ResizeObserver(function (entries) {
  for (var ei = 0; ei < entries.length; ei++) {
    var entry = entries[ei];
    var idx = parseInt(entry.target.dataset.globalIdx, 10);
    if (isNaN(idx)) continue;
    var h = entry.borderBoxSize && entry.borderBoxSize[0]
      ? entry.borderBoxSize[0].blockSize
      : entry.target.getBoundingClientRect().height;
    if (h > 0) _roPending[idx] = h;
  }
  clearTimeout(_roTimer);
  // Use longer delay when user is actively scrolling to avoid race with settle timer
  var delay = isUserScrolling ? 600 : 200;
  _roTimer = setTimeout(_flushResizeObserver, delay);
});

function _flushResizeObserver() {
  // FLAT / WINDOWED FLAT: just collect measurements, don't touch layout.
  if (_flatRender || _flatWindowed) {
    for (var idxF in _roPending) {
      var giF = parseInt(idxF, 10);
      var hF = _roPending[idxF];
      if (hF > 0) measuredHeights[giF] = hF;
    }
    _roPending = {};
    return;
  }
  // If _stayAtBottom is active (within timeout of initial scroll), process immediately — no deferral.
  // But respect _userScrolledAway — if user scrolled away, don't re-snap.
  var shouldStayBottom = _stayAtBottom && Date.now() < _stayAtBottomUntil && !_userScrolledAway;
  if (!shouldStayBottom) {
    var timeSinceScroll = Date.now() - _lastScrollTime;
    // Defer harder: the old 400 ms threshold tripped on every wheel-scroll
    // pause, firing _restoreScrollAnchor which felt like "scroll pushes back".
    if (isUserScrolling || timeSinceScroll < 1200) {
      clearTimeout(_roTimer);
      _roTimer = setTimeout(_flushResizeObserver, 400);
      return;
    }
    // Expire the flag
    if (_stayAtBottom && Date.now() >= _stayAtBottomUntil) { _stayAtBottom = false; _userScrolledAway = false; }
  }
  var minChanged = totalCount;
  var anyChanged = false;
  for (var idx in _roPending) {
    var gi = parseInt(idx, 10);
    var h = _roPending[idx];
    var old = measuredHeights[gi];
    if (old === undefined || Math.abs(old - h) > 2) {
      measuredHeights[gi] = h;
      anyChanged = true;
      if (gi < minChanged) minChanged = gi;
    }
  }
  _roPending = {};
  if (anyChanged) {
    var prevGuard = initialScrollGuard;
    initialScrollGuard = true;  // suppress scroll → requestVisibleTiles cascade
    // Save scroll anchor BEFORE height changes — after rebuildHeights the
    // browser may clamp scrollTop, corrupting getBoundingClientRect offsets.
    var _preAnchor = null;
    var _preWasAtBottom = false;
    if (!_suppressAnchor && !shouldStayBottom && initialScrollDone) {
      _preWasAtBottom = ((heightTotal - _getScrollTop()) - chatArea.clientHeight) < 40;
      _preAnchor = _findScrollAnchor();
    }
    rebuildHeights(minChanged);
    _updateScrollHeight();
    if (_suppressAnchor) {
      // During inline expand/collapse (call participants, polls, reactions) — don't touch scroll
      // Just let the browser handle the resize naturally
    } else if (shouldStayBottom) {
      // During initial load: silently keep viewport at the bottom as images load.
      _beginProgScroll();
      if (renderedFirst >= 0 && container.children.length > 0) {
        container.style.transform = 'translateY(' + (getItemTop(renderedFirst) * _scrollScale) + 'px)';
      }
      // scrollContent.offsetHeight is AUTHORITATIVE (#messages is absolute
      // positioned so it doesn't contribute to scroll bounds). Since
      // _updateScrollHeight() was already called above, this is in sync.
      var _endScroll = Math.max(0, scrollContent.offsetHeight - chatArea.clientHeight);
      _setScrollTop(_endScroll);
      _endProgScroll();
      if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }
    } else {
      // Normal scrolling: only re-anchor when the height change is SIGNIFICANT.
      // The old code called _restoreScrollAnchor on every measurement cycle,
      // which bumped scrollTop programmatically and produced the "scroll
      // down then bounces up" feeling the . With real image
      // loads firing ResizeObserver every few seconds, this happened often.
      _beginProgScroll();
      if (renderedFirst >= 0 && container.children.length > 0) {
        container.style.transform = 'translateY(' + (getItemTop(renderedFirst) * _scrollScale) + 'px)';
      }
      if (_preWasAtBottom && !isUserScrolling) {
        // _updateScrollHeight() was called above (in rebuildHeights branch),
        // so scrollContent now matches container bottom. Use its offsetHeight
        // as the authoritative max (browser scroll bound, since #messages is
        // position:absolute and doesn't contribute to it).
        var _endScroll2 = Math.max(0, scrollContent.offsetHeight - chatArea.clientHeight);
        _setScrollTop(_endScroll2);
      } else if (_preAnchor) {
        // Skip anchor-restore when the user is within one viewport of the
        // END of the conversation. Restoring here produces a "scroll
        // pushes back up" feel: the user tries to scroll toward the last
        // message, an image above finishes loading, and the anchor
        // restore yanks them back up by ~image-height pixels.
        var _nearBottom =
          (heightTotal - _getScrollTop()) - chatArea.clientHeight < chatArea.clientHeight;
        if (!_nearBottom) {
          // Only call _restoreScrollAnchor if the anchor element has moved
          // MORE than 25 px due to measurement changes.
          var _liveEl = container.querySelector(
            '[data-global-idx="' + _preAnchor.globalIdx + '"]'
          );
          if (_liveEl) {
            var _chatRect2 = chatArea.getBoundingClientRect();
            var _liveOff = _liveEl.getBoundingClientRect().top - _chatRect2.top;
            if (Math.abs(_liveOff - _preAnchor.offset) > 25) {
              _restoreScrollAnchor(_preAnchor);
            }
          }
        }
      }
      _endProgScroll();
      if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }
    }
    initialScrollGuard = prevGuard;
  }
}

// ---- Manual Scroll Anchoring (save/restore pattern) ----
function _findScrollAnchor() {
  if (!container.children.length) return null;
  var chatRect = chatArea.getBoundingClientRect();
  var children = container.children;
  for (var i = 0; i < children.length; i++) {
    var child = children[i];
    var rect = child.getBoundingClientRect();
    if (rect.bottom > chatRect.top) {
      return {
        globalIdx: parseInt(child.dataset.globalIdx, 10) || (renderedFirst + i),
        offset: (rect.top - chatRect.top) / _scrollScale
      };
    }
  }
  return null;
}

function _restoreScrollAnchor(anchor) {
  if (!anchor || totalCount === 0) return;
  var el = container.querySelector('[data-global-idx="' + anchor.globalIdx + '"]');
  if (el) {
    var chatRect = chatArea.getBoundingClientRect();
    var elRect = el.getBoundingClientRect();
    var currentOffset = (elRect.top - chatRect.top) / _scrollScale;
    _setScrollTop(_getScrollTop() + currentOffset - anchor.offset);
  } else {
    _setScrollTop(getItemTop(anchor.globalIdx) - anchor.offset);
  }
}

function _updateScrollHeight() {
  if (!scrollContent) return;
  // Single physical/logical coordinate system (see `_scrollScale` comment).
  var h = heightTotal;
  if (chatArea && h < chatArea.clientHeight) h = chatArea.clientHeight;

  if (container && container.children.length) {
    var _tm = (container.style.transform || '').match(/translateY\(([-\d.e+]+)/);
    var _ty = _tm ? parseFloat(_tm[1]) : 0;
    var _rb = _ty + container.offsetHeight;
    if (renderedLast === totalCount - 1 && _rb > 0) {
      // At end of chat: pin scrollContent to actual rendered bottom so
      // the viewport cannot scroll past the last message into blank space.
      //
      // NOTE: _rb varies with the current render state — if renderedFirst is
      // deeper into the chat (scroll was mid-chat when this render fired),
      // container.offsetHeight is smaller and _ty uses ESTIMATED positions
      // which differ from MEASURED (estimates are 5-10 % off vs. reality
      // for chats that mix short texts with tall receipt / link cards).
      // That makes _rb oscillate by 2-4 K px between fully-scrolled-to-bottom
      // and mid-scroll renders, so naively writing `h = _rb` SHRINKS
      // scrollContent below the true end-of-chat bottom, and scrollTop gets
      // clamped short.
      //
      // Fix: remember the HIGHEST _rb we've ever seen and use it as a floor.
      // Over the course of scrolling through the chat, measured heights
      // stabilise and _rb asymptotes to its true value — we never need to
      // shrink below that.
      if (_rb > _maxRealBottom) _maxRealBottom = _rb;
      h = Math.max(_maxRealBottom, chatArea ? chatArea.clientHeight : 0);
    } else if (_rb > h) {
      // Mid-chat: floor so container never extends past scrollContent
      h = _rb;
    }
  }

  // Second safety floor: if the last msg has ever been in the DOM for this
  // conversation (_maxRealBottom > 0), never let scrollContent go below that
  // value. Handles the transient render window where renderedLast briefly
  // drops off the last message (e.g. user scrolls rapidly up, renderVisible
  // re-evaluates with last tile not in the visible range yet).
  if (_maxRealBottom > 0 && h < _maxRealBottom) h = _maxRealBottom;

  scrollContent.style.height = h + 'px';
}


// Create sticky date indicator element
(function () {
  _stickyDateEl = document.createElement('div');
  _stickyDateEl.className = 'sticky-date';
  _stickyDateEl.style.opacity = '0';
  chatArea.parentNode.insertBefore(_stickyDateEl, chatArea);
})();

var COLORS = [
  '#06cf9c', '#53bdeb', '#e9a640', '#d450e6',
  '#ef5350', '#66bb6a', '#42a5f5', '#ff7043',
  '#ab47bc', '#26c6da', '#ec407a', '#7e57c2',
];

var MICON = {
  image: '\u{1F4F7}', video: '\u{1F3AC}', audio: '\u{1F3B5}', voice: '\u{1F399}',
  sticker: '\u{1F36D}', gif: '\u{1F3AC}', animated_gif: '\u{1F3AC}', document: '\u{1F4C4}', contact_card: '\u{1F465}',
  location: '\u{1F4CD}', vcard: '\u{1F464}', album: '\u{1F5BC}', '': '\u{1F4CE}'
};
// Map WhatsApp integer message types to label for quoted_type resolution.
// VERIFIED against msgstore.db cross-joins (2026-03-27):
// 42 = view_once_image (2596/2600 match message_view_once_media)
// 43 = view_once_video (313/314 match message_view_once_media)
// 66 = poll           (488/488 match message_poll)
// AI/bot identified by bot_message_info table, NOT by message_type
var QTYPE = {
  0: 'text', 1: 'image', 2: 'audio', 3: 'video', 4: 'vcard', 5: 'location', 7: 'system',
  9: 'document', 11: 'pending', 13: 'gif', 14: 'vcard', 16: 'live_location', 20: 'sticker',
  42: 'view_once_image', 43: 'view_once_video', 46: 'poll_vote', 64: 'admin_revoke', 66: 'poll',
  82: 'voice', 90: 'call_log', 92: 'event', 99: 'album', 112: 'system', 116: 'status'
};

// ---- Media dimension helper (prevents scroll jitter) ----
// Sets width/height attributes AND min-height style so the browser reserves space before image loads
function mediaDims(msg) {
  var w = msg.media_w || msg.media_width || 0;
  var h = msg.media_h || msg.media_height || 0;
  if (!w || !h) return ' style="min-height:150px"';  // Reserve space even without dimensions
  var maxW = 330;
  if (w > maxW) { h = Math.round(h * maxW / w); w = maxW; }
  if (h > 330) { w = Math.round(w * 330 / h); h = 330; }
  return ' width="' + w + '" height="' + h + '" style="min-height:' + h + 'px"';
}

// ---- HD/SD pair pill (shown on both members of a dual-quality send) ----
// WhatsApp's dual-quality send (msgstore message_association.
// association_type = 7 for video, 12 for image) creates two
// distinct message rows — both real messages forensically.  We
// surface a clear cross-reference pill on each bubble:
//   * SD bubble: "SD → HD #N"  (where N is the HD twin's msg id)
//   * HD bubble: "HD ← SD #M"
// Click jumps to the twin so the analyst can compare them.
// Tooltip gives the HD twin's on-disk / downloadable / missing
// state at a glance.
function hdPairBadge(msg, typeLabel) {
  if (!msg) return '';
  // BOTH members of an HD pair (msgstore message_association
  // type 7 / 12) are visible in the chat list now — each
  // bubble carries this clickable pill cross-referencing the
  // other member.  Click → scroll to the twin so the analyst
  // can flip between SD and HD without leaving the chat.
  if (msg.hd_pair_role === 'sd' && msg.hd_pair_twin_id) {
    var hdLoc = msg.hd_exists ? '✔ on disk'
              : (msg.hd_has_url && msg.hd_has_key) ? '⤓ downloadable'
              : '✕ missing';
    var _atype = (typeLabel === 'video' ? 'type 7' : 'type 12');
    return '<span class="hd-pair-badge hd-pair-sd" '
      + 'onclick="event.stopPropagation();scrollToMessage(' + msg.hd_pair_twin_id + ', false, \'center\');" '
      + 'title="WhatsApp dual-quality send (' + _atype + ').\nThis is the SD parent (reactions and replies live here).\nHD twin: msg #' + msg.hd_pair_twin_id + ' (' + hdLoc + ').\nClick to jump to the HD bubble.">'
      + 'SD &rarr; HD #' + msg.hd_pair_twin_id + '</span>';
  }
  if (msg.hd_pair_role === 'hd' && msg.hd_pair_twin_id) {
    var sdLoc = msg.sd_parent_exists ? '✔ on disk' : '✕ missing';
    var _atype2 = (typeLabel === 'video' ? 'type 7' : 'type 12');
    return '<span class="hd-pair-badge hd-pair-hd" '
      + 'onclick="event.stopPropagation();scrollToMessage(' + msg.hd_pair_twin_id + ', false, \'center\');" '
      + 'title="WhatsApp dual-quality send (' + _atype2 + ').\nThis is the HD pair (twin — same content as the SD parent at higher resolution).\nSD parent: msg #' + msg.hd_pair_twin_id + ' (' + sdLoc + ').\nClick to jump to the SD bubble.">'
      + 'HD &larr; SD #' + msg.hd_pair_twin_id + '</span>';
  }
  // Legacy fallback for older analysis.db rows that have only
  // the forward pointer (parent.hd_twin_msg_id) but no per-row
  // pair role label — informational only (no click target).
  if (msg.hd_msg_id) {
    return '<span class="hd-pair-badge" title="HD pair detected. Twin msg #' + msg.hd_msg_id + '.">HD pair</span>';
  }
  return '';
}

// "⤓ Download HD" pill — only on the SD parent, only when the
// HD twin's bytes aren't on disk but its CDN URL + decryption
// key are still valid.  HD upload is a *separate* CDN object
// from the SD upload, so SD-on-disk says nothing about HD
// availability.  bDl(<HD twin's msg_id>) routes the download
// through the existing media-download bridge.
function downloadHdBadge(msg) {
  if (!msg) return '';
  if (msg.hd_pair_role !== 'sd' || !msg.hd_pair_twin_id) return '';
  if (msg.hd_exists) return '';
  if (!msg.hd_has_url || !msg.hd_has_key) return '';
  // bDl's bridge slot is @Slot(str) — pass the HD twin's id
  // as a string so the type matches the existing download flow.
  return '<span class="dl-hd-badge" '
    + 'onclick="event.stopPropagation();bDl(\'' + msg.hd_pair_twin_id + '\');" '
    + 'title="HD twin (msg #' + msg.hd_pair_twin_id + ') is not on disk but its CDN URL + decryption key are still valid. Click to fetch the HD bytes.">'
    + '⤓ Download HD</span>';
}

// ---- HD/SD quality badge for images/videos ----
function qualityBadge(msg) {
  var w = msg.media_w || msg.media_width || 0;
  var h = msg.media_h || msg.media_height || 0;
  if (!w && !h) return '';
  if (w > 1600 || h > 1600) return '<span class="quality-badge quality-hd">HD</span>';
  if (w < 1000 && h < 1000 && w > 0 && h > 0) return '<span class="quality-badge quality-sd">SD</span>';
  return '';
}

// ---- Provenance badge for media bubbles ----
// Visually distinguishes the FIVE possible states of a media file so an
// analyst can scan the chat at a glance:
//   * Original  - file was on the device for THIS message (default for
//                 most receivable media; we don't render a badge in this
//                 case because it's the unsurprising state and would
//                 clutter the bubble).
//   * Downloaded - tool fetched the file post-extraction from CDN.
//   * Hash-linked - this message had no downloaded file; what's shown
//                   came from a DIFFERENT message with the same SHA-256.
//                   Forensically critical to flag - looks identical
//                   visually but is NOT proof of receipt for this msg.
//   * Downloadable - file not on disk but CDN URL + key are still valid.
//   * Missing - no file, no URL/key.  Rendered with the existing
//               "media missing" tile, no extra badge needed.
//
// Badges sit top-right (above the HD/SD badge which sits bottom-right,
// so they never collide).  Pointer-events:none so they don't intercept
// clicks meant for the media itself.
function provenanceBadge(msg) {
  if (!msg) return '';
  var rm = msg.recovery_method || '';
  if (rm === 'hash_linked_after_delete') {
    return '<span class="prov-badge prov-hash-deleted" title="Originally received in this chat but the local file was deleted later. Same SHA-256 still exists in another message; that’s what is shown.">⚠ Received & deleted</span>';
  }
  if (rm === 'orphan_recovered') {
    return '<span class="prov-badge prov-orphan" title="Rescued from an orphaned file on disk. The chat record was lost (cleared/reinstalled) but a file with the same SHA-256 was still in the WhatsApp media folder — that file is shown here.">💾 Rescued from disk</span>';
  }
  if (rm === 'hash_linked') {
    return '<span class="prov-badge prov-hash" title="Hash-linked: this message had no downloaded file — the displayed file is content-equivalent (same SHA-256) but came from a different message">🔗 Hash-linked</span>';
  }
  if (rm === 'downloaded') {
    return '<span class="prov-badge prov-downloaded" title="Recovered: tool downloaded this file from WhatsApp CDN after extraction">⬇ Recovered</span>';
  }
  // No recovery_method, but file exists -> original WhatsApp transfer.
  // Show only on heavyweight media (image/video) to avoid badge clutter
  // on every voice note / audio etc.
  if (msg.file_exists && (msg.type_label === 'image' || msg.type_label === 'video' ||
                          msg.type_label === 'gif' || msg.type_label === 'animated_gif')) {
    // Intentionally empty: original-and-on-disk is the unsurprising
    // case and a badge would clutter every photo.  The forensic-info
    // panel still surfaces it explicitly.
    return '';
  }
  if (!msg.file_exists && msg.has_url && msg.has_key) {
    return '<span class="prov-badge prov-downloadable" title="Not downloaded — CDN URL and decryption key are available">☁ On CDN</span>';
  }
  return '';
}

// Media load is handled by ResizeObserver — no separate handler needed.

// ---- Helpers ----
function esc(t) {
  if (!t) return '';
  var d = document.createElement('div');
  d.textContent = t;
  return d.innerHTML;
}
function waMarkdown(text) {
  text = text.replace(/```([\s\S]*?)```/g, '<code>$1</code>');
  text = text.replace(/(?<!\w)`([^`\n]+)`(?!\w)/g, '<code>$1</code>');
  text = text.replace(/(?<!\w)\*([^\*\n]+)\*(?!\w)/g, '<b>$1</b>');
  text = text.replace(/(?<!\w)_([^_\n]+)_(?!\w)/g, '<i>$1</i>');
  text = text.replace(/(?<!\w)~([^~\n]+)~(?!\w)/g, '<s>$1</s>');
  return text;
}
function linkify(t) {
  return t.replace(/(https?:\/\/[^\s<]+)/g, function (u) {
    return '<a href="#" onclick="bUrl(\'' + esc(u).replace(/'/g, "\\'") + '\');return false;">' + esc(u) + '</a>';
  });
}
function proc(raw) {
  if (!raw) return '';
  // Trim leading/trailing whitespace — some WhatsApp message types
  // (button_message / interactive card bodies) arrive with a leading
  // newline that CSS `white-space: pre-wrap` renders as an empty first
  // row, making the bubble look empty. Preserve inner newlines.
  raw = raw.replace(/^[\s\u00A0]+|[\s\u00A0]+$/g, '');
  if (!raw) return '';
  // IMPORTANT: waMarkdown BEFORE linkify — otherwise linkify inserts <a> tags
  // that break backtick matching, causing unclosed <code> tags that leak
  // monospace font into subsequent messages.
  return linkify(waMarkdown(esc(raw)));
}
// IANA timezone for chat timestamps.  Set by Python via setTimezone(name)
// at chat-open time and whenever the global setting changes.  Empty
// string means "use browser local" (matches old behaviour).
var _CASE_TZ = '';
function setTimezone(tz) {
  _CASE_TZ = tz || '';
  // Day dividers and bubble timestamps are baked into rendered HTML —
  // re-render the visible window so existing bubbles pick up the new
  // timezone immediately instead of waiting for a scroll-driven
  // re-render.
  try {
    if (typeof renderVisible === 'function') { renderedFirst = -1; renderVisible(); }
  } catch (e) { /* ignore — chat may not be loaded yet */ }
}

// Cached Intl.DateTimeFormat objects, keyed on the timezone string.
// Building one is non-trivial (~ms per call), so we don't want to
// re-build on every fmtFullTs / fmtDate.
var _tsFmtCache = {};
function _getTsFmt(tz) {
  var key = '__t__' + (tz || 'LOCAL');
  if (!_tsFmtCache[key]) {
    var opts = {
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
    };
    if (tz) opts.timeZone = tz;
    try { _tsFmtCache[key] = new Intl.DateTimeFormat('en-CA', opts); }
    catch (e) {
      // Bad tz name — fall back to browser local.
      delete opts.timeZone;
      _tsFmtCache[key] = new Intl.DateTimeFormat('en-CA', opts);
    }
  }
  return _tsFmtCache[key];
}
var _dateFmtCache = {};
function _getDateFmt(tz) {
  var key = '__d__' + (tz || 'LOCAL');
  if (!_dateFmtCache[key]) {
    var opts = { year: 'numeric', month: 'long', day: 'numeric' };
    if (tz) opts.timeZone = tz;
    try { _dateFmtCache[key] = new Intl.DateTimeFormat('en-US', opts); }
    catch (e) {
      delete opts.timeZone;
      _dateFmtCache[key] = new Intl.DateTimeFormat('en-US', opts);
    }
  }
  return _dateFmtCache[key];
}

function fmtFullTs(ts) {
  if (!ts) return '';
  var fmt = _getTsFmt(_CASE_TZ);
  var parts = fmt.formatToParts(new Date(ts));
  var get = function (t) {
    for (var i = 0; i < parts.length; i++) if (parts[i].type === t) return parts[i].value;
    return '';
  };
  var ms = (Math.floor(ts) % 1000).toString().padStart(3, '0');
  return get('year') + '-' + get('month') + '-' + get('day') + ' '
    + get('hour') + ':' + get('minute') + ':' + get('second') + '.' + ms;
}
function fmtDate(ts) {
  if (!ts) return '';
  return _getDateFmt(_CASE_TZ).format(new Date(ts));
}
function ticks(st, fm, msgId) {
  if (!fm) return '';
  var click = msgId ? ' onclick="bReceipt(\'' + msgId + '\')" style="cursor:pointer" title="Click for receipt details"' : '';
  if (st >= 13) return '<span class="ticks played"' + click + '>\u2713\u2713</span>'; // played (voice)
  if (st >= 6) return '<span class="ticks read"' + click + '>\u2713\u2713</span>';    // read
  if (st >= 5) return '<span class="ticks"' + click + '>\u2713\u2713</span>';         // delivered
  if (st >= 4) return '<span class="ticks"' + click + '>\u2713</span>';               // sent
  if (st === 2) return '<span class="ticks failed"' + click + '>\u26A0</span>';       // failed
  if (st === 0 || st === 1) return '<span class="ticks pending"' + click + '>\u{1F551}</span>'; // pending/clock
  return '';
}
function sColor(name) {
  var h = 0;
  for (var i = 0; i < name.length; i++) h = ((h << 5) - h + name.charCodeAt(i)) | 0;
  return COLORS[Math.abs(h) % COLORS.length];
}
function fmtDur(ms) {
  if (!ms || ms <= 0) return '0:00';
  var s = Math.floor(ms / 1000);
  return Math.floor(s / 60) + ':' + (s % 60).toString().padStart(2, '0');
}
function fmtSize(b) {
  if (!b || b <= 0) return '';
  if (b > 1048576) return (b / 1048576).toFixed(1) + ' MB';
  if (b > 1024) return Math.floor(b / 1024) + ' KB';
  return b + ' B';
}
function extractDom(url) {
  try {
    var u = url.indexOf('://') > -1 ? url.split('://')[1] : url;
    var d = u.split('/')[0].split('?')[0];
    if (d.startsWith('www.')) d = d.substring(4);
    return d;
  } catch (e) { return ''; }
}
function extractName(sender) {
  if (!sender) return '';
  var m = sender.match(/^(.+?)(?:\s*\([\+\d\s]+\))?$/);
  return m ? m[1].trim() : sender;
}
function avatarLetter(name) {
  if (!name) return '?';
  return name.replace(/^~/, '').charAt(0).toUpperCase();
}

// ---- Bridge helpers ----
function bUrl(u) { if (bridge) bridge.onUrlClick(u); }
function bQuote(k, srcId) { _returnToMsgId = srcId || null; if (bridge) bridge.onQuoteClick(k); }
function bMedia(p, id) { if (bridge) bridge.onMediaClick(JSON.stringify({ path: p, id: id })); }
function bSender(c) { if (bridge) bridge.onSenderClick(c); }
function bReceipt(msgId) { if (bridge) bridge.onReceiptDetail(parseInt(msgId)); }
function bMention(c) { if (bridge) bridge.onMentionClick(c); }
function bAudio(p, id) { if (bridge) bridge.onAudioClick(p, id); }
function bDl(id) { if (bridge) bridge.onDownloadClick(id); }
function bReact(id) { if (bridge) bridge.onReactionClick(id); }
function bVcardDl(msgId, name) { if (bridge) bridge.onVcardDownload(String(msgId), name); }
function bComments(msgId) { if (bridge) bridge.onCommentsClick(String(msgId)); }
function bEditHistory(msgId) { if (bridge) bridge.onEditHistoryClick(parseInt(msgId)); }
function bReplies(msgId, sourceKey) { if (bridge) bridge.onRepliesClick(parseInt(msgId), sourceKey || ''); }

// ---- Poll voter toggle (inline dropdown) ----
function togglePollVoters(el) {
  _suppressAnchor = true;
  clearTimeout(_suppressAnchorTimer);
  _suppressAnchorTimer = setTimeout(function () { _suppressAnchor = false; }, 600);
  var panel = el.querySelector('.poll-voter-panel');
  if (!panel) return;
  if (panel.style.display === 'none') {
    // Close any other open panels first
    document.querySelectorAll('.poll-voter-panel').forEach(function (p) {
      p.style.display = 'none'; p.style.maxHeight = '0';
    });
    panel.style.display = 'block';
    panel.style.maxHeight = (panel.scrollHeight + 10) + 'px';
    el.querySelector('.poll-opt-count').innerHTML =
      el.querySelector('.poll-opt-count').innerHTML.replace('\u25BC', '\u25B2');
  } else {
    panel.style.maxHeight = '0';
    setTimeout(function () { panel.style.display = 'none'; }, 200);
    el.querySelector('.poll-opt-count').innerHTML =
      el.querySelector('.poll-opt-count').innerHTML.replace('\u25B2', '\u25BC');
  }
}

// ---- Reaction detail toggle (inline dropdown) ----
function toggleReactionDetail(el) {
  _suppressAnchor = true;
  clearTimeout(_suppressAnchorTimer);
  _suppressAnchorTimer = setTimeout(function () { _suppressAnchor = false; }, 600);
  var panel = el.querySelector('.reaction-detail-panel');
  if (!panel) return;
  if (panel.style.display === 'none') {
    document.querySelectorAll('.reaction-detail-panel').forEach(function (p) {
      p.style.display = 'none'; p.style.maxHeight = '0';
    });
    panel.style.display = 'block';
    panel.style.maxHeight = (panel.scrollHeight + 10) + 'px';
  } else {
    panel.style.maxHeight = '0';
    setTimeout(function () { panel.style.display = 'none'; }, 200);
  }
}

// ---- Mentions ----
function renderMentions(text, mentionsStr) {
  if (!mentionsStr || !text) return proc(text);
  var mentions = mentionsStr.split(';;').filter(Boolean);
  var p = esc(text);
  for (var mi = 0; mi < mentions.length; mi++) {
    var parts = mentions[mi].split('::');
    var name = parts[0] || 'Unknown';
    var cid = parseInt(parts[1]) || 0;
    var phone = parts[2] || '';
    var lid = parts[3] || '';
    var dispName = parts[4] || '';
    // Handle bot mentions: display_name may be "Meta AI|867051314767696"
    var botNum = '';
    if (dispName && dispName.indexOf('|') >= 0) {
      var dp = dispName.split('|');
      dispName = dp[0];  // "Meta AI"
      botNum = dp[1];    // "867051314767696"
      if (!name || name === 'Unknown') name = dispName;
    }
    var label = phone ? name + ' (+' + phone + ')' : name;
    var tag = '<span class="mention" onclick="bMention(' + cid + ')">@' + esc(label) + '</span>';
    var replaced = false;
    if (phone) { var a = esc('@' + phone); if (p.includes(a)) { p = p.replace(a, tag); replaced = true; } }
    if (!replaced && lid) { var a = esc('@' + lid); if (p.includes(a)) { p = p.replace(a, tag); replaced = true; } }
    if (!replaced && botNum) { var a = esc('@' + botNum); if (p.includes(a)) { p = p.replace(a, tag); replaced = true; } }
    // Match full "Meta AI|867051314767696" pattern (display_name|botNum in raw text)
    if (!replaced && dispName && botNum) { var a = esc('@' + dispName + '|' + botNum); if (p.includes(a)) { p = p.replace(a, tag); replaced = true; } }
    if (!replaced && name && botNum) { var a = esc('@' + name + '|' + botNum); if (p.includes(a)) { p = p.replace(a, tag); replaced = true; } }
    if (!replaced) { var a = esc('@' + name); if (p.includes(a)) { p = p.replace(a, tag); replaced = true; } }
    if (!replaced && dispName && dispName !== name) { var a = esc('@' + dispName); if (p.includes(a)) { p = p.replace(a, tag); replaced = true; } }
  }
  return linkify(waMarkdown(p));
}

// ---- Waveform ----
function waveBars(id) {
  var h = '', s = id || 1;
  for (var i = 0; i < 30; i++) {
    var r = Math.abs(Math.sin(s * 7919 + i * 104729)) * 0.7 + 0.3;
    h += '<div class="bar" style="height:' + Math.round(r * 24) + 'px"></div>';
  }
  return h;
}

// ---- Link cards (WhatsApp-style: thumbnail on top, title+domain below) ----
function renderLinks(text, linkDetails, thumb) {
  if (!text || !linkDetails) return '';
  var urls = text.match(/https?:\/\/\S+/g);
  if (!urls) return '';
  var meta = {};
  linkDetails.split(';;').forEach(function (e) {
    var p = e.split('||');
    if (p.length >= 4) meta[p[1]] = { title: p[0], desc: p[2], domain: p[3] };
  });
  var h = '';
  urls.slice(0, 2).forEach(function (url, idx) {
    var m = meta[url];
    var dom = (m && m.domain) ? m.domain : extractDom(url);
    h += '<div class="link-card" onclick="bUrl(\'' + esc(url).replace(/'/g, "\\'") + '\')">';
    // Show thumbnail for first link if available
    if (idx === 0 && thumb) {
      h += '<div class="link-thumb"><img src="' + thumb + '" loading="lazy" /></div>';
    }
    h += '<div class="link-info">';
    if (dom) h += '<div class="link-domain">\uD83C\uDF10 ' + esc(dom) + '</div>';
    h += '<div class="link-title">' + esc((m && m.title) || url.substring(0, 80)) + '</div>';
    if (m && m.desc) h += '<div class="link-desc">' + esc(m.desc.length > 120 ? m.desc.substring(0, 120) + '...' : m.desc) + '</div>';
    h += '</div></div>';
  });
  return h;
}

// ================================================================
// TILE DATA MANAGEMENT
// ================================================================

function getMsg(globalIdx) {
  var ti = Math.floor(globalIdx / TILE_SIZE);
  var tile = tileMap[ti];
  if (!tile) return null;
  var li = globalIdx - (ti * TILE_SIZE);
  return (li >= 0 && li < tile.length) ? (tile[li] || null) : null;
}

var _tileAccessCounter = 0;
var _tileLastAccess = {};  // ti → access counter
function touchTile(ti) {
  _tileLastAccess[ti] = ++_tileAccessCounter;
}

function evictTiles() {
  if (totalDataCount <= MAX_DATA) return;
  // Find tiles with lowest access counter, evict oldest-accessed first
  var tileKeys = Object.keys(tileMap);
  if (tileKeys.length <= 5) return;
  tileKeys.sort(function (a, b) {
    return (_tileLastAccess[a] || 0) - (_tileLastAccess[b] || 0);
  });
  var ki = 0;
  while (totalDataCount > MAX_DATA && ki < tileKeys.length - 5) {
    var oldest = parseInt(tileKeys[ki], 10);
    if (tileMap[oldest]) {
      totalDataCount -= tileMap[oldest].length;
      delete tileMap[oldest];
      delete _tileLastAccess[oldest];
    }
    ki++;
  }
}

/**
 * SCROLL-IDLE TILE LOADING
 * Called after scroll settles (SCROLL_IDLE_MS of inactivity).
 * Requests tiles that cover the current viewport + buffer.
 */
function sweepStalledTiles() {
  /* Shared stall-detection helper. Clears any tile request older than
   * TILE_STALL_MS so the slot becomes available for re-request. Called from
   * requestVisibleTiles(), the periodic watchdog, and on tile delivery. */
  var now = Date.now();
  var cleared = 0;
  for (var sti in pendingTileRequests) {
    if (pendingTileRequests[sti] && tileRequestTimestamps[sti] &&
        (now - tileRequestTimestamps[sti]) > TILE_STALL_MS) {
      delete pendingTileRequests[sti];
      delete tileRequestTimestamps[sti];
      tileLoadingCount = Math.max(0, tileLoadingCount - 1);
      cleared++;
    }
  }
  if (cleared > 0) {
    console.log('[JS] stall-sweep: cleared ' + cleared + ' stuck tiles, inflight=' + tileLoadingCount);
    // Re-kick using the mode-appropriate tile request path
    try {
      if (_flatWindowed)    _wFlatRequestTiles();
      else if (_flatRender) _flatRequestVisibleTiles();
      else                  requestVisibleTiles();
    } catch (e) {}
  }
  return cleared;
}

function requestVisibleTiles() {
  if (initialScrollGuard || totalCount === 0) return;
  // Stall detection inline (also runs via watchdog / on-delivery)
  sweepStalledTiles();

  if (tileLoadingCount >= MAX_CONCURRENT_TILES) return;

  var scrollTop = _getScrollTop();
  var vpH = chatArea.clientHeight;
  if (vpH <= 0) return;

  // If a scroll target is pending, Python is already streaming the tiles
  // around that target. Don't waste slots fetching tiles 0..2 just because
  // scrollTop is still at 0 — wait for scrollToMessage to move us.
  if (_pendingScrollMsgId !== null) return;

  // ONLY viewport tiles — NOT buffer zone. Buffer tombstones are cosmetic.
  var rawFirst = findItemAtOffset(scrollTop);
  var rawLast = findItemAtOffset(scrollTop + vpH);
  var first = Math.max(0, rawFirst);
  var last = Math.min(totalCount - 1, rawLast);

  var firstTile = Math.floor(first / TILE_SIZE);
  var lastTile = Math.floor(last / TILE_SIZE);

  // Only request tiles covering the viewport — no eager prefetch
  var requested = 0;
  for (var ti = firstTile; ti <= lastTile; ti++) {
    if (!tileMap[ti] && !pendingTileRequests[ti] && tileLoadingCount + requested < MAX_CONCURRENT_TILES) {
      var globalIdx = ti * TILE_SIZE;
      if (globalIdx < totalCount) {
        pendingTileRequests[ti] = true;
        tileRequestTimestamps[ti] = Date.now();
        tileLoadingCount++;
        if (bridge) bridge.onLoadRange(globalIdx);
        requested++;
      }
    } else if (tileMap[ti]) {
      touchTile(ti);
    }
  }

  // Prefetch 2 tiles ahead in scroll direction (200 msgs with TILE_SIZE=100)
  var scrollDir = first > renderedFirst ? 1 : -1;
  for (var pi = 1; pi <= 2; pi++) {
    if (requested >= MAX_CONCURRENT_TILES || tileLoadingCount >= MAX_CONCURRENT_TILES) break;
    var prefetchTi = scrollDir > 0 ? lastTile + pi : firstTile - pi;
    if (prefetchTi >= 0 && prefetchTi * TILE_SIZE < totalCount && !tileMap[prefetchTi] && !pendingTileRequests[prefetchTi]) {
      pendingTileRequests[prefetchTi] = true;
      tileRequestTimestamps[prefetchTi] = Date.now();
      tileLoadingCount++;
      if (bridge) bridge.onLoadRange(prefetchTi * TILE_SIZE);
      requested++;
    }
  }

  if (requested > 0) {
    console.log('[JS] requestVisibleTiles: tiles ' + firstTile + '-' + lastTile + ', requested=' + requested + ', inflight=' + tileLoadingCount);
  }
}

// FLAT MODE: use DOM bounding rects to find which tombstones overlap the
// viewport, then request those tiles. DOM-authoritative — no estimate math.
//
// HISTORICAL BUG: this function checked for class 'tombstone' but
// _flatRenderAll emits FLAT-mode tombstones as <div class="msg tmb">.
// They never matched, so when the user "Go to Chat"-navigated into the
// middle of a 700-msg conversation, only the target tile loaded and
// scrolling up gave them a stretch of empty tiles that never refilled.
// Now matches BOTH 'tmb' (FLAT mode) AND 'tombstone' (virtualized mode).
function _flatRequestVisibleTiles() {
  if (initialScrollGuard || totalCount === 0) return;
  sweepStalledTiles();
  if (tileLoadingCount >= MAX_CONCURRENT_TILES) return;
  if (_pendingScrollMsgId !== null) return;
  if (!container.children.length) return;
  var cr = chatArea.getBoundingClientRect();
  // Expand viewport probe by one viewport-height on each side so we prefetch
  var probeTop = cr.top - cr.height;
  var probeBot = cr.bottom + cr.height;
  var wantedTiles = {};
  for (var i = 0; i < container.children.length; i++) {
    var ch = container.children[i];
    if (!ch.classList) continue;
    if (!ch.classList.contains('tmb') && !ch.classList.contains('tombstone')) continue;
    var r = ch.getBoundingClientRect();
    if (r.bottom < probeTop || r.top > probeBot) continue;   // not in probe
    var gi = parseInt(ch.dataset.globalIdx, 10);
    if (isNaN(gi)) continue;
    var ti = Math.floor(gi / TILE_SIZE);
    wantedTiles[ti] = true;
  }
  var requested = 0;
  for (var tiKey in wantedTiles) {
    if (tileLoadingCount + requested >= MAX_CONCURRENT_TILES) break;
    var tiInt = parseInt(tiKey, 10);
    if (tileMap[tiInt] || pendingTileRequests[tiInt]) continue;
    var gidx = tiInt * TILE_SIZE;
    if (gidx >= totalCount) continue;
    pendingTileRequests[tiInt] = true;
    tileRequestTimestamps[tiInt] = Date.now();
    tileLoadingCount++;
    if (bridge) bridge.onLoadRange(gidx);
    requested++;
  }
  if (requested > 0) {
    console.log('[JS] flat tile request: ' + requested + ' tile(s), inflight=' + tileLoadingCount);
  }
}

// ================================================================
// TOMBSTONE — lightweight placeholder for unloaded messages
// ================================================================
function renderTombstone(gi) {
  var estH = (gi !== undefined) ? estimateHeight(gi) : 52;
  return '<div class="msg tombstone" data-global-idx="' + (gi || 0) + '" style="min-height:' + estH + 'px"><div class="bubble tombstone-bubble">' +
    '<div class="tombstone-line" style="width:60%"></div>' +
    '<div class="tombstone-line short"></div>' +
    '</div></div>';
}

// ================================================================
// renderMsg — single message to HTML
// ================================================================
function renderMsg(msg, prev, gi) {
  var id = msg.id, fm = msg.from_me, text = msg.text || '';
  var mt = msg.type, tl = msg.type_label || '', ts = msg.ts;
  var st = msg.status || 0;
  var _gi = gi !== undefined ? gi : 0;
  // Unread-from-here divider: prepend before the first unread msg.
  // The divider participates in normal flow so scrollIntoView on the divider
  // (via "jump to first unread") or on the msg both land in the right place.
  var _unreadPrefix = '';
  if (_firstUnreadMsgId && id === _firstUnreadMsgId) {
    _unreadPrefix = '<div class="unread-sep" data-unread-anchor="1">'
                  + '<span>Unread messages</span></div>';
  }

  if (mt === 7 || mt === 112) {
    var s = msg.system_text || msg.display_text || text || tl || 'system event';
    // Override for number_changed events — show old → new number clearly
    if (msg.event_label === 'number_changed' && msg.nc_old_phone && msg.nc_new_phone) {
      var ncName = msg.nc_old_name || msg.nc_new_name || '';
      s = '\u{1F4F1} ' + (ncName ? ncName + ' ' : '') + 'changed their number: '
        + msg.nc_old_phone + ' \u2192 ' + msg.nc_new_phone;
    }
    var sysTs = ts ? fmtFullTs(ts) : '';
    return '<div class="msg system" data-msg-id="' + id + '" data-global-idx="' + _gi + '"><div class="system-text">' + esc(s) + '</div>' +
      (sysTs ? '<div class="system-ts">' + sysTs + '</div>' : '') + '</div>';
  }
  if (mt === -1) {
    return '<div class="date-sep"><span>' + esc(text) + '</span></div>';
  }

  var ds = '';
  if (prev && prev.type !== -1 && prev.type !== 7 && ts && prev.ts) {
    var d1 = fmtDate(ts), d2 = fmtDate(prev.ts);
    if (d1 && d1 !== d2) ds = '<div class="date-sep"><span>' + d1 + '</span></div>';
  }

  var dir = fm ? 'sent' : 'received';
  var isSticker = tl === 'sticker';
  if (!!msg.album_parent_id) return '<div class="msg album-child-hidden" data-gi="' + (msg._gi || '') + '" style="height:0;overflow:hidden;margin:0;padding:0"></div>';

  var isCont = prev && prev.from_me === fm && !ds &&
    prev.type !== 7 && prev.type !== -1 &&
    prev.sender === msg.sender &&
    Math.abs((ts || 0) - (prev.ts || 0)) < 60000;

  var cls = 'msg ' + dir;
  if (isCont) cls += ' cont';
  if (msg.is_tagged) cls += ' tagged';
  if (msg.is_starred) cls += ' starred';
  if (isSticker) cls += ' sticker-msg';

  var h = ds + '<div class="' + cls + '" data-msg-id="' + id + '" data-global-idx="' + _gi + '">';

  if (isGroup && !fm) {
    if (!isCont && msg.sender) {
      var sc = sColor(msg.sender);
      if (msg.avatar) {
        h += '<img class="avatar avatar-img" src="' + msg.avatar + '" onclick="bSender(' + (msg.sender_id || 0) + ')" />';
      } else {
        h += '<div class="avatar" style="background:' + sc + '" onclick="bSender(' + (msg.sender_id || 0) + ')">' + avatarLetter(extractName(msg.sender)) + '</div>';
      }
    } else {
      h += '<div class="avatar-spacer"></div>';
    }
  }

  h += '<div class="bubble' + (msg.is_starred ? ' starred-border' : '') + '">';

  // Sender name header: show for all senders in group chats (including owner)
  var _showSender = isGroup && !isCont;
  var _senderName = fm ? (ownerLabel || msg.sender || 'You') : (msg.sender || '');
  if (_showSender && _senderName) {
    var sc2 = sColor(_senderName);
    var sname = extractName(_senderName);
    // Extract phone from ownerLabel "(+XXXXX)" for owner, or use msg.sender_phone for others
    var _senderPhone = fm ? (ownerLabel.match(/\(\+(\d+)\)/) ? ownerLabel.match(/\(\+(\d+)\)/)[1] : '') : (msg.sender_phone || '');
    var _clickSid = fm ? -1 : (msg.sender_id || 0);
    h += '<div class="sender" style="color:' + sc2 + '" onclick="bSender(' + _clickSid + ')">' + esc(sname);
    if (_senderPhone && sname.indexOf(_senderPhone) < 0) h += ' <span class="sender-phone">(+' + esc(_senderPhone) + ')</span>';
    if (msg.member_label && msg.member_label !== sname) h += '<span class="sender-jid">~ ' + esc(msg.member_label) + '</span>';
    if (msg.is_bot && !fm && tl !== 'poll' && tl !== 'poll_vote' && tl !== 'call_log' && mt !== 20 && mt !== 46) h += '<span class="bot-badge">AI</span>';
    if (msg.is_verified) h += '<span class="verified-badge" title="Meta Verified">\u2713</span>';
    else if (msg.is_biz && !msg.is_bot) h += '<span class="biz-badge" title="WhatsApp Business">\u{1F4BC}</span>';
    h += '</div>';
  }

  if (msg.is_ghost) h += '<div class="ghost-label">\u2620 RECOVERED</div>';
  if (msg.is_fwd) {
    var fwdTxt = '\u21AA Forwarded';
    if (msg.fwd_score != null && msg.fwd_score > 0) {
      fwdTxt += ' <span class="fwd-score">\u00D7' + msg.fwd_score;
      if (msg.fwd_score >= 5) fwdTxt += ' \u{1F525}';
      fwdTxt += '</span>';
    }
    h += '<div class="fwd">' + fwdTxt + '</div>';
  }

  if (msg.quoted_text || msg.reply_key) {
    var rk = (msg.reply_key || '').replace(/'/g, "\\'");
    // Determine sender color for quote left border
    var qSenderColor = msg.quoted_sender ? sColor(extractName(msg.quoted_sender)) : '';
    var qBorderStyle = qSenderColor ? ' style="border-left-color:' + qSenderColor + '"' : '';
    h += '<div class="quote" onclick="bQuote(\'' + esc(rk) + '\',' + msg.id + ')"' + qBorderStyle + '>';
    // Quoted media thumbnail (floated right like WhatsApp)
    if (msg.quoted_thumb) {
      h += '<img class="q-thumb" src="' + msg.quoted_thumb + '" style="float:right;width:48px;height:48px;object-fit:cover;border-radius:4px;margin:0 0 2px 6px"/>';
    }
    // Quoted sender name with matching color
    if (msg.quoted_sender) {
      var qNameStyle = qSenderColor ? ' style="color:' + qSenderColor + '"' : '';
      h += '<span class="q-sender"' + qNameStyle + '>' + esc(extractName(msg.quoted_sender)) + '</span>';
    }
    // Resolve integer quoted_type to label
    var qt = msg.quoted_type;
    var qtLabel = (typeof qt === 'number') ? (QTYPE[qt] || '') : (qt || '');
    var qIcons = {
      image: '\u{1F4F7} Photo', video: '\u{1F3A5} Video', audio: '\u{1F3B5} Audio',
      voice: '\u{1F399} Voice note', sticker: '\u{1F36D} Sticker', document: '\u{1F4C4} Document',
      location: '\u{1F4CD} Location', live_location: '\u{1F4F1} Live Location',
      vcard: '\u{1F464} Contact', poll: '\u{1F4CA} Poll', gif: '\u{1F3AC} GIF',
      animated_gif: '\u{1F3AC} GIF', album: '\u{1F5BC} Album', newsletter: '\u{1F4F0} Channel',
      event: '\u{1F4C5} Event', status: '\u{1F4F1} Status'
    };
    if (!msg.quoted_text && qtLabel) {
      h += '<span class="q-type">' + (qIcons[qtLabel] || ('\u{1F4CE} ' + qtLabel)) + '</span>';
    } else if (msg.quoted_text) {
      // Show type icon prefix if media reply with text
      if (qtLabel && qIcons[qtLabel] && qtLabel !== 'text') {
        h += '<span class="q-type-prefix">' + qIcons[qtLabel] + '</span> ';
      }
      h += '<span class="q-text">' + esc(msg.quoted_text.substring(0, 200)) + '</span>';
    } else {
      h += '<span class="q-text">\u21A9 Tap to view original</span>';
    }
    h += '</div>';
  }

  // ── Unavailable/undecrypted message detection ──
  // Messages with a media type but zero content = "Waiting for this message"
  var _noContent = !text && !msg.caption && !msg.thumb && !msg.file_url && !msg.file_exists && !msg.media_name && !(msg.has_url && msg.has_key);
  var _skipTypes = ['album', 'location', 'live_location', 'poll', 'poll_vote', 'vcard', 'vcard_list', 'call_log', 'scheduled_event', 'text', '',
    'voice', 'audio', 'view_once_voice', 'view_once_image', 'view_once_video', 'newsletter', 'document'];
  var _isViewOnce = tl === 'view_once_voice' || tl === 'view_once_image' || tl === 'view_once_video' || msg.is_view_once;
  if (_noContent && tl && _skipTypes.indexOf(tl) === -1 && mt !== 7 && mt !== -1 && !_isViewOnce) {
    h += '<div class="msg-unavailable"><span class="lock-icon">\u{1F512}</span> Waiting for this message. This may take a while.</div>';
  }
  // View-once with no local media file — show view-once card with type, thumbnail, download
  if (_isViewOnce && !msg.file_url && !msg.file_exists) {
    var _voIcon = (tl === 'view_once_voice') ? '\u{1F399}' : (tl === 'view_once_video') ? '\u{1F3AC}' : '\u{1F4F7}';
    var _voLabel = tl === 'view_once_voice' ? 'View Once Voice Note'
      : tl === 'view_once_video' ? 'View Once Video'
        : tl === 'view_once_image' ? 'View Once Photo'
          : msg.is_view_once ? 'View Once ' + (tl || 'Media')
            : 'View Once Media';
    h += '<div class="view-once-card">';
    // Show thumbnail if available (common for view-once images/videos)
    if (msg.thumb) {
      h += '<div style="position:relative;margin-bottom:6px">';
      h += '<img src="' + msg.thumb + '" style="max-width:200px;border-radius:6px;opacity:0.7" />';
      h += '<div style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);font-size:28px;text-shadow:0 1px 4px rgba(0,0,0,.5)">\u{1F441}</div>';
      h += '</div>';
    }
    h += '<span class="vo-icon">' + _voIcon + '</span> ' + _voLabel;
    if (msg.duration_ms) h += ' (' + fmtDur(msg.duration_ms) + ')';
    // View-once state badge
    if (msg.vo_state === 0) h += ' <span style="font-size:10px;color:#e65100;font-weight:600">\u{1F7E0} Not Opened</span>';
    else if (msg.vo_state === 1) h += ' <span style="font-size:10px;color:#1565c0;font-weight:600">\u{1F441} Opened</span>';
    else if (msg.vo_state === 2) h += ' <span style="font-size:10px;color:#2e7d32;font-weight:600">\u2705 Played</span>';
    if (msg.has_url && msg.has_key) {
      h += '<div class="download-inline" style="margin-top:4px" onclick="bDl(\'' + id + '\')">';
      h += '<span class="dl-icon">\u2B07</span> Download from server';
      if (msg.file_size) h += ' (' + fmtSize(msg.file_size) + ')';
      h += '</div>';
    } else if (msg.has_url && !msg.has_key) {
      h += '<div style="font-size:10px;color:#999;margin-top:3px;font-style:italic">\u{1F512} Key unavailable \u2014 WhatsApp deletes decryption keys for received view-once media</div>';
    }
    h += '</div>';
  }
  // Album (multi-photo / multi-video post, msgstore.message_type=99)
  // Renders for ANY album parent \u2014 even when album_children is empty,
  // we still show the authoritative count from album_meta and a row of
  // placeholder cells so the analyst sees "this is an album of 4 videos"
  // instead of an empty bubble.
  else if (tl === 'album') {
    var _ac = msg.album_children || [];
    var _acLen = _ac.length;
    var _meta = msg.album_meta || null;
    // Authoritative counts from message_album.  Fall back to children
    // count when meta is missing (older db, no album-ingester run).
    var _imgCnt = _meta ? (_meta.image_count || 0) : 0;
    var _vidCnt = _meta ? (_meta.video_count || 0) : 0;
    var _missImg = _meta ? (_meta.missing_image_count || 0) : 0;
    var _missVid = _meta ? (_meta.missing_video_count || 0) : 0;
    var _missTotal = _missImg + _missVid;
    var _expectedTotal = _meta
        ? ((_meta.expected_image_count || _imgCnt) + (_meta.expected_video_count || _vidCnt))
        : 0;
    if (!_meta) {
      // Heuristic counts from children alone
      for (var _ck = 0; _ck < _acLen; _ck++) {
        var _ckt = _ac[_ck].type_label;
        if (_ckt === 'video' || _ckt === 'gif' || _ckt === 'animated_gif') _vidCnt++;
        else _imgCnt++;
      }
    }
    var _dlCount = 0, _onDisk = 0;
    for (var _ci = 0; _ci < _acLen; _ci++) {
      if (_ac[_ci].file_exists) _onDisk++;
      else if (_ac[_ci].has_url) _dlCount++;
    }
    // Build human-readable count: "5 photos . 2 videos"
    var _countParts = [];
    if (_imgCnt) _countParts.push('\u{1F4F7} ' + _imgCnt + ' photo' + (_imgCnt === 1 ? '' : 's'));
    if (_vidCnt) _countParts.push('\u{1F3AC} ' + _vidCnt + ' video' + (_vidCnt === 1 ? '' : 's'));
    var _countStr = _countParts.join(' \u00B7 ') || (_acLen + ' items');

    h += '<div class="album-wrapper">';
    h += '<div class="album-header">\u{1F5BC} Album \u2014 ' + _countStr;
    if (_onDisk > 0) h += ' <span style="color:#2e7d32">\u2714 ' + _onDisk + ' on disk</span>';
    if (_dlCount > 0) h += ' <span class="album-dl-all" onclick="event.stopPropagation();bAlbumDownload(' + id + ')" title="Download all ' + _dlCount + ' items">\u2913 Download ' + _dlCount + '</span>';
    h += '</div>';
    // Forensic note - missing children, expected vs actual, etc.
    if (_meta && _meta.note) {
      h += '<div class="album-note" title="forensic note">\u26A0 ' + esc(_meta.note) + '</div>';
    } else if (_missTotal > 0) {
      h += '<div class="album-note">\u26A0 WhatsApp expected ' + _expectedTotal +
           ', only ' + (_imgCnt + _vidCnt) + ' present, ' + _missTotal + ' missing</div>';
    }

    // Cap the visible grid at 9 cells when there are more.  The 9th cell
    // becomes a "+N more" overlay; clicking it expands the grid in place.
    // Even at 9 cells, every item still gets a "i / N" position label
    // top-left so investigators can verify "image #47 of 100" exactly.
    var _ALBUM_CAP = 9;
    var _expanded = msg._album_expanded;  // sticky toggle on the msg dict
    var _totalCells = (_acLen > 0) ? _acLen : (_imgCnt + _vidCnt);
    var _showCount = (_expanded || _totalCells <= _ALBUM_CAP) ? _totalCells : _ALBUM_CAP;
    var _hidden = _totalCells - _showCount;

    h += '<div class="album-grid">';
    if (_acLen > 0) {
      // Children resolved - render real grid with thumbnails
      for (var ai = 0; ai < _showCount; ai++) {
        var c = _ac[ai];
        var cfp = esc((c.file_path || '').replace(/\\/g, '/')).replace(/'/g, "\\'");
        var isVid = c.type_label === 'video' || (c.mime && c.mime.startsWith('video/'));
        // Position label "47/100" - lets the analyst point at a specific
        // item in a forwarded forensic export ("the 23rd photo in album X").
        var _posLabel = (ai + 1) + '/' + _totalCells;
        // data-vid-msg-id lets updateVideoThumb() find this cell
        // and swap the <img src> when ChatVideoThumbWorker
        // finishes extracting a first frame for the child video.
        // Set unconditionally (zero for images) — querying by
        // exact id costs nothing and keeps the markup uniform.
        // ``data-album-child-id`` carries the msg.id of the
        // album CHILD (each photo/video in the grid is its own
        // message in msgstore).  The contextmenu handler below
        // treats this as a higher-priority id source than the
        // outer ``data-msg-id`` (which is the album parent), so
        // right-click → "Find Similar Images" works on the
        // child cell rather than the album-as-a-whole.
        h += '<div class="album-item" data-album-pos="' + _posLabel +
             '" data-album-child-id="' + (c.id || 0) + '"' +
             ' data-vid-msg-id="' + (c.id || 0) + '"' +
             ' onclick="bAlbumOpen(' + id + ',' + ai + ')">';
        // Videos: prefer the worker-extracted first frame
        // (already overrode c.thumb in chat_web_view.py when it
        // was cached); fall back to embedded msgstore thumb;
        // last resort is a pending <img> that updateVideoThumb
        // will fill in when the async extraction completes (or
        // a placeholder when the file isn't on disk at all).
        // We deliberately avoid <video preload=metadata> here —
        // Qt WebEngine doesn't ship proprietary codecs (HEVC,
        // AV1, sometimes high-profile H.264) so the element
        // renders blank for many real-world videos.  Qt's
        // native QMediaPlayer does decode them and gives us a
        // JPEG instead.
        // Images: original file (full resolution from disk)
        // wins over the tiny embedded thumb when available.
        if (isVid) {
          if (c.thumb) {
            h += '<img src="' + c.thumb + '" loading="lazy" />';
          } else if (c.file_url && c.file_exists) {
            // File is on disk, worker is extracting; render an
            // empty <img> placeholder that updateVideoThumb()
            // will populate, instead of a <video> tag that
            // would show blank for unsupported codecs.
            h += '<img class="video-thumb-pending" src="" loading="lazy" />';
          } else {
            h += '<div class="album-placeholder">' + (MICON[c.type_label] || '\u{1F4CE}') + '</div>';
          }
        } else {
          var _albumSrc = c.file_url || c.thumb || '';
          if (_albumSrc) {
            h += '<img src="' + _albumSrc + '" loading="lazy" />';
          } else {
            h += '<div class="album-placeholder">' + (MICON[c.type_label] || '\u{1F4CE}') + '</div>';
          }
        }
        h += '<span class="album-pos-badge">' + _posLabel + '</span>';
        if (isVid) h += '<span class="album-vid-badge">\u25B6</span>';
        if (c.file_exists) h += '<span class="album-disk-badge">\u2714</span>';
        else if (c.has_url) h += '<span class="album-dl-badge" onclick="event.stopPropagation();bDl(\'' + (c.id || 0) + '\')" title="Download this item">\u2913</span>';
        // "+N more" overlay on the LAST visible cell when capped.
        if (ai === _showCount - 1 && _hidden > 0) {
          h += '<span class="album-more-overlay" onclick="event.stopPropagation();bAlbumExpand(' + id + ')" title="Show all ' + _totalCells + ' items">+' + _hidden + ' more</span>';
        }
        h += '</div>';
      }
    } else if (_meta && (_imgCnt + _vidCnt) > 0) {
      // Children were not resolved into the loaded items but meta tells
      // us how many there are - render placeholder cells so the album
      // is visibly an N-item post, not an empty bubble.
      for (var pi = 0; pi < _showCount; pi++) {
        var _isVidPh = pi >= _imgCnt;  // images first, then videos
        var _posLabel2 = (pi + 1) + '/' + _totalCells;
        h += '<div class="album-item album-item-placeholder" data-album-pos="' + _posLabel2 + '">' +
             '<div class="album-placeholder">' + (_isVidPh ? '\u{1F3AC}' : '\u{1F4F7}') + '</div>' +
             '<span class="album-pos-badge">' + _posLabel2 + '</span>' +
             (_isVidPh ? '<span class="album-vid-badge">\u25B6</span>' : '') +
             (pi === _showCount - 1 && _hidden > 0
                ? '<span class="album-more-overlay" onclick="event.stopPropagation();bAlbumExpand(' + id + ')" title="Show all ' + _totalCells + ' items">+' + _hidden + ' more</span>'
                : '') +
             '</div>';
      }
    }
    h += '</div>';
    // Footer with "Collapse" link when expanded
    if (_expanded && _totalCells > _ALBUM_CAP) {
      h += '<div class="album-footer"><span class="album-collapse-link" onclick="event.stopPropagation();bAlbumCollapse(' + id + ')">\u25B2 Collapse to ' + _ALBUM_CAP + '</span></div>';
    }
    h += '</div>';
  }

  // Sticker
  else if (isSticker) {
    if (msg.sticker_url || msg.thumb) {
      var sfp = esc((msg.file_path || '').replace(/\\/g, '/')).replace(/'/g, "\\'");
      h += '<img class="sticker-img" src="' + (msg.sticker_url || msg.thumb) + '" onclick="bMedia(\'' + sfp + '\',' + id + ')" />';
    } else {
      h += '<div class="sticker-placeholder">\u{1F36D} Sticker</div>';
    }
  }
  // Image/GIF/Video from file (on disk)
  // Each bubble now renders its OWN bytes (SD parent shows SD,
  // HD twin shows HD).  Both pair members are visible so the
  // analyst sees the full truth — there's nothing to dedupe.
  else if (msg.file_url && !isSticker && tl !== 'album' &&
    (tl === 'image' || tl === 'gif' || tl === 'animated_gif' || tl === 'video' ||
      tl === 'view_once_image' || tl === 'view_once_video' ||
      (msg.mime && (msg.mime.startsWith('image/') || msg.mime.startsWith('video/'))))) {
    var _displayUrl = msg.file_url;
    var mfp = esc((msg.file_path || '').replace(/\\/g, '/')).replace(/'/g, "\\'");
    var _dimsMsg = msg;
    var _qb = qualityBadge(msg);
    var _pb = provenanceBadge(msg);   // Hash-linked / Recovered / On CDN
    var _hdPair = hdPairBadge(msg, tl);
    var _dlHd = downloadHdBadge(msg);
    // "▶ Live" badge for Android Motion Photos (msgstore
    // message_association.association_type=11 — still image parent
    // with a 1-2 s motion-clip child).  Click plays the clip via
    // the bMotion bridge (opens MediaViewer pre-loaded on the clip).
    var _motionBadge = '';
    if (msg.motion_msg_id && msg.motion_file_url) {
      var _motionFp = esc(msg.motion_path.replace(/\\/g, '/')).replace(/'/g, "\\'");
      var _motionDur = msg.motion_duration ? (msg.motion_duration / 1000).toFixed(1) + 's' : '';
      _motionBadge = '<span class="motion-badge" '
        + 'onclick="event.stopPropagation();bMedia(\'' + _motionFp + '\',' + msg.motion_msg_id + ')" '
        + 'title="Android Motion Photo / Live Photo — click to play the ' + _motionDur + ' motion clip">'
        + '▶ Live' + (_motionDur ? ' · ' + _motionDur : '') + '</span>';
    }
    if (tl === 'gif' || tl === 'animated_gif' || msg.mime === 'image/gif') {
      var gifSrc = (msg.mime && msg.mime.startsWith('video/')) ? (msg.thumb || _displayUrl) : _displayUrl;
      h += '<div class="gif-container" onclick="bMedia(\'' + mfp + '\',' + id + ')"><img class="media-thumb" src="' + gifSrc + '"' + mediaDims(_dimsMsg) + ' loading="lazy" /><span class="gif-badge">GIF</span>' + _qb + _pb + _hdPair + _dlHd + _motionBadge + '</div>';
    } else if (tl === 'video' || (msg.mime && msg.mime.startsWith('video/'))) {
      // VIDEO PREVIEW STRATEGY (rev 2):
      // We always render the bubble as an <img> with msg.thumb as
      // the source \u2014 never <video>.  Reason: Qt WebEngine ships
      // without proprietary codec support (HEVC/H.265, AV1), so a
      // <video src=mp4> tag renders blank for most non-H.264
      // videos.  Instead, the Python-side ChatVideoThumbWorker
      // extracts the first frame using Qt's native media stack
      // (Windows Media Foundation etc.) and caches it as a JPEG
      // in the shared L2 thumb cache; the chat payload's
      // msg.thumb already points at that JPEG when the cache
      // hits.  For cache misses, msg.thumb is the embedded
      // msgstore thumb (often blurry but at least visible) \u2014 and
      // updateVideoThumb() will swap the src to the freshly
      // extracted high-quality frame as soon as the worker
      // finishes.  The data-vid-msg-id attribute is what
      // updateVideoThumb finds by selector.
      h += '<div class="video-container" onclick="bMedia(\'' + mfp + '\',' + id + ')" data-vid-msg-id="' + id + '">';
      if (msg.thumb) {
        h += '<img class="media-thumb" src="' + msg.thumb + '"' + mediaDims(_dimsMsg) + ' loading="lazy" />';
      } else {
        // No thumb at all (no msgstore blob and no extracted
        // frame yet).  Empty <img> with the same dims so the
        // bubble keeps its layout footprint while we wait for
        // the worker; updateVideoThumb fills it in.
        h += '<img class="media-thumb video-thumb-pending" src=""' + mediaDims(_dimsMsg) + ' loading="lazy" />';
      }
      h += '<span class="video-play-badge">\u25B6</span>' + _qb + _pb + _hdPair + _dlHd + _motionBadge + '</div>';
    } else {
      h += '<div class="image-container" onclick="bMedia(\'' + mfp + '\',' + id + ')"><img class="media-thumb" src="' + _displayUrl + '"' + mediaDims(_dimsMsg) + ' loading="lazy" />' + _qb + _pb + _hdPair + _dlHd + _motionBadge + '</div>';
    }
    if (msg.is_view_once) h += '<span class="view-once-badge">\u{1F441} View Once</span>';
  }
  // Thumbnail (with overlay badges for GIF/video)
  // When file not on disk: show thumbnail + download overlay instead of open
  // Exclude 'document' — handled by dedicated doc card branches below.
  // Exclude 'text' AND messages that carry link_detail rows (msg.link):
  //   for those, ``msg.thumb`` is the og:image of a link preview, NOT a
  //   media attachment.  The link card (renderLinks) renders that thumb
  //   itself further down — without this guard we'd draw a redundant
  //   "Media Missing" tile above the link card for every URL-bearing
  //   text message.
  else if (msg.thumb && tl !== 'album' && tl !== 'document'
           && tl !== 'text' && !msg.link) {
    var mfp = esc((msg.file_path || '').replace(/\\/g, '/')).replace(/'/g, "\\'");
    var isGifType = (tl === 'gif' || tl === 'animated_gif' || msg.mime === 'image/gif');
    var isVideoType = (tl === 'video' || (msg.mime && msg.mime.startsWith('video/')));
    var _qb = qualityBadge(msg);
    var _pb = provenanceBadge(msg);   // Hash-linked / Recovered / On CDN
    var _hdPair = hdPairBadge(msg, tl);
    var _dlHd = downloadHdBadge(msg);
    var _canDl = !msg.file_exists && msg.has_url && msg.has_key;
    // Label "Download HD" only when dimensions confirm it's HD (>1600px side).
    // SD images (quality=3, ~986×1203) are labelled just "Download" — CDN has same SD quality.
    var _w = msg.media_w || msg.media_width || 0;
    var _h = msg.media_h || msg.media_height || 0;
    var _isHdMedia = _w > 1600 || _h > 1600;
    var _dlLabel = _canDl ? ('\u2B07 Download' + (_isHdMedia ? ' HD' : '')) : '';
    var _onClick = msg.file_exists
      ? 'onclick="bMedia(\'' + mfp + '\',' + id + ')"'
      : (_canDl ? 'onclick="bDl(\'' + id + '\')"' : '');
    // Media status overlay for files not on disk
    var _missingLabel = '';
    if (!msg.file_exists) {
      if (msg.media_status === 'expired') _missingLabel = '\u{1F7E0} URL Expired';
      else if (msg.media_status === 'no_key') _missingLabel = '\u{1F512} No Decryption Key';
      else if (msg.media_status === 'thumb_only') _missingLabel = '\u{1F5BC} Thumbnail Only';
      else if (msg.has_url && msg.has_key) _missingLabel = '';  // downloadable — handled by _canDl
      else if (msg.has_url) _missingLabel = '\u{1F512} Key Missing';
      else _missingLabel = '\u274C Media Missing';
    }
    var _extra = _canDl
      ? '<div class="dl-overlay"><span class="dl-overlay-label">' + _dlLabel + '</span></div>'
      : (_missingLabel
        ? '<div class="dl-overlay dl-overlay-missing"><span class="dl-overlay-label">' + _missingLabel + '</span></div>'
        : '');
    if (isGifType) {
      h += '<div class="gif-container' + (_canDl ? ' not-on-disk' : '') + '" ' + _onClick + '><img class="media-thumb" src="' + msg.thumb + '"' + mediaDims(msg) + ' loading="lazy" /><span class="gif-badge">GIF</span>' + _qb + _pb + _hdPair + _dlHd + _extra + '</div>';
    } else if (isVideoType) {
      h += '<div class="video-container' + (_canDl ? ' not-on-disk' : '') + '" ' + _onClick + '><img class="media-thumb" src="' + msg.thumb + '"' + mediaDims(msg) + ' loading="lazy" />';
      if (msg.file_exists) h += '<span class="video-play-badge">\u25B6</span>';
      h += _qb + _pb + _hdPair + _dlHd + _extra + '</div>';
    } else {
      h += '<div class="image-container' + (_canDl ? ' not-on-disk' : '') + '" ' + _onClick + '><img class="media-thumb" src="' + msg.thumb + '"' + mediaDims(msg) + ' loading="lazy" />' + _qb + _pb + _hdPair + _dlHd + _extra + '</div>';
    }
    if (msg.is_view_once) h += '<span class="view-once-badge">\u{1F441} View Once</span>';
  }
  // Audio (including view_once_voice)
  else if (tl === 'voice' || tl === 'audio' || tl === 'view_once_voice' || (msg.mime && msg.mime.startsWith('audio/'))) {
    var afp = esc((msg.file_path || '').replace(/\\/g, '/')).replace(/'/g, "\\'");
    var _audioDl = !msg.file_exists && msg.has_url && msg.has_key;
    var _audioClick = msg.file_exists
      ? 'onclick="bAudio(\'' + afp + '\',' + id + ')"'
      : (_audioDl ? 'onclick="bDl(\'' + id + '\')" title="Click to download from server"' : '');
    h += '<div class="audio-card' + (msg.file_exists ? '' : ' not-on-disk') + '" data-audio-id="' + id + '" ' + _audioClick + '>';
    h += '<div class="play-btn">' + (_audioDl ? '\u2B07' : '\u25B6') + '</div>';
    h += '<div class="waveform" data-waveform-id="' + id + '">' + waveBars(id) + '</div>';
    h += '<div class="duration">' + fmtDur(msg.duration_ms) + '</div>';
    if (_audioDl) h += '<div class="audio-dl-hint">Tap to download</div>';
    h += '</div>';
  }
  // Location
  else if (tl === 'location' || tl === 'live_location') {
    var locIcon = msg.loc_is_live ? '\u{1F4F1}' : '\u{1F4CD}';
    h += '<div class="loc-card">';
    // Map preview thumbnail (from msgstore message_thumbnail)
    if (msg.loc_thumb) {
      h += '<div class="loc-thumb-wrap"><img class="loc-thumb-img" src="' + msg.loc_thumb + '" alt="Map preview" style="width:100%;max-height:160px;object-fit:cover;border-radius:8px 8px 0 0;display:block"/></div>';
    }
    h += '<div style="display:flex;align-items:flex-start;padding:6px 8px"><div class="loc-icon">' + locIcon + '</div><div class="loc-info">';
    h += '<div class="loc-place">' + esc(msg.place || (msg.loc_is_live ? 'Live Location' : 'Location'));
    if (msg.loc_is_live && msg.loc_dur) {
      var dm = Math.round(msg.loc_dur / 60);
      h += ' <span class="loc-dur">(' + (dm >= 60 ? Math.round(dm / 60) + 'h' : dm + ' min') + ')</span>';
    }
    h += '</div>';
    if (msg.place_addr) h += '<div class="loc-addr">' + esc(msg.place_addr) + '</div>';
    if (msg.lat != null && msg.lon != null) {
      var coordLabel = msg.loc_is_live ? '\u{1F4CD} Start: ' : '\u{1F4CD} ';
      h += '<div class="loc-coords">' + coordLabel + msg.lat.toFixed(5) + ', ' + msg.lon.toFixed(5);
      h += ' &middot; <a href="#" onclick="bUrl(\'https://maps.google.com/?q=' + msg.lat + ',' + msg.lon + '\');return false;">Maps</a></div>';
    }
    if (msg.loc_is_live && msg.loc_final_lat != null && msg.loc_final_lon != null) {
      h += '<div class="loc-coords">\u{1F3C1} End: ' + msg.loc_final_lat.toFixed(5) + ', ' + msg.loc_final_lon.toFixed(5);
      h += ' &middot; <a href="#" onclick="bUrl(\'https://maps.google.com/?q=' + msg.loc_final_lat + ',' + msg.loc_final_lon + '\');return false;">Maps</a>';
      if (msg.loc_final_ts) h += ' &middot; ' + fmtFullTs(msg.loc_final_ts);
      h += '</div>';
    }
    h += '</div></div></div>';
  }
  // Poll
  else if (tl === 'poll' || tl === 'poll_vote' || mt === 46) {
    // Always enter poll branch for poll types, even if msg.poll is empty
    if (!msg.poll) {
      h += '<div class="poll-card"><div class="poll-q">\u{1F4CA} ' + esc(text || 'Poll') + '</div><div style="color:#999;padding:4px 8px;font-size:12px;">Poll data loading...</div></div>';
    } else {
      var opts = msg.poll.split('\n').filter(function (l) { return l.trim(); });
      var maxV = 0;
      // Format: name::votes::voter_names (voter_names is comma-separated, may be empty)
      var parsed = opts.map(function (o) {
        var pp = o.split('::');
        var v = parseInt(pp[1]) || 0;
        if (v > maxV) maxV = v;
        return { name: pp[0] || o, votes: v, voters: pp[2] || '' };
      });
      var totalVotes = 0;
      parsed.forEach(function (o) { totalVotes += o.votes; });
      // Build a name \u2192 image-src map from msg.poll_option_images
      // (channel polls with image options use msgstore
      // message_association.association_type=6 to attach a separate
      // image message per option; we resolved those during ingestion).
      var _poiMap = {};
      var _hasImages = false;
      if (Array.isArray(msg.poll_option_images)) {
        msg.poll_option_images.forEach(function (oi) {
          if (oi && oi.name) {
            _poiMap[oi.name] = oi.src || '';
            if (oi.src) _hasImages = true;
          }
        });
      }
      h += '<div class="poll-card' + (_hasImages ? ' has-images' : '') + '">'
         + '<div class="poll-q">\u{1F4CA} ' + esc(text || 'Poll') + '</div>';
      parsed.forEach(function (o, oi) {
        var pct = maxV > 0 ? Math.round(o.votes / maxV * 100) : 0;
        var hasVoters = o.voters && o.voters.trim();
        var optImgSrc = _poiMap[o.name] || '';
        h += '<div class="poll-option' + (hasVoters ? ' has-voters' : '') + (optImgSrc ? ' has-img' : '') + '"' +
          (hasVoters ? ' onclick="togglePollVoters(this)"' : '') + '>';
        if (optImgSrc) {
          h += '<img class="poll-opt-img" src="' + optImgSrc + '" alt="" onclick="event.stopPropagation();bImgZoom(\'' + optImgSrc.replace(/'/g, "\\'") + '\')" />';
        }
        h += '<div class="poll-bar-bg"><div class="poll-bar-fill" style="width:' + pct + '%"></div>';
        h += '<span class="poll-opt-text">' + esc(o.name) + '</span>';
        h += '<span class="poll-opt-count">' + o.votes + (hasVoters ? ' \u25BC' : '') + '</span></div>';
        // Inline voter list (hidden by default, toggled on click)
        if (hasVoters) {
          // Split carefully — voter names may contain commas inside parentheses like "Name (+91xxx)"
          var voterList = o.voters.split(/,\s*(?=[^)]*(?:\(|$))/).map(function (v) { return v.trim(); }).filter(Boolean);
          h += '<div class="poll-voter-panel" style="display:none;">';
          voterList.forEach(function (vraw) {
            // Strip leading ~ (wa_name prefix from ingestion)
            var vname = vraw.charAt(0) === '~' ? vraw.substring(1) : vraw;
            var initial = vname.charAt(0) === '+' || vname.charAt(0) === '(' ? vname.charAt(1) : vname.charAt(0);
            var sc = sColor(vname);
            h += '<div class="poll-voter-row">';
            h += '<div class="poll-voter-avatar" style="background:' + sc + '">' + (initial || '?').toUpperCase() + '</div>';
            h += '<span class="poll-voter-name">' + esc(vname) + '</span>';
            h += '</div>';
          });
          h += '</div>';
        }
        h += '</div>';
      });
      h += '<div class="poll-footer">';
      var vCount = msg.poll_voters || totalVotes;
      h += '<span class="poll-total">' + vCount + ' vote' + (vCount !== 1 ? 's' : '') + '</span>';
      h += '</div></div>';
    } // close else { msg.poll exists }
  }
  // vCard
  else if (tl === 'vcard' || tl === 'vcard_list' || msg.vcard) {
    if (msg.vcard) {
      msg.vcard.split(';;').filter(Boolean).forEach(function (card) {
        var vp = card.split('||'); var cName = vp[0] || 'Contact'; var cPhones = vp[1] || '';
        var phoneList = cPhones ? cPhones.split(',').map(function (p) { return p.trim(); }).filter(Boolean) : [];
        h += '<div class="vcard-card"><div class="vcard-avatar">\u{1F464}</div><div class="vcard-info"><div class="vcard-name">' + esc(cName) + '</div>';
        if (phoneList.length > 0) { phoneList.forEach(function (ph) { h += '<div class="vcard-phone">\u{1F4DE} ' + esc(ph) + '</div>'; }); }
        else { h += '<div class="vcard-sub">Shared Contact</div>'; }
        h += '</div><button class="vcard-dl-btn" title="Save as VCF" onclick="bVcardDl(' + msg.id + ',\'' + esc(cName).replace(/'/g, "\\'") + '\')">\u{1F4BE}</button></div>';
      });
    } else {
      h += '<div class="vcard-card"><div class="vcard-avatar">\u{1F464}</div><div class="vcard-info"><div class="vcard-name">' + esc(text || 'Contact') + '</div><div class="vcard-sub">Shared Contact</div></div></div>';
    }
  }
  // Call (voice, video, group — with result styling)
  else if (tl === 'call_log' || mt === 90 || mt === 10 || mt === 16) {
    var ci = msg.call_video ? '\u{1F4F9}' : '\u{1F4DE}';
    var cat = msg.call_category || '';
    var ct = msg.call_video ? 'Video Call' : 'Voice Call';
    if (cat === 'voice_chat') { ct = 'Voice Chat'; ci = '\u{1F3A4}'; }
    else if (cat === 'group_call') ct = (msg.call_video ? 'Group Video Call' : 'Group Voice Call');
    else if (cat === 'multi_person') ct = (msg.call_video ? 'Multi-person Video' : 'Multi-person Voice');
    else if (msg.call_is_group) ct = (msg.call_video ? 'Group Video Call' : 'Group Voice Call');
    var result = msg.call_result || 'unknown';
    var resultLower = result.toLowerCase();
    // Map DB result_labels to display labels
    // call_result=5 ("answered") is the normal successful call — most common (3500+ calls)
    // call_result=0 ("connected") is a transient ringing state (rare, 0 duration)
    var resultDisplay = result;
    var hasDuration = msg.call_dur && msg.call_dur > 0;
    if (resultLower === 'answered' || resultLower === 'disconnected' || resultLower === 'completed') {
      resultDisplay = hasDuration ? (fm ? 'Outgoing' : 'Incoming') : (fm ? 'Outgoing' : 'Incoming');
    }
    else if (resultLower === 'connected') resultDisplay = fm ? 'Outgoing' : 'Incoming';
    else if (resultLower === 'missed') {
      // Voice chats: "missed" means owner didn't join (others may have)
      resultDisplay = (cat === 'voice_chat') ? 'Not Joined' : 'Missed';
    }
    else if (resultLower === 'unavailable') resultDisplay = 'Unavailable';
    else if (resultLower === 'rejected') resultDisplay = 'Declined';
    else if (resultLower === 'joined_voice_chat') resultDisplay = 'Joined';
    else if (resultLower === 'cancelled') resultDisplay = fm ? 'Cancelled' : 'Missed';
    else if (resultLower.startsWith('unknown_')) resultDisplay = fm ? 'Outgoing' : 'Incoming';
    // Result class for styling (green=answered, red=missed/rejected, gray=other)
    var resultCls = 'call-result';
    if (resultLower === 'answered' || resultLower === 'disconnected' || resultLower === 'completed' || resultLower === 'connected') resultCls += ' call-accepted';
    else if (resultLower === 'missed') resultCls += ' call-missed';
    else if (resultLower === 'rejected') resultCls += ' call-missed';
    else if (resultLower === 'unavailable' || resultLower === 'cancelled') resultCls += ' call-cancelled';
    else if (resultLower === 'joined_voice_chat') resultCls += ' call-accepted';
    // Duration
    var durStr = '';
    if (msg.call_dur > 0) {
      var hrs = Math.floor(msg.call_dur / 3600);
      var mins = Math.floor((msg.call_dur % 3600) / 60), secs = msg.call_dur % 60;
      durStr = hrs > 0 ? hrs + 'h ' + mins + 'm ' + secs + 's' : (mins > 0 ? mins + 'm ' + secs + 's' : secs + 's');
    }
    // Result icon — arrows for direction, X for failed/missed
    var rIcon = fm ? '\u2197\uFE0F' : '\u2199\uFE0F'; // default: outgoing/incoming arrow
    if (resultLower === 'answered' || resultLower === 'disconnected' || resultLower === 'completed' || resultLower === 'connected') {
      rIcon = fm ? '\u2197\uFE0F' : '\u2199\uFE0F'; // ↗ outgoing / ↙ incoming
    }
    else if (resultLower === 'missed') rIcon = '\u2199\uFE0F'; // ↙ missed incoming
    else if (resultLower === 'rejected') rIcon = '\u2716'; // ✖ declined
    else if (resultLower === 'cancelled') rIcon = fm ? '\u2716' : '\u2199\uFE0F';
    else if (resultLower === 'unavailable' || resultLower === 'busy') rIcon = '\u2716';
    else if (resultLower === 'joined_voice_chat') rIcon = '\u2713';

    // Parse participants — format: "Name|resultCode, Name2|resultCode2, ..."
    var parsedParts = [];
    if (msg.call_participants) {
      msg.call_participants.split(', ').filter(Boolean).forEach(function (raw) {
        var pipeIdx = raw.lastIndexOf('|');
        if (pipeIdx > 0) {
          parsedParts.push({ name: raw.substring(0, pipeIdx).trim(), result: parseInt(raw.substring(pipeIdx + 1), 10) });
        } else {
          parsedParts.push({ name: raw.trim(), result: -1 });
        }
      });
    }
    var partCount = parsedParts.length;
    // Per-participant status icon (call_log_participant_v2.call_result):
    // 0=joined, 2=rejected/no answer, 5=initiated/participated (context-dependent)
    var _callMissed = (resultLower === 'missed' || resultLower === 'rejected' || resultLower === 'unavailable');
    function _cpIcon(r) {
      if (r === 0) return '<span class="cp-status cp-joined" title="Joined">\u2713</span>';
      if (r === 2) return '<span class="cp-status cp-declined" title="No Answer">\u2717</span>';
      if (r === 5) {
        if (_callMissed) return '<span class="cp-status cp-missed" title="Invited (did not join)">\u2022</span>';
        return '<span class="cp-status cp-joined" title="Participated">\u2713</span>';
      }
      return '';
    }
    var dirLabel = fm ? 'Outgoing' : 'Incoming';
    var dirCls = fm ? 'call-dir-out' : 'call-dir-in';

    h += '<div class="call-card' + (msg.is_synthesized ? ' call-synthesized' : '') + '">';
    h += '<div class="call-icon-wrap"><span class="call-icon-emoji">' + ci + '</span>';
    h += '<span class="call-dir-badge ' + dirCls + '">' + dirLabel + '</span></div>';
    h += '<div class="call-body">';
    // Synthesized indicator — voice chats (and group-call echoes in
    // participant-only conversations) are NOT native message_row entries
    // in msgstore.db; they're reassembled from call_record + participant
    // tables during ingestion. Surface this clearly so the examiner
    // knows the record is reconstructed.
    if (msg.is_synthesized || cat === 'voice_chat') {
      h += '<div class="call-synthetic-badge" title="This entry was reassembled from call_record + participant events. WhatsApp does not store voice-chat sessions as regular messages.">\u26A0\uFE0F RECONSTRUCTED</div>';
    }
    // Origin chat for synthetic per-participant call echoes —
    // when we're viewing a 1-on-1 chat and this call's original
    // lives in a group / community / multi-person chat, surface
    // that origin name and a click-through to the real call
    // message.  Without this the analyst sees only "Group Video
    // Call" with no indication of WHICH group the call actually
    // happened in.
    if (msg.call_origin_conv_id && msg.call_origin_conv_id > 0) {
      var _originName = msg.call_origin_conv_name || 'group chat';
      var _originIcon = (msg.call_origin_chat_type === 'community' || msg.call_origin_chat_type === 'community_sub')
                        ? '\u{1F3D8}\uFE0F' : '\u{1F465}';
      // Pill button label is context-aware so analysts see
      // the right "kind" of call they\'re jumping to:
      //   group_call       -> "Go to group call"
      //   multi_person     -> "Go to multi-person call"
      //   voice_chat       -> "Go to voice chat"
      //   anything else    -> "Go to original call"
      // Plus a fall-through to "Go to original chat" when the
      // call_record didn\'t carry a category at all.
      var _jumpLabel;
      if (cat === 'voice_chat')          _jumpLabel = 'Go to voice chat';
      else if (cat === 'group_call')     _jumpLabel = 'Go to group call';
      else if (cat === 'multi_person')   _jumpLabel = 'Go to multi-person call';
      else if (msg.call_is_group)        _jumpLabel = 'Go to group call';
      else                                _jumpLabel = 'Go to original call';
      var _originSubject = (cat === 'voice_chat') ? 'voice chat' : 'call';
      h += '<div class="call-origin-row">';
      h += '<span class="call-origin-icon">' + _originIcon + '</span>';
      h += '<span class="call-origin-label">originally in </span>';
      h += '<span class="call-origin-name">' + esc(_originName) + '</span>';
      h += '<button class="call-origin-jump" '
        +  'onclick="event.stopPropagation();'
        +  'if(window.bridge&&window.bridge.onCallOriginNav){'
        +  'window.bridge.onCallOriginNav(' + msg.call_origin_conv_id + ',' + (msg.call_origin_msg_id || 0) + ');'
        +  '}else{console.warn(\'bridge.onCallOriginNav unavailable\');}" '
        +  'title="Open the original ' + _originSubject + ' in ' + esc(_originName) + ' and scroll to the ' + _originSubject + ' message.">'
        +  _jumpLabel + ' \u2192</button>';
      h += '</div>';
    }
    // Compact summary line
    h += '<div class="call-summary">';
    h += '<span class="call-type">' + esc(ct) + '</span>';
    if (msg.call_creator) h += ' <span class="call-sep">\u2022</span> <span class="call-creator">by ' + esc(msg.call_creator) + '</span>';
    if (partCount > 0) {
      var pLabel = (cat === 'voice_chat') ? ' joined' : ' participant' + (partCount > 1 ? 's' : '');
      h += ' <span class="call-sep">\u2022</span> <span class="call-part-count">' + partCount + pLabel + '</span>';
    }
    if (durStr) h += ' <span class="call-sep">\u2022</span> <span class="call-duration">' + durStr + '</span>';
    h += '</div>';
    // Result status line
    h += '<div class="' + resultCls + '">' + rIcon + ' ' + esc(resultDisplay) + '</div>';
    // Participant section: collapsed preview + expandable full list
    if (partCount > 0) {
      var previewMax = partCount > 10 ? 5 : partCount;
      var previewNames = [];
      for (var pi = 0; pi < previewMax; pi++) previewNames.push(esc(parsedParts[pi].name));
      var previewStr = previewNames.join(', ');
      if (partCount > 10) previewStr += ' +' + (partCount - 5) + ' more';
      h += '<div class="call-parts-preview">' + previewStr + '</div>';
      var uid = 'cp_' + (msg.id || Math.random().toString(36).substr(2, 8));
      h += '<div class="call-expand-toggle" onclick="_suppressAnchor=true;clearTimeout(_suppressAnchorTimer);_suppressAnchorTimer=setTimeout(function(){_suppressAnchor=false},600);var p=document.getElementById(\'' + uid + '\');if(!p)return;var o=p.style.maxHeight&&p.style.maxHeight!==\'0px\';p.style.maxHeight=o?\'0px\':p.scrollHeight+\'px\';this.textContent=o?\'Show participants \u25BE\':\'Hide participants \u25B4\';">Show participants \u25BE</div>';
      h += '<div class="call-participants-list" id="' + uid + '" style="max-height:0">';
      parsedParts.forEach(function (p) {
        var ch = p.name.charAt(0).toUpperCase();
        if (ch === '~' || ch === '+') ch = p.name.length > 1 ? p.name.charAt(1).toUpperCase() : '?';
        h += '<div class="call-participant-row">';
        h += '<span class="cp-avatar">' + ch + '</span>';
        h += '<span class="cp-name">' + esc(p.name) + '</span>';
        h += _cpIcon(p.result);
        h += '</div>';
      });
      h += '</div>';
    }
    h += '</div></div>';
  }
  // Scheduled event
  else if (tl === 'scheduled_event' && msg.scheduled_event_data) {
    var ep = msg.scheduled_event_data.split('||');
    var isCall = ep[7] === '1'; var eventIcon = isCall ? '\u{1F4DE}' : '\u{1F4C5}';
    h += '<div class="event-card"><div class="event-name">' + eventIcon + ' ' + esc(ep[0] || (isCall ? 'Scheduled Call' : 'Event')) + (ep[5] === '1' ? ' (Canceled)' : '') + '</div>';
    if (ep[1]) h += '<div class="event-detail">' + esc(ep[1]) + '</div>';
    if (ep[2]) h += '<div class="event-detail">\u{1F4CD} ' + esc(ep[2]) + '</div>';
    if (ep[4]) { var startStr = '\u{1F552} Start: ' + fmtFullTs(parseInt(ep[4])); if (ep[6]) startStr += ' \u2014 End: ' + fmtFullTs(parseInt(ep[6])); h += '<div class="event-detail">' + startStr + '</div>'; }
    if (ep[3]) h += '<div class="event-detail">\u{1F517} <a href="#" onclick="bUrl(\'' + esc(ep[3]).replace(/'/g, "\\'") + '\');return false;">' + esc(ep[3]) + '</a></div>';
    h += '</div>';
  }
  // Document shared as image — render as image with doc-info bar below
  else if (tl === 'document' && msg.mime && msg.mime.startsWith('image/') && (msg.file_url || msg.thumb)) {
    var difp = esc((msg.file_path || '').replace(/\\/g, '/')).replace(/'/g, "\\'");
    var diSrc = msg.file_url || msg.thumb;
    var diName = msg.media_name || (msg.file_path ? msg.file_path.replace(/\\/g, '/').split('/').pop() : 'Image');
    var diSz = msg.file_size ? fmtSize(msg.file_size) : '';
    h += '<div class="doc-as-image-wrap">';
    h += '<img class="media-thumb" src="' + diSrc + '"' + mediaDims(msg) + (msg.file_exists ? ' onclick="bMedia(\'' + difp + '\',' + id + ')" style="cursor:pointer"' : '') + ' loading="lazy" />';
    h += '<div class="doc-info-bar">';
    h += '<span class="doc-info-icon">\u{1F4C4}</span>';
    h += '<span class="doc-info-name">' + esc(diName) + '</span>';
    if (diSz) h += '<span class="doc-info-size">' + diSz + '</span>';
    h += '</div></div>';
  }
  // Document shared as PDF — render doc card with thumbnail + PDF info
  else if (tl === 'document' && msg.mime === 'application/pdf') {
    var pdfName = msg.media_name || (msg.file_path ? msg.file_path.replace(/\\/g, '/').split('/').pop() : 'Document.pdf');
    var pdfp = esc((msg.file_path || '').replace(/\\/g, '/')).replace(/'/g, "\\'");
    var pdfSz = msg.file_size ? fmtSize(msg.file_size) : '';
    var pdfPg = msg.page_count ? msg.page_count + ' page' + (msg.page_count > 1 ? 's' : '') : '';
    var pdfMeta = [];
    pdfMeta.push('PDF');
    if (pdfPg) pdfMeta.push(pdfPg);
    if (pdfSz) pdfMeta.push(pdfSz);
    var pdfMetaLine = pdfMeta.join(' \u00B7 ');
    var pdfClick = msg.file_exists ? ' onclick="bMedia(\'' + pdfp + '\',' + id + ')"' : '';
    // Show thumbnail preview above doc card if available
    if (msg.thumb) {
      h += '<div class="pdf-preview-wrap' + (msg.file_exists ? ' clickable' : '') + '"' + pdfClick + '>';
      h += '<img class="pdf-preview-thumb" src="' + msg.thumb + '" loading="lazy" />';
      h += '</div>';
    }
    h += '<div class="doc-card' + (msg.file_exists ? ' clickable' : '') + '"' + pdfClick + '>';
    h += '<div class="doc-icon-wrap"><span class="doc-icon">\u{1F4D1}</span></div>';
    h += '<div class="doc-info">';
    h += '<div class="doc-name">' + esc(pdfName) + '</div>';
    if (pdfMetaLine) h += '<div class="doc-meta">' + pdfMetaLine + '</div>';
    h += '</div>';
    if (msg.file_exists) {
      h += '<div class="doc-action">\u{1F4C2}</div>';
    } else if (msg.has_url && msg.has_key) {
      h += '<div class="doc-action doc-dl" onclick="event.stopPropagation();bDl(\'' + id + '\')">\u2B07</div>';
    }
    h += '</div>';
  }
  // Document card (DOCX, XLS, ZIP, etc.) — dedicated rich card
  else if (tl === 'document') {
    var docIcon = '\u{1F4C4}';
    var docName = msg.media_name || '';
    var ext = '';
    if (docName) {
      ext = docName.split('.').pop().toLowerCase();
      if (ext === 'pdf') docIcon = '\u{1F4D1}';
      else if (ext === 'doc' || ext === 'docx') docIcon = '\u{1F4DD}';
      else if (ext === 'xls' || ext === 'xlsx') docIcon = '\u{1F4CA}';
      else if (ext === 'ppt' || ext === 'pptx') docIcon = '\u{1F4CA}';
      else if (ext === 'zip' || ext === 'rar' || ext === '7z') docIcon = '\u{1F4E6}';
      else if (ext === 'apk') docIcon = '\u{1F4E6}';
      else if (ext === 'txt' || ext === 'csv' || ext === 'json' || ext === 'xml') docIcon = '\u{1F4C3}';
    }
    // Fallback filename from file_path
    if (!docName && msg.file_path) {
      docName = msg.file_path.replace(/\\/g, '/').split('/').pop();
      ext = docName.split('.').pop().toLowerCase();
    }
    var dfp = esc((msg.file_path || '').replace(/\\/g, '/')).replace(/'/g, "\\'");
    var szStr = msg.file_size ? fmtSize(msg.file_size) : '';
    var pgStr = msg.page_count ? msg.page_count + ' page' + (msg.page_count > 1 ? 's' : '') : '';
    var metaParts = [];
    if (ext) metaParts.push(ext.toUpperCase());
    if (pgStr) metaParts.push(pgStr);
    if (szStr) metaParts.push(szStr);
    var metaLine = metaParts.join(' \u00B7 ');

    h += '<div class="doc-card' + (msg.file_exists ? ' clickable' : '') + '"' +
      (msg.file_exists ? ' onclick="bMedia(\'' + dfp + '\',' + id + ')"' : '') + '>';
    h += '<div class="doc-icon-wrap"><span class="doc-icon">' + docIcon + '</span></div>';
    h += '<div class="doc-info">';
    h += '<div class="doc-name">' + esc(docName || 'Document') + '</div>';
    if (metaLine) h += '<div class="doc-meta">' + metaLine + '</div>';
    h += '</div>';
    if (msg.file_exists) {
      h += '<div class="doc-action">\u{1F4C2}</div>';
    } else if (msg.has_url && msg.has_key) {
      h += '<div class="doc-action doc-dl" onclick="event.stopPropagation();bDl(\'' + id + '\')">\u2B07</div>';
    }
    h += '</div>';
  }
  // Generic media badge (media without thumbnails — non-document)
  // Skip for revoked messages and newsletters (they get their own rendering below)
  else if (!msg.thumb && tl && tl !== 'text' && tl !== '' && tl !== 'sticker' && tl !== 'album' && tl !== 'location' && tl !== 'live_location' && tl !== 'poll' && tl !== 'poll_vote' && tl !== 'vcard' && tl !== 'call_log' && tl !== 'scheduled_event' && !(msg.is_revoked && tl === 'newsletter')) {
    var icon = MICON[tl] || MICON[''];
    var badgeLabel = (tl === 'gif' || tl === 'animated_gif') ? 'GIF' : (tl === 'voice' ? 'Voice' : tl.charAt(0).toUpperCase() + tl.slice(1));
    var sz = msg.file_size ? ' (' + fmtSize(msg.file_size) + ')' : '';
    if (msg.file_exists) {
      var gfp = esc((msg.file_path || '').replace(/\\/g, '/')).replace(/'/g, "\\'");
      h += '<div class="media-badge clickable" onclick="bMedia(\'' + gfp + '\',' + id + ')">';
      h += '<span class="icon">' + icon + '</span> ' + esc(badgeLabel) + sz + '</div>';
    } else if (msg.has_url && msg.has_key) {
      h += '<div class="download-badge" onclick="bDl(\'' + id + '\')">';
      h += '<span class="icon">' + icon + '</span> ' + esc(badgeLabel) + sz + ' <span class="dl-arrow">\u2B07</span></div>';
    } else {
      h += '<div class="media-badge">';
      h += '<span class="icon">' + icon + '</span> ' + esc(badgeLabel) + sz + '</div>';
    }
  }

  // Download from server button — shows for media with enc URL but no local file
  // Skip if thumbnail already shows the download overlay (avoids duplicate controls)
  var _thumbOverlayShown = msg.thumb && !msg.file_exists && msg.has_url && msg.has_key && tl !== 'album';
  if (msg.has_url && msg.has_key && !msg.file_exists && !_thumbOverlayShown && (!_noContent || _isViewOnce) && tl && tl !== 'text' && tl !== '' && mt !== 7 && mt !== -1) {
    // Check URL expiry via oe= parameter (UTC timestamp)
    var _dlUrlExpired = false;
    if (msg.cdn_url) {
      var _dlOe = msg.cdn_url.match(/oe=([0-9A-Fa-f]+)/);
      if (_dlOe) _dlUrlExpired = (parseInt(_dlOe[1], 16) * 1000) < Date.now();
    }
    if (_dlUrlExpired) {
      h += '<div class="download-inline" style="opacity:0.5;cursor:default;color:#999"><span class="dl-icon">\u23F3</span> URL expired';
      if (msg.file_size) h += ' (' + fmtSize(msg.file_size) + ')';
      h += '</div>';
    } else {
      var _dlW = msg.media_w || msg.media_width || 0;
      var _dlH = msg.media_h || msg.media_height || 0;
      var _dlIsHd = (_dlW > 1600 || _dlH > 1600);
      h += '<div class="download-inline" onclick="bDl(\'' + id + '\')"><span class="dl-icon">\u2B07</span> Download' + (_dlIsHd ? ' HD' : '') + ' from server';
      if (msg.file_size) h += ' (' + fmtSize(msg.file_size) + ')';
      h += '</div>';
    }
  }

  // File path + hash/recovery resolution indicator (forensic provenance)
  if ((msg.file_path || msg.recovery_method) && tl && tl !== 'text' && tl !== '' && tl !== 'sticker' && mt !== 7 && mt !== -1) {
    var resolvedName = (msg.file_path || '').replace(/\\/g, '/').split('/').pop();
    var origName = msg.orig_file_path ? msg.orig_file_path.replace(/\\/g, '/').split('/').pop() : '';
    var displayName = origName || resolvedName;

    if (msg.recovery_method === 'downloaded') {
      // Our tool downloaded this file from WhatsApp CDN and decrypted it
      h += '<div class="file-path-info">';
      h += '<span class="hash-badge recovered-badge" title="Our tool downloaded this media from WhatsApp CDN and decrypted it">\u2B07 Downloaded &amp; Recovered</span>';
      h += '<span class="hash-matched-file">\u2714 <b>' + esc(resolvedName) + '</b></span>';
      h += '</div>';
    } else if (msg.recovery_method === 'hash_linked') {
      // Distinguish 3 sub-cases based on what happened
      var _srcIsToolDl = (msg.file_path || '').indexOf('recovered_media') >= 0
        || (msg.file_path || '').indexOf('Recovered_') >= 0;
      var _wasDownloadable = msg.has_url && msg.has_key;
      var _hashLabel, _hashTitle;
      if (_srcIsToolDl) {
        // Case 3: Tool downloaded in another chat → hash linked here
        _hashLabel = '\u{1F517} Downloaded by tool elsewhere, linked here via hash';
        _hashTitle = 'Our tool downloaded this file in another chat. Linked here because SHA-256 hash matches.';
      } else if (_wasDownloadable) {
        // Case 1: Was downloadable but not downloaded → found via hash
        _hashLabel = '\u{1F517} Not downloaded, recovered via hash';
        _hashTitle = 'This media was not downloaded in this chat. Same file found in another chat via SHA-256 hash match.';
      } else {
        // Case 2: Media missing (no URL) → found via hash
        _hashLabel = '\u{1F517} Media missing, recovered via hash';
        _hashTitle = 'Media file was missing (no download URL). Same file found in another chat via SHA-256 hash match.';
      }
      h += '<div class="file-path-info">';
      h += '<span class="hash-badge" title="' + _hashTitle + '">' + _hashLabel + '</span>';
      h += '<span class="hash-matched-file">\u2714 <b>' + esc(displayName) + '</b></span>';
      if (origName && origName !== resolvedName) {
        h += '<span class="hash-orig-path">\u{1F4C2} Source: ' + esc(resolvedName) + '</span>';
      }
      if (msg.is_fwd || (msg.file_hash && msg.fwd_score > 0)) {
        h += '<span class="hash-fwd-chain">\u{1F504} Likely forward chain</span>';
      }
      h += '</div>';
    }
  }

  // Link preview (above text, like WhatsApp)
  if (msg.link && (text || msg.caption)) h += renderLinks(text || msg.caption, msg.link, msg.thumb);

  // Text
  var dt = '';
  if (msg.is_revoked && !msg.is_ghost) { dt = '<em style="color:var(--text-secondary)">\u{1F6AB} This message was deleted' + (msg.revoked_by ? ' by ' + esc(msg.revoked_by) : '') + '</em>'; }
  else if (msg.caption) { dt = msg.mentions ? renderMentions(msg.caption, msg.mentions) : proc(msg.caption); }
  else if (text && tl !== 'location' && tl !== 'live_location' && tl !== 'vcard' && tl !== 'poll' && tl !== 'poll_vote' && tl !== 'document' && tl !== 'call_log' && !msg.poll && !msg.call_result && mt !== 20 && mt !== 46) { dt = msg.mentions ? renderMentions(text, msg.mentions) : proc(text); }
  if (dt) h += '<div class="' + (msg.thumb || tl === 'album' ? 'text caption' : 'text') + '">' + dt + '</div>';

  // Reactions
  if (msg.reactions && (msg.reaction_count > 0 || msg.reactions_detail)) {
    var emojis;
    if (typeof Intl !== 'undefined' && Intl.Segmenter) {
      var seg = new Intl.Segmenter('en', { granularity: 'grapheme' });
      emojis = Array.from(seg.segment(msg.reactions), function (s) { return s.segment; });
    } else { emojis = Array.from(msg.reactions); }
    var em = {};
    for (var ri = 0; ri < emojis.length; ri++) { var ch = emojis[ri]; if (ch.codePointAt(0) > 255) em[ch] = (em[ch] || 0) + 1; }
    var pills = Object.entries(em);
    if (pills.length > 0) {
      h += '<div class="reactions-wrap" onclick="toggleReactionDetail(this)">';
      h += '<div class="reactions">';
      pills.forEach(function (pair) {
        h += '<span class="reaction-pill">' + pair[0];
        if (pair[1] > 1) h += '<span class="r-count">' + pair[1] + '</span>';
        h += '</span>';
      });
      h += '</div>';
      // Build inline detail panel (hidden by default)
      if (msg.reactions_detail) {
        h += '<div class="reaction-detail-panel" style="display:none;">';
        var rEntries = msg.reactions_detail.split(';;').filter(Boolean);
        rEntries.forEach(function (re) {
          // Format: emoji:name:phone:timestamp — split by finding first 3 colons
          var ci1 = re.indexOf(':');
          var ci2 = ci1 >= 0 ? re.indexOf(':', ci1 + 1) : -1;
          var ci3 = ci2 >= 0 ? re.indexOf(':', ci2 + 1) : -1;
          var rEmoji, rName, rPhone, rTs;
          if (ci1 >= 0 && ci2 >= 0 && ci3 >= 0) {
            rEmoji = re.substring(0, ci1);
            rName = re.substring(ci1 + 1, ci2);
            rPhone = re.substring(ci2 + 1, ci3);
            rTs = parseInt(re.substring(ci3 + 1)) || 0;
          } else {
            var rp = re.split(':');
            rEmoji = rp[0] || '';
            rName = rp[1] || '';
            rPhone = rp[2] || '';
            rTs = parseInt(rp[3]) || 0;
          }
          if (!rName && !rPhone) rName = 'Unknown';
          else if (!rName) rName = rPhone;
          var initial = rName.charAt(0) === '~' ? rName.charAt(1) : rName.charAt(0);
          var rc = sColor(rName);
          h += '<div class="reaction-row">';
          h += '<div class="react-avatar" style="background:' + rc + '">' + (initial || '?').toUpperCase() + '</div>';
          h += '<div class="react-info"><div class="react-name">' + esc(rName);
          if (rPhone) h += ' <span class="react-phone">(+' + esc(rPhone) + ')</span>';
          h += '</div>';
          if (rTs) h += '<div class="react-ts">' + fmtFullTs(rTs) + '</div>';
          h += '</div>';
          h += '<div class="react-emoji">' + rEmoji + '</div>';
          h += '</div>';
        });
        h += '</div>';
      }
      h += '</div>';
    }
  }

  // Reply/quote count — how many messages quoted/replied to this one
  if (msg.reply_count > 0) {
    h += '<div class="reply-count-badge" onclick="event.stopPropagation();bReplies(' + id + ',\'' + esc(msg.source_key || '').replace(/'/g, "\\'") + '\')">';
    h += '<span class="reply-count-icon">\u21A9</span> ';
    h += '<span class="reply-count-label">' + msg.reply_count + ' repl' + (msg.reply_count > 1 ? 'ies' : 'y') + '</span>';
    h += '<span class="reply-count-arrow">\u203A</span>';
    h += '</div>';
  }

  // Comment thread (channel replies)
  if (msg.comment_count > 0) {
    h += '<div class="comment-thread" onclick="bComments(' + id + ')">';
    h += '<span class="comment-icon">\u{1F4AC}</span> ';
    h += '<span class="comment-label">' + msg.comment_count + ' Replies</span>';
    h += '<span class="comment-arrow">\u203A</span>';
    h += '</div>';
  }

  // Meta row
  h += '<div class="meta-row">';
  if (msg.is_tagged) h += '<span class="tag-flag">\u2691</span>';
  if (msg.is_starred) h += '<span class="star">\u2605</span>';
  if (msg.is_edited && !msg.is_bot) h += '<span class="edited clickable-edit" onclick="event.stopPropagation();bEditHistory(' + id + ')">edited \u270E</span>';
  // AI badge: only for messages FROM Meta AI (not user messages mentioning @Meta AI)
  if (msg.is_bot && !fm && tl !== 'poll' && tl !== 'poll_vote' && tl !== 'call_log' && tl !== 'deleted' && mt !== 7 && mt !== -1 && mt !== 20 && mt !== 46 && mt !== 90) {
    h += '<span class="bot-meta-badge">\u{1F916} AI</span>';
  }
  // Device indicator (skip for bot, system, newsletter, channel, business_api)
  if (msg.platform && !msg.is_bot && mt !== 7 && mt !== -1 &&
    msg.platform !== 'newsletter' && msg.platform !== 'channel_bot' && msg.platform !== 'unknown' && msg.platform !== 'business_api') {
    var _devMap = { 'android': ['Android', 'di-android'], 'iphone': ['iPhone', 'di-iphone'], 'android_linked': ['Android', 'di-android'], 'iphone_linked': ['iPhone', 'di-iphone'], 'companion': ['Web', 'di-web'] };
    var _dm = _devMap[msg.platform];
    if (_dm) h += '<span class="device-indicator ' + _dm[1] + '">' + _dm[0] + '</span>';
  }
  h += '<span class="time">' + fmtFullTs(ts) + '</span>';
  h += ticks(st, fm, id);
  h += '<button class="msg-info-btn" onclick="event.stopPropagation();showForensicInfo(' + id + ')" title="Forensic Info">i</button>';
  if (msg.is_ghost) h += '<span class="ghost-badge">GHOST</span>';
  // Origination flags inline badges — pure bitmask
  // Only the most visually useful bits; full decomposition is in Forensic Info panel.
  var of = msg.oflags || 0;
  if (of & 256)    h += '<span class="oflag-badge oflag-eph" title="Ephemeral / disappearing-chat message (origination_flags bit 8 = 256)">\u23F3</span>';
  if (of & 131072) h += '<span class="oflag-badge oflag-sync" title="Edited message or Meta AI response (origination_flags bit 17 = 131072)">\u270F</span>';
  if (of & 64)     h += '<span class="oflag-badge oflag-multi" title="Image sent to multiple contacts (origination_flags bit 6 = 64)">\u2194</span>';
  h += '</div>';

  if (fm && (msg.delivered_ts || msg.read_ts)) { h += '<div class="forensic-meta">' + (msg.read_ts ? 'Read: ' + fmtFullTs(msg.read_ts) : 'Delivered: ' + fmtFullTs(msg.delivered_ts)) + '</div>'; }
  else if (!fm && msg.delivered_ts) { h += '<div class="forensic-meta">Received: ' + fmtFullTs(msg.delivered_ts) + '</div>'; }
  h += '</div>';  // close .bubble
  h += '</div>';  // close .msg
  return _unreadPrefix + h;
}

// ---- Context menu ----
// Right-click priority:
//   1. Album cell (data-album-child-id) — each cell is its own
//      message, so "Find Similar Images" / "Find Copies" should
//      operate on the *photo* the user actually right-clicked,
//      not on the album-as-a-whole (which has type_label='album'
//      and doesn't pass the image-type filter).
//   2. Otherwise, the closest data-msg-id (regular bubble).
document.addEventListener('contextmenu', function (e) {
  e.preventDefault();
  if (!bridge) return;
  var cell = e.target.closest('[data-album-child-id]');
  if (cell) {
    var cid = cell.dataset.albumChildId;
    if (cid && cid !== '0') {
      bridge.onContextMenu(cid, e.screenX, e.screenY);
      return;
    }
  }
  var el = e.target.closest('[data-msg-id]');
  if (el) bridge.onContextMenu(el.dataset.msgId, e.screenX, e.screenY);
});

// ================================================================
// Virtual Scroll Engine
// ================================================================

var _renderCount = 0;

// Render an EXPLICIT [first,last] range — bypasses the estimate-based
// findItemAtOffset used by renderVisible. Used by scrollToMessage to
// guarantee the target is in the DOM even when heightPositions are
// badly out of sync with reality (every chat has dynamic content, so
// estimates can miss by 10-30 %). DOM-based scrollIntoView after this
// is the authoritative source of truth for target positioning.
function renderRange(first, last) {
  if (totalCount === 0 || !scrollContent) return;
  if (heightDirty) rebuildHeights(0);
  // Windowed flat: re-centre the window on the requested range centre.
  if (_flatWindowed) {
    var mid = Math.floor((first + last) / 2);
    _wFlatRenderWindow(mid);
    return;
  }
  if (_flatRender) {
    if (!_flatRendered) _flatRenderAll();
    return;
  }
  first = Math.max(0, Math.min(totalCount - 1, first));
  last = Math.max(first, Math.min(totalCount - 1, last));
  _renderCoreImpl(first, last);
}

// FLAT RENDER: one innerHTML pass for the entire conversation. No transforms,
// no scroll-driven re-rendering, no coordinate math. `content-visibility:
// auto` (set in CSS on .msg) means the browser skips layout + paint for
// off-screen messages — so this is memory-cheap AND fast on modern engines,
// even for ~50K messages. Called from renderVisible / renderRange when
// `_flatRender` is on; otherwise the virtual-scroll path handles rendering.
function _flatRenderAll() {
  // Unobserve old elements before replacing DOM
  var oldChildren = container.children;
  for (var oi = 0; oi < oldChildren.length; oi++) {
    _resizeObserver.unobserve(oldChildren[oi]);
  }
  // Use arrays + join for large-N — innerHTML += string concat copies the
  // growing string O(N²) at this scale; Array.join is O(N).
  var parts = new Array(totalCount);
  var rendered = 0;
  var missing = 0;
  for (var gi = 0; gi < totalCount; gi++) {
    var msg = getMsg(gi);
    if (msg) {
      var prev = gi > 0 ? getMsg(gi - 1) : null;
      parts[gi] = renderMsg(msg, prev, gi);
      rendered++;
    } else {
      // Minimal tombstone for flat mode — just enough for data-global-idx
      // lookup and natural height. Styled via .msg.tmb CSS.
      parts[gi] = '<div class="msg tmb" data-global-idx="' + gi + '"></div>';
      missing++;
    }
  }
  container.innerHTML = parts.join('');
  renderedFirst = 0;
  renderedLast = totalCount - 1;
  _flatRendered = true;
  // No translateY, no manual scrollContent.height — natural flow handles it.
  // Clear any leftover JS-set overrides from a previous virtualized chat.
  container.style.transform = '';

  // Observe ONLY the real message elements (not tombstones — they have a
  // fixed CSS height so they don't need measurement, and observing 200K+
  // of them would choke the RO flush). Patched-in real msgs get observed
  // individually by _flatPatchRange when they arrive.
  var newChildren = container.children;
  for (var ni = 0; ni < newChildren.length; ni++) {
    var ch = newChildren[ni];
    if (!ch.classList.contains('tmb')) {
      _resizeObserver.observe(ch);
    }
  }

  // Audio state re-apply
  if (activeAudioId) {
    var wf = container.querySelector('[data-waveform-id="' + activeAudioId + '"]');
    if (wf) {
      var bars = wf.querySelectorAll('.bar');
      var fc = Math.floor(activeAudioProgress * bars.length);
      bars.forEach(function (b, i) { b.classList.toggle('filled', i < fc); });
    }
  }
  _renderCount++;
  console.log('[JS] flat render: total=' + totalCount + ', rendered=' + rendered + ', missing=' + missing);
  // Post-render diagnostic: log heights of the REAL messages near the bottom.
  // If they all come out as ~48 px (tombstone height), something's wrong with
  // renderMsg output or CSS is treating them as tombstones.
  if (rendered > 0 && rendered < 100) {
    var sampleStart = Math.max(0, totalCount - rendered);
    var sample = [];
    for (var si = sampleStart; si < totalCount && sample.length < 3; si++) {
      var sch = container.children[si];
      if (!sch) continue;
      sample.push({
        gi: si,
        cls: sch.className,
        h: sch.offsetHeight,
        w: sch.offsetWidth,
        innerLen: sch.innerHTML.length,
      });
    }
    console.log('[JS] flat render sample (last real msgs): ' + JSON.stringify(sample));
  }
  // Use the DOM-authoritative flat-mode request (not the estimate-based one,
  // which would request tiles based on findItemAtOffset of the current
  // scrollTop — unrelated to where the viewport actually is).
  if (missing > 0 && !initialScrollGuard) _flatRequestVisibleTiles();
}

// FLAT PATCH: tile delivery in flat mode. We could trigger a full re-render,
// but that's 285K elements recreated per arriving tile — horrible. Instead,
// locate each affected child by data-global-idx and replaceChild only the
// nodes that actually changed. Keeps tile arrivals O(tile-size), not O(N).
function _flatPatchRange(first, last) {
  if (!_flatRendered) { _flatRenderAll(); return; }
  first = Math.max(0, first);
  last = Math.min(totalCount - 1, last);
  var tmp = document.createElement('div');
  var patched = 0;
  for (var gi = first; gi <= last; gi++) {
    var el = container.querySelector('[data-global-idx="' + gi + '"]');
    if (!el) continue;
    var msg = getMsg(gi);
    if (!msg) continue;   // still a tombstone — leave existing node
    var prev = gi > 0 ? getMsg(gi - 1) : null;
    tmp.innerHTML = renderMsg(msg, prev, gi);
    var fresh = tmp.firstElementChild;
    if (!fresh) continue;
    _resizeObserver.unobserve(el);
    el.parentNode.replaceChild(fresh, el);
    _resizeObserver.observe(fresh);
    patched++;
  }
  if (patched > 0 && _renderCount < 10) {
    console.log('[JS] flat patch: range=' + first + '-' + last + ', patched=' + patched);
  }
  if (patched > 0) {
    _maybeSettleScrollTarget();
    _maybeStickToBottom();
  }
}

// Re-pin scrollTop to scrollHeight if we're inside the bottom-stick window.
// Called from every tile patch in flat and windowed modes. Cancels itself
// if the user scrolled away (so we don't fight them) or the window expired.
//
// EXTEND-ON-TILE-ARRIVAL: each call that actually does work (i.e. scrollTop
// was behind bottom) extends _bottomStickUntil by 2 more seconds — that way,
// as long as tiles keep arriving and growing the content, we keep the user
// pinned at bottom; once tiles stop for 2s, the stick naturally releases.
function _maybeStickToBottom() {
  if (Date.now() > _bottomStickUntil) return;
  if (_userScrolledAway) { _bottomStickUntil = 0; return; }
  var newMax = chatArea.scrollHeight - chatArea.clientHeight;
  if (newMax <= 0) return;
  if (chatArea.scrollTop < newMax - 8) {
    _beginProgScroll();
    chatArea.scrollTop = newMax;
    _endProgScroll();
    // Content just grew — keep sticking for another 2 s
    var ext = Date.now() + 2000;
    if (ext > _bottomStickUntil) _bottomStickUntil = ext;
  }
}

// Re-center the scroll-settle target (set by scrollToMessage) if it has
// drifted more than 40 px off from the viewport's 1/3 point. Called from
// every tile-patch / window-render in flat and windowed modes. Without
// this, tile arrivals above the viewport grow the layout and push the
// target off-screen — user experiences "it scrolled back" after search nav.
function _maybeSettleScrollTarget() {
  if (!_scrollSettleTargetId) return;
  if (Date.now() > _scrollSettleUntil) {
    _scrollSettleTargetId = null;
    return;
  }
  // If the user has taken over with a real scroll input, abandon the settle.
  // _isProgScroll() lets us distinguish our own scrollIntoView from user input
  // (wheel/mousedown/touchstart handlers clear _programmaticScrollUntil when
  // the user interacts, so after user input _isProgScroll() is false).
  if (isUserScrolling && !_isProgScroll()) {
    _scrollSettleTargetId = null;
    _scrollSettleUntil = 0;
    return;
  }
  var el = container.querySelector('[data-msg-id="' + _scrollSettleTargetId + '"]');
  if (!el) return;
  var r = el.getBoundingClientRect();
  var cr = chatArea.getBoundingClientRect();
  // Honour the placement the original scroll-to-message used.  For
  // 'start', the desired top is the viewport top; for 'center', it's
  // the viewport's 1/3 line.
  var desired = (_scrollSettlePlacement === 'start')
    ? cr.top
    : (cr.top + cr.height / 3);
  var off = r.top - desired;
  if (Math.abs(off) > 40) {
    _beginProgScroll();
    el.scrollIntoView({
      block: (_scrollSettlePlacement === 'start') ? 'start' : 'center',
      behavior: 'instant',
    });
    _endProgScroll();
  }
}

// ===== WINDOWED FLAT =====
// Only WINDOW_SIZE real .msg elements are in the DOM. Top & bottom spacers
// at constant AVG_MSG_H per unrendered msg give the scroller the right total
// range. When the viewport drifts beyond SHIFT_THRESHOLD from the window
// centre, we re-render the window centred on the new viewport index.

function _wFlatRenderWindow(centerGi) {
  if (totalCount <= 0) return;
  centerGi = Math.max(0, Math.min(totalCount - 1, centerGi));
  var half = WINDOW_SIZE >> 1;
  var first = Math.max(0, centerGi - half);
  var last = Math.min(totalCount - 1, first + WINDOW_SIZE - 1);
  first = Math.max(0, last - WINDOW_SIZE + 1);

  // ---- Capture scroll anchor BEFORE the innerHTML wipe ----
  // Same idea as _wFlatPatchRange but for the heavyweight full
  // window rebuild.  Without this, a re-render with the same window
  // (e.g. triggered by RO measurement update + scrollToMessage retry)
  // would replace every DOM node — even the message the user was just
  // looking at — and the new layout could put it at a different
  // pixel offset because real heights were already measured for the
  // old layout but the new render uses fresh tombstone-AVG heights
  // until ResizeObserver re-measures.  We pin the topmost in-or-below
  // viewport message that's still in the new window so its on-screen
  // position survives the rebuild.
  var _wflatAnchorGi = -1;
  var _wflatAnchorOffset = 0;
  if (_flatRendered) {
    var _wfCrTop = chatArea.getBoundingClientRect().top;
    var _wfKids = container.children;
    for (var _wfk = 0; _wfk < _wfKids.length; _wfk++) {
      var _wfCh = _wfKids[_wfk];
      if (_wfCh.classList && _wfCh.classList.contains('w-spacer')) continue;
      var _wfG = _wfCh.getAttribute && _wfCh.getAttribute('data-global-idx');
      if (_wfG === null || _wfG === undefined) continue;
      var _wfGi = parseInt(_wfG, 10);
      if (isNaN(_wfGi)) continue;
      // Only useful if the new window will still contain this gi
      if (_wfGi < first || _wfGi > last) continue;
      var _wfRect = _wfCh.getBoundingClientRect();
      if (_wfRect.bottom > _wfCrTop) {
        _wflatAnchorGi = _wfGi;
        _wflatAnchorOffset = _wfRect.top - _wfCrTop;
        break;
      }
    }
  }

  // Unobserve existing real msgs
  var prevChildren = container.children;
  for (var pi = 0; pi < prevChildren.length; pi++) {
    if (!prevChildren[pi].classList.contains('w-spacer')) {
      _resizeObserver.unobserve(prevChildren[pi]);
    }
  }

  var topSpace = first * AVG_MSG_H;
  var botSpace = (totalCount - 1 - last) * AVG_MSG_H;

  var parts = [];
  parts.push('<div class="w-spacer w-spacer-top" style="height:' + topSpace + 'px"></div>');
  var rendered = 0;
  var missing = 0;
  for (var gi = first; gi <= last; gi++) {
    var msg = getMsg(gi);
    if (msg) {
      var prev = gi > 0 ? getMsg(gi - 1) : null;
      parts.push(renderMsg(msg, prev, gi));
      rendered++;
    } else {
      parts.push('<div class="msg tmb" data-global-idx="' + gi + '" style="height:' + AVG_MSG_H + 'px"></div>');
      missing++;
    }
  }
  parts.push('<div class="w-spacer w-spacer-bot" style="height:' + botSpace + 'px"></div>');

  container.innerHTML = parts.join('');
  _windowFirst = first;
  _windowLast = last;
  _flatRendered = true;
  renderedFirst = first;
  renderedLast = last;

  // scrollContent height: totalCount * AVG_MSG_H (consistent estimate).
  // We don't set it via JS here — CSS rule `.flat-windowed #scrollContent`
  // uses height:auto so it follows natural content (spacers + window).
  container.style.transform = '';

  // Observe real msgs for future RO-triggered measurement updates
  var newChildren = container.children;
  for (var ni = 0; ni < newChildren.length; ni++) {
    var ch = newChildren[ni];
    if (!ch.classList.contains('w-spacer') && !ch.classList.contains('tmb')) {
      _resizeObserver.observe(ch);
    }
  }
  console.log('[JS] wflat render: window=[' + first + '-' + last + ']' +
    ' rendered=' + rendered + ' missing=' + missing +
    ' topSpace=' + topSpace + ' botSpace=' + botSpace);

  // Restore the pre-render anchor's screen position so the user sees
  // the same content at the same place after the innerHTML swap.
  // Skipped while bottom-sticking — _maybeStickToBottom takes priority
  // in that case (new messages arriving).
  if (_wflatAnchorGi >= 0
      && !(Date.now() < _bottomStickUntil && !_userScrolledAway)) {
    var _wfNew = container.querySelector('[data-global-idx="' + _wflatAnchorGi + '"]');
    if (_wfNew) {
      var _wfNewCrTop = chatArea.getBoundingClientRect().top;
      var _wfNewRect = _wfNew.getBoundingClientRect();
      var _wfNewOffset = _wfNewRect.top - _wfNewCrTop;
      var _wfDelta = _wfNewOffset - _wflatAnchorOffset;
      if (Math.abs(_wfDelta) > 0.5) {
        _beginProgScroll();
        chatArea.scrollTop = chatArea.scrollTop + _wfDelta;
        _endProgScroll();
        if (_scrollAnchorGi !== null) _scrollAnchorTop += _wfDelta;
      }
    }
  }

  // After any window render, re-pin scroll settle + bottom stick. These are
  // cheap (noops when not active) but guarantee the user's intended target
  // stays on-screen across the innerHTML swap inside the window render.
  _maybeSettleScrollTarget();
  _maybeStickToBottom();

  if (missing > 0 && !initialScrollGuard) _wFlatRequestTiles();
}

// Shift window if viewport drifted outside current window.
// IMPORTANT: clamp centerGi to [0, totalCount-1] BEFORE comparing to
// windowCenter. Without clamping, at the end of the chat scrollTop pushes
// centerGi past totalCount-1, the compare never matches, and we re-render
// the same window forever on every scroll event.
// DOM-based centerGi recovery: walks the rendered children, finds the one
// whose vertical centre is closest to the viewport centre, returns its
// global index.  This is HARD authoritative — no AVG_MSG_H estimate
// involved — and correct even if message heights have drifted from the
// 48 px estimate after RO measurement updates.
//
// Returns -1 if the viewport currently sits over a tombstone-only or
// spacer region (i.e. the user has scrolled into a not-yet-rendered
// area), in which case the caller should fall back to the math
// estimate.
function _wFlatDomVisibleCenterGi() {
  if (!_flatRendered || !container.children.length) return -1;
  var crTop = chatArea.getBoundingClientRect().top;
  var vpCenterPx = crTop + chatArea.clientHeight / 2;
  var bestGi = -1, bestDist = Infinity;
  var kids = container.children;
  for (var i = 0; i < kids.length; i++) {
    var ch = kids[i];
    if (!ch.classList) continue;
    if (ch.classList.contains('w-spacer')) continue;
    if (ch.classList.contains('tmb')) continue;   // tombstone — not a real msg
    var giAttr = ch.getAttribute && ch.getAttribute('data-global-idx');
    if (giAttr === null || giAttr === undefined) continue;
    var gi = parseInt(giAttr, 10);
    if (isNaN(gi)) continue;
    var rect = ch.getBoundingClientRect();
    // Skip elements entirely above or below the viewport — they can't be
    // the visual centre.  Save the layout work for the in-viewport ones.
    if (rect.bottom < crTop) continue;
    if (rect.top > crTop + chatArea.clientHeight) break;   // ordered, so bail
    var elCenter = (rect.top + rect.bottom) / 2;
    var d = Math.abs(elCenter - vpCenterPx);
    if (d < bestDist) { bestDist = d; bestGi = gi; }
  }
  return bestGi;
}

function _wFlatMaybeShift() {
  if (totalCount <= 0) return;
  // DURING SCROLL-SETTLE: do NOT shift the window. scrollToMessage set the
  // window centred on the target, and a shift would swap the target out of
  // the DOM — breaking the settle watchdog's scrollIntoView and making the
  // user "lose" the message they just clicked on. Hold the window fixed
  // until the settle window expires.
  if (_scrollSettleTargetId && Date.now() < _scrollSettleUntil) return;
  // Shift cooldown: prevents back-to-back shifts from thrashing the DOM
  // while the user is actively dragging the scrollbar. Each innerHTML
  // replace causes layout to re-run over the next 1-2 frames, during which
  // scrollTop settles; a second shift in that window double-shakes the
  // scroll position (the "scrollbar drag + release + still moving" bug).
  if (Date.now() - _lastWindowShiftAt < SHIFT_COOLDOWN_MS) return;
  var st = _getScrollTop();
  var vpCenter = st + chatArea.clientHeight / 2;
  var centerGi;
  // PRIORITY 1: ground truth from rendered DOM.  When at least one real
  // message intersects the viewport, use its global index directly —
  // this is correct regardless of what AVG_MSG_H estimates might say
  // and survives RO height updates without drift.
  var domCenter = _wFlatDomVisibleCenterGi();
  if (domCenter >= 0) {
    centerGi = domCenter;
  } else {
    // PRIORITY 2: incremental prediction from a fresh calibration anchor
    // (set by scrollToMessage on a successful landing).  The anchor is
    // invalidated when the user scrolls far away from it.
    var _useAnchor = (_scrollAnchorGi !== null
                      && (Date.now() - _scrollAnchorAt) < SCROLL_ANCHOR_TTL_MS
                      && Math.abs(st - _scrollAnchorTop) < ANCHOR_INVALIDATE_PX);
    if (_useAnchor) {
      var _delta = st - _scrollAnchorTop;
      centerGi = Math.max(0, Math.min(totalCount - 1,
        _scrollAnchorGi + Math.round(_delta / AVG_MSG_H)
      ));
    } else {
      if (_scrollAnchorGi !== null) {
        _scrollAnchorGi = null;
      }
      // PRIORITY 3 (last resort): pure AVG_MSG_H estimate.  Compounds
      // any drift from the true heights, so we only land here when
      // the viewport is over a tombstone-only / spacer region (i.e.
      // the user has scrolled FAR away from the rendered window).
      centerGi = Math.max(0, Math.min(totalCount - 1, Math.floor(vpCenter / AVG_MSG_H)));
    }
  }
  var windowCenter = (_windowFirst + _windowLast) >> 1;
  if (Math.abs(centerGi - windowCenter) <= SHIFT_THRESHOLD) return;
  // Compute what the new window WOULD be — skip render if it's identical.
  var half = WINDOW_SIZE >> 1;
  var newFirst = Math.max(0, centerGi - half);
  var newLast = Math.min(totalCount - 1, newFirst + WINDOW_SIZE - 1);
  newFirst = Math.max(0, newLast - WINDOW_SIZE + 1);
  if (newFirst === _windowFirst && newLast === _windowLast) return;
  _lastWindowShiftAt = Date.now();
  _wFlatRenderWindow(centerGi);
}

// Patch delivered tile — only if it overlaps current window
//
// SCROLL-POSITION PRESERVATION:
// Each tile patch replaces tombstones (48 px each) with real messages
// (heights 30-300 px).  When patches happen ABOVE the viewport, the
// total height of content above the viewport changes — pushing the
// user's visible content up or down by the height delta.  The chat
// CSS sets `overflow-anchor: auto` to delegate this to the browser,
// but Qt WebEngine doesn't always honour it (especially under
// rapid back-to-back patches).
//
// So we manually anchor on the topmost element at-or-below the
// viewport top that's NOT being replaced by this patch, capture its
// pixel offset before the patch, and restore that offset after.
// Result: tile patches above the viewport become visually invisible
// to the user — no more "I saw the image, then the chat scrolled
// somewhere else" after a navigate-to-message.
function _wFlatPatchRange(first, last) {
  if (!_flatRendered) return;   // will render on first window build
  if (last < _windowFirst || first > _windowLast) return;   // outside window

  // ---- Capture anchor BEFORE patching ----
  // Pick the topmost child whose globalIdx is OUTSIDE [first, last]
  // (won't be replaced) AND whose bottom is at or below viewport top.
  // That gives us a stable element whose screen position we can
  // preserve across the DOM swap.
  var _crTop = chatArea.getBoundingClientRect().top;
  var _anchorEl = null, _anchorGi = -1, _anchorOffset = 0;
  var _kids = container.children;
  for (var _ki = 0; _ki < _kids.length; _ki++) {
    var _ch = _kids[_ki];
    if (_ch.classList && _ch.classList.contains('w-spacer')) continue;
    var _gAttr = _ch.getAttribute && _ch.getAttribute('data-global-idx');
    if (_gAttr === null || _gAttr === undefined) continue;
    var _kgi = parseInt(_gAttr, 10);
    if (isNaN(_kgi)) continue;
    // Skip elements that are themselves about to be replaced
    if (_kgi >= first && _kgi <= last) continue;
    var _kr = _ch.getBoundingClientRect();
    if (_kr.bottom > _crTop) {
      _anchorEl = _ch;
      _anchorGi = _kgi;
      _anchorOffset = _kr.top - _crTop;
      break;
    }
  }

  // ---- Apply patches ----
  var tmp = document.createElement('div');
  var patched = 0;
  var lo = Math.max(first, _windowFirst);
  var hi = Math.min(last, _windowLast);
  for (var gi = lo; gi <= hi; gi++) {
    var el = container.querySelector('[data-global-idx="' + gi + '"]');
    if (!el) continue;
    var msg = getMsg(gi);
    if (!msg) continue;
    var prev = gi > 0 ? getMsg(gi - 1) : null;
    tmp.innerHTML = renderMsg(msg, prev, gi);
    var fresh = tmp.firstElementChild;
    if (!fresh) continue;
    if (el.classList.contains('tmb')) {
      // tombstones aren't observed
    } else {
      _resizeObserver.unobserve(el);
    }
    el.parentNode.replaceChild(fresh, el);
    _resizeObserver.observe(fresh);
    patched++;
  }
  if (patched > 0 && _renderCount < 20) {
    console.log('[JS] wflat patch: range=' + lo + '-' + hi + ' patched=' + patched);
  }

  // ---- Restore anchor position AFTER patching ----
  // Only restore if the user is NOT actively bottom-sticking (new
  // messages arriving) — in that case _maybeStickToBottom takes
  // precedence and pulls scroll to the bottom.  Settle re-snap also
  // runs below; if it fires (within 4 s of scrollToMessage) it'll
  // re-snap our adjustment to keep the original target centred,
  // which is the desired UX.
  if (patched > 0 && _anchorGi >= 0
      && !(Date.now() < _bottomStickUntil && !_userScrolledAway)) {
    var _newAnchor = container.querySelector('[data-global-idx="' + _anchorGi + '"]');
    if (_newAnchor) {
      var _newCrTop = chatArea.getBoundingClientRect().top;
      var _newRect = _newAnchor.getBoundingClientRect();
      var _newOffset = _newRect.top - _newCrTop;
      var _delta = _newOffset - _anchorOffset;
      if (Math.abs(_delta) > 0.5) {
        _beginProgScroll();
        chatArea.scrollTop = chatArea.scrollTop + _delta;
        _endProgScroll();
        // Also shift the calibration anchor's scrollTop by the same
        // delta so post-patch user-scrolls compute centerGi correctly
        // against the new pixel landscape.
        if (_scrollAnchorGi !== null) _scrollAnchorTop += _delta;
      }
    }
  }

  if (patched > 0) {
    _maybeSettleScrollTarget();
    _maybeStickToBottom();
  }
}

// Request tiles for WINDOW + PREFETCH_RADIUS on each side, prioritized by
// distance from viewport centre so visible tiles arrive first, then outer
// prefetch fills in while the user reads. As each tile arrives, loadMessages
// calls us again to pipeline the next batch.
function _wFlatRequestTiles() {
  if (initialScrollGuard || totalCount === 0) return;
  sweepStalledTiles();
  if (_pendingScrollMsgId !== null) return;
  if (tileLoadingCount >= MAX_CONCURRENT_TILES) return;

  var startGi = Math.max(0, _windowFirst - PREFETCH_RADIUS);
  var endGi = Math.min(totalCount - 1, _windowLast + PREFETCH_RADIUS);
  var startTile = Math.floor(startGi / TILE_SIZE);
  var endTile = Math.floor(endGi / TILE_SIZE);

  // Viewport tile = what the user is actually looking at RIGHT NOW
  var st = _getScrollTop();
  var vpCenter = st + chatArea.clientHeight / 2;
  var vpTile = Math.floor(Math.max(0, Math.min(totalCount - 1, vpCenter / AVG_MSG_H)) / TILE_SIZE);

  // Collect tiles we still need, sorted by distance from viewport tile
  var need = [];
  for (var ti = startTile; ti <= endTile; ti++) {
    if (tileMap[ti] || pendingTileRequests[ti]) continue;
    var gidx = ti * TILE_SIZE;
    if (gidx >= totalCount) continue;
    need.push({ ti: ti, dist: Math.abs(ti - vpTile) });
  }
  need.sort(function (a, b) { return a.dist - b.dist; });

  var requested = 0;
  for (var i = 0; i < need.length; i++) {
    if (tileLoadingCount + requested >= MAX_CONCURRENT_TILES) break;
    var t = need[i];
    pendingTileRequests[t.ti] = true;
    tileRequestTimestamps[t.ti] = Date.now();
    tileLoadingCount++;
    if (bridge) bridge.onLoadRange(t.ti * TILE_SIZE);
    requested++;
  }
  if (requested > 0 || need.length > 0) {
    console.log('[JS] wflat tile request: ' + requested + '/' + need.length +
      ' (vpTile=' + vpTile + ', window=' + _windowFirst + '-' + _windowLast + ')');
  }
}

function renderVisible() {
  if (totalCount === 0 || !scrollContent) return;
  if (heightDirty) rebuildHeights(0);

  // WINDOWED FLAT MODE: shift check is the only thing we need. If the window
  // doesn't need shifting, do nothing. If it does, render a fresh window.
  if (_flatWindowed) {
    if (!_flatRendered) {
      _wFlatRenderWindow(totalCount > 0 ? totalCount - 1 : 0);
    } else {
      _wFlatMaybeShift();
    }
    return;
  }

  // FLAT RENDER MODE (small chats): paint every message once, then never again.
  if (_flatRender) {
    if (!_flatRendered || renderedFirst !== 0 || renderedLast !== totalCount - 1) {
      _flatRenderAll();
    }
    return;
  }

  var scrollTop = _getScrollTop();
  var vpH = chatArea.clientHeight;
  if (vpH <= 0) return;

  // Binary search for first visible item
  var rawFirst = findItemAtOffset(scrollTop);
  var rawLast = findItemAtOffset(scrollTop + vpH);
  var first = Math.max(0, rawFirst - BUFFER);
  var last = Math.min(totalCount - 1, rawLast + BUFFER);

  // ──────────────────────────────────────────────────────────────
  // ANTI-FLASH (search-result scroll): while a scroll target is pending,
  // DO NOT repaint tombstones for the scrollTop=0 viewport — that's the
  // flash we observed as "range=0-33, rendered=0, missing=34" right before the
  // target tile arrived. scrollToMessage will issue its own renderRange
  // as soon as idToGlobal[pending] is populated.
  // ──────────────────────────────────────────────────────────────
  if (_pendingScrollMsgId !== null) {
    var _viewportHasData = false;
    for (var _gi = rawFirst; _gi <= rawLast; _gi++) {
      if (getMsg(_gi)) { _viewportHasData = true; break; }
    }
    if (!_viewportHasData) return;   // <- RETURN, not just trigger more loads
  }

  // Skip if range hasn't shifted enough — reduces innerHTML churn
  if (renderedFirst >= 0 && Math.abs(first - renderedFirst) < 8 && Math.abs(last - renderedLast) < 8) return;

  _renderCoreImpl(first, last);
}

function _renderCoreImpl(first, last) {
  // Position container at the prefix-sum offset of the first rendered item
  var translateY = getItemTop(first);
  container.style.transform = 'translateY(' + translateY + 'px)';

  // Unobserve old elements before replacing DOM
  var oldChildren = container.children;
  for (var oi = 0; oi < oldChildren.length; oi++) {
    _resizeObserver.unobserve(oldChildren[oi]);
  }

  var html = '';
  var rendered = 0;
  var missing = 0;
  for (var gi = first; gi <= last; gi++) {
    var msg = getMsg(gi);
    if (msg) {
      var prev = gi > 0 ? getMsg(gi - 1) : null;
      html += renderMsg(msg, prev, gi);
      rendered++;
    } else {
      html += renderTombstone(gi);
      missing++;
    }
  }

  container.innerHTML = html;
  renderedFirst = first;
  renderedLast = last;

  // Observe new elements with ResizeObserver
  var newChildren = container.children;
  for (var ni = 0; ni < newChildren.length; ni++) {
    _resizeObserver.observe(newChildren[ni]);
  }

  // Capture whether the user was AT THE BOTTOM before we mutate scrollContent.
  // Use scrollContent.offsetHeight — that's the AUTHORITATIVE scroll bound
  // (#messages is position:absolute so it doesn't contribute).
  var _wasAtBottom = false;
  if (renderedLast === totalCount - 1 && chatArea && scrollContent) {
    var _prevMax = scrollContent.offsetHeight - chatArea.clientHeight;
    if (_prevMax > 0 && chatArea.scrollTop >= _prevMax - 40) _wasAtBottom = true;
  }

  // Re-sync scrollContent height after every render. The old code only did
  // this when reaching the last message — which meant heightTotal could drift
  // below scrollContent.offsetHeight on fast scrolls through the middle,
  // leaving the container positioned at a translate-Y that put all rendered
  // content BELOW the viewport. User saw an empty chat until another scroll
  // retriggered render. Unconditional sync cures the blank-on-fast-scroll.
  _updateScrollHeight();

  // If the user was pinned at the bottom before this render AND the bottom
  // moved, re-pin using scrollContent.offsetHeight (authoritative after
  // the _updateScrollHeight call above).
  if (_wasAtBottom && !isUserScrolling && !_userScrolledAway
      && renderedLast === totalCount - 1 && chatArea && scrollContent) {
    var _newMax = scrollContent.offsetHeight - chatArea.clientHeight;
    if (_newMax > 0 && Math.abs(chatArea.scrollTop - _newMax) > 2) {
      chatArea.scrollTop = _newMax;
    }
  }

  // Re-apply the scroll-to-target highlight if still within its 2.5 s
  // window — renderVisible rewrites innerHTML which detaches the
  // previously-highlighted node, so without this the pulse disappears
  // after the first render cycle following scrollToMessage.
  if (_highlightTargetMsgId !== null && Date.now() < _highlightUntil) {
    var _hl = container.querySelector('[data-msg-id="' + _highlightTargetMsgId + '"]');
    if (_hl && !_hl.classList.contains('highlight-pulse')) {
      _hl.classList.add('highlight-pulse');
    }
  } else if (_highlightTargetMsgId !== null) {
    _highlightTargetMsgId = null;   // window expired
  }

  _renderCount++;
  if (_renderCount <= 3 || _renderCount % 200 === 0) {
    console.log('[JS] renderVisible #' + _renderCount + ': range=' + first + '-' + last +
      ', rendered=' + rendered + ', missing=' + missing +
      ', tiles=' + Object.keys(tileMap).length);
  }

  // Audio state
  if (activeAudioId) {
    var wf = container.querySelector('[data-waveform-id="' + activeAudioId + '"]');
    if (wf) {
      var bars = wf.querySelectorAll('.bar');
      var fc = Math.floor(activeAudioProgress * bars.length);
      bars.forEach(function (b, i) { b.classList.toggle('filled', i < fc); });
      var card = container.querySelector('[data-audio-id="' + activeAudioId + '"]');
      if (card) { var btn = card.querySelector('.play-btn'); if (btn) btn.textContent = activeAudioProgress > 0 && activeAudioProgress < 1 ? '\u23F8' : '\u25B6'; }
    }
  }

  // Update sticky date indicator
  updateStickyDate();

  if (missing > 0 && !initialScrollGuard) requestVisibleTiles();
}

function updateStickyDate() {
  if (!_stickyDateEl || totalCount === 0) return;
  // DOM-authoritative: find the first rendered child whose bottom crosses
  // the viewport's top. Previously used findItemAtOffset(scrollTop) which
  // depends on the estimate-based heightPositions — when those drift from
  // reality the date chip gets stuck on one date while the user scrolls
  // through many days ().
  var dateStr = '';
  if (container.children.length) {
    var cr = chatArea.getBoundingClientRect();
    for (var ci = 0; ci < container.children.length; ci++) {
      var ch = container.children[ci];
      var r = ch.getBoundingClientRect();
      if (r.bottom <= cr.top) continue;  // still above viewport
      var gi = parseInt(ch.dataset.globalIdx, 10);
      if (isNaN(gi)) continue;
      // Walk forward from that idx to find a real chat message (skip
      // system / subtype / deleted markers) with a usable timestamp.
      for (var si = gi; si < Math.min(gi + 20, totalCount); si++) {
        var sm = getMsg(si);
        if (sm && sm.ts && sm.type !== -1 && sm.type !== 7 && sm.type !== 112) {
          dateStr = fmtDate(sm.ts);
          break;
        }
      }
      break;
    }
  }
  if (!dateStr || dateStr === _lastStickyDate) return;
  _lastStickyDate = dateStr;
  _stickyDateEl.textContent = dateStr;
  _stickyDateEl.style.opacity = '1';
  clearTimeout(_stickyDateTimer);
  _stickyDateTimer = setTimeout(function () {
    _stickyDateEl.style.opacity = '0';
  }, 1500);
}

function measureRenderedHeights() {
  // Measure all currently rendered children and update measuredHeights
  if (!container.children.length) return false;
  var anyChanged = false;
  var minChanged = totalCount;
  for (var i = 0; i < container.children.length; i++) {
    var ch = container.children[i];
    var gi = parseInt(ch.dataset.globalIdx, 10);
    if (isNaN(gi)) continue;
    var h = ch.offsetHeight;
    if (h > 0 && (measuredHeights[gi] === undefined || Math.abs(measuredHeights[gi] - h) > 1)) {
      measuredHeights[gi] = h;
      anyChanged = true;
      if (gi < minChanged) minChanged = gi;
    }
  }
  // Estimate calibration previously shrank unmeasured estimates mid-flight
  // to match measured heights — but this mutated `heightTotal` while the
  // doScroll stability loop was still running, causing intermittent
  // "scroll didn't land at bottom" bugs under real image-loading cadence.
  // With `_updateScrollHeight` now pinning scrollContent to rendered
  // bottom when we're at end-of-chat, the calibration isn't needed for
  // the bottom-pin to be accurate. Skip it.
  if (anyChanged) {
    rebuildHeights(minChanged);
    // CRITICAL FIX: The DOM container's absolute position MUST be kept in
    // sync with heightPositions. If rebuildHeights profoundly shifted the
    // offset (e.g. from 18M to 32M pixels), and we don't update translateY,
    // the subsequent lastEl.scrollIntoView() will push the view to 18M instead
    // of 32M, putting it entirely out of bounds.
    if (renderedFirst >= 0 && container.children.length > 0) {
      container.style.transform = 'translateY(' + getItemTop(renderedFirst) + 'px)';
    }
    _updateScrollHeight();
  }
  return anyChanged;
}

function calibrateEstH(jumpToBottom) {
  // Measure visible children, rebuild prefix sums, update scroll height
  measureRenderedHeights();
  if (jumpToBottom && totalCount > 0) {
    _setScrollTop(heightTotal);
  }
}

// ---- Scroll date overlay ----
var scrollDateEl = document.getElementById('scrollDate');
var scrollDateVisTimer = 0;

function updateScrollDate() {
  if (!scrollDateEl || totalCount === 0) return;
  // DOM-authoritative top-visible msg lookup. Previously used
  // findItemAtOffset(scrollTop) + getMsg(topIdx), but in windowed-flat mode
  // that gi's tile may not be loaded in tileMap (estimate-based index points
  // to an unloaded region), so msg was null and the date silently dropped —
  // user saw the counter "N / M" without the date for exactly those chats
  // where the estimate landed on a tombstone range. Instead, walk the DOM
  // children top-down, find the first one whose bottom crosses the viewport
  // top, and use its data-global-idx — that's ALWAYS a gi that's currently
  // rendered, so getMsg returns real data when it's real, or we walk forward
  // a few to find one. Mirrors updateStickyDate's proven logic.
  var dateStr = '';
  if (container.children.length) {
    var cr0 = chatArea.getBoundingClientRect();
    for (var ci0 = 0; ci0 < container.children.length; ci0++) {
      var ch0 = container.children[ci0];
      // Skip w-spacer (windowed flat) and anything without a gi
      if (ch0.classList && ch0.classList.contains('w-spacer')) continue;
      var r0 = ch0.getBoundingClientRect();
      if (r0.bottom <= cr0.top) continue;   // still above viewport
      var gi0 = parseInt(ch0.dataset.globalIdx, 10);
      if (isNaN(gi0)) continue;
      // Walk forward up to 20 msgs to find one with a usable timestamp
      for (var si0 = gi0; si0 < Math.min(gi0 + 20, totalCount); si0++) {
        var sm0 = getMsg(si0);
        if (sm0 && sm0.ts && sm0.type !== -1 && sm0.type !== 7 && sm0.type !== 112) {
          dateStr = fmtDate(sm0.ts);
          break;
        }
      }
      break;
    }
  }
  // Fallback: estimate-based if DOM walk failed (very early load state)
  if (!dateStr) {
    var topIdx = findItemAtOffset(_getScrollTop());
    var msg = getMsg(topIdx);
    if (msg && msg.ts) dateStr = fmtDate(msg.ts);
  }
  // Show the BOTTOM visible message index so "end of chat" reads "N / N".
  //
  // We USED to use (heightTotal - scrollTop - clientHeight) but heightTotal
  // is an estimate that can disagree with the real DOM scrollHeight by
  // hundreds of kilopixels on large chats (e.g. 285 K msgs). When they
  // diverge, remaining is positive while the user is visually at the end
  // and the counter reads "285,059 / 285,070" — a stale bug the user
  // reported directly. Use the real scrollHeight + DOM-authoritative
  // last-rendered-element check instead.
  var bottomIdx;
  var physRemaining = chatArea.scrollHeight - chatArea.scrollTop - chatArea.clientHeight;
  if (physRemaining < 10 && renderedLast === totalCount - 1) {
    bottomIdx = totalCount - 1;   // at the very end
  } else if (container.children.length) {
    // Authoritative: last DOM child whose bottom is within the viewport
    var cr = chatArea.getBoundingClientRect();
    var vpBottom = cr.bottom;
    var found = -1;
    for (var ci = container.children.length - 1; ci >= 0; ci--) {
      var ch = container.children[ci];
      var rb = ch.getBoundingClientRect().bottom;
      if (rb <= vpBottom + 2) {
        var gi2 = parseInt(ch.dataset.globalIdx, 10);
        if (!isNaN(gi2)) { found = gi2; break; }
      }
    }
    bottomIdx = found >= 0 ? found
      : Math.min(totalCount - 1, findItemAtOffset(_getScrollTop() + chatArea.clientHeight));
  } else {
    bottomIdx = Math.min(totalCount - 1, findItemAtOffset(_getScrollTop() + chatArea.clientHeight));
  }
  var posLabel = (bottomIdx + 1).toLocaleString() + ' / ' + totalCount.toLocaleString();
  scrollDateEl.innerHTML = dateStr
    ? '<div class="sd-date">' + dateStr + '</div><div class="sd-pos">' + posLabel + '</div>'
    : '<div class="sd-pos">' + posLabel + '</div>';
  scrollDateEl.classList.add('visible');
  clearTimeout(scrollDateVisTimer);
  scrollDateVisTimer = setTimeout(function () { scrollDateEl.classList.remove('visible'); }, 1500);
}

// ---- Scroll handler: render immediately, load tiles ONLY after scroll settles ----
chatArea.addEventListener('scroll', function () {
  // WINDOWED FLAT MODE (large chats): shift window if viewport drifted past
  // threshold from current window centre. Otherwise just update UI chrome.
  if (_flatWindowed) {
    _lastScrollTime = Date.now();
    isUserScrolling = true;
    // Only cancel bottom-stick + scroll-settle if a REAL user input fired
    // within the last 500 ms. Overflow-anchor auto-scrolls during tile
    // patches ALSO trigger this scroll handler, but they aren't user input;
    // cancelling stick on them caused the "chat didn't start at the end"
    // drift (seeing msg 244,556 / N instead of ending
    // exactly at N because overflow-anchor auto-scrolls during the
    // tile-patch burst were mis-classified as user scroll-away).
    var userScrolled = (Date.now() - _lastUserInputAt) < 500;
    if (userScrolled && _bottomStickUntil > 0 && !_isProgScroll()) {
      var gapFromBot = (chatArea.scrollHeight - chatArea.scrollTop) - chatArea.clientHeight;
      if (gapFromBot > 80) { _userScrolledAway = true; _bottomStickUntil = 0; }
    }
    if (userScrolled && _scrollSettleTargetId && !_isProgScroll()) {
      _scrollSettleTargetId = null;
      _scrollSettleUntil = 0;
    }
    clearTimeout(scrollActiveTimer);
    scrollActiveTimer = setTimeout(function () { isUserScrolling = false; }, SCROLL_ACTIVE_MS);
    if (_stickyDateEl && _stickyDateEl.textContent) {
      _stickyDateEl.style.opacity = '1';
      clearTimeout(_stickyDateTimer);
      _stickyDateTimer = setTimeout(function () { _stickyDateEl.style.opacity = '0'; }, 1500);
    }
    if (!rafId) {
      rafId = requestAnimationFrame(function () {
        rafId = 0;
        _wFlatMaybeShift();
        updateStickyDate();
        updateScrollDate();
      });
    }
    clearTimeout(scrollIdleTimer);
    scrollIdleTimer = setTimeout(function () { _wFlatRequestTiles(); }, SCROLL_IDLE_MS);
    return;
  }

  // FLAT RENDER MODE (small chats): browser handles everything natively.
  if (_flatRender) {
    _lastScrollTime = Date.now();
    isUserScrolling = true;
    // Cancel residual stick/settle ONLY on real user input (see comment
    // in windowed-flat path above).
    var userScrolledF = (Date.now() - _lastUserInputAt) < 500;
    if (userScrolledF && _bottomStickUntil > 0 && !_isProgScroll()) {
      var gapFromBotF = (chatArea.scrollHeight - chatArea.scrollTop) - chatArea.clientHeight;
      if (gapFromBotF > 80) { _userScrolledAway = true; _bottomStickUntil = 0; }
    }
    if (userScrolledF && _scrollSettleTargetId && !_isProgScroll()) {
      _scrollSettleTargetId = null;
      _scrollSettleUntil = 0;
    }
    clearTimeout(scrollActiveTimer);
    scrollActiveTimer = setTimeout(function () { isUserScrolling = false; }, SCROLL_ACTIVE_MS);
    if (_stickyDateEl && _stickyDateEl.textContent) {
      _stickyDateEl.style.opacity = '1';
      clearTimeout(_stickyDateTimer);
      _stickyDateTimer = setTimeout(function () {
        _stickyDateEl.style.opacity = '0';
      }, 1500);
    }
    if (!rafId) {
      rafId = requestAnimationFrame(function () {
        rafId = 0;
        updateStickyDate();
        updateScrollDate();
      });
    }
    // Tile-request settle timer — only fires if any visible msg is a tombstone
    clearTimeout(scrollIdleTimer);
    scrollIdleTimer = setTimeout(function () {
      _flatRequestVisibleTiles();
    }, SCROLL_IDLE_MS);
    return;
  }

  var inProg = _isProgScroll();

  // During programmatic scrolls: cancel any throttled rAF so scrollIntoView's
  // async-dispatched scroll events don't override an explicit renderRange.
  // BUT do NOT early-return — we still want the fast-scroll anti-blank path
  // below to repaint if the viewport has drifted off the rendered range.
  // The original boolean-flag version returned here, and if _programmaticScroll
  // ever leaked to TRUE permanently (any missed clear across ~15 set-sites),
  // the viewport went blank forever. With _isProgScroll() now self-expiring
  // (PROG_SCROLL_MAX_MS = 1500 ms) and the anti-blank path unconditional, a
  // leaked flag is at worst a 1.5 s anomaly, not a permanent broken state.
  if (inProg) {
    if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }
  } else {
    // Mark as actively scrolling
    isUserScrolling = true;
    _lastScrollTime = Date.now();
    // If user scrolls AWAY from bottom during initial load, stop auto-snapping
    if (_stayAtBottom && !initialScrollGuard) {
      var gap = (heightTotal - _getScrollTop()) - chatArea.clientHeight;
      if (gap > 80) {
        _userScrolledAway = true;  // Mark as scrolled away — prevents FAILSAFE re-snap
      }
    }
    clearTimeout(scrollActiveTimer);
    scrollActiveTimer = setTimeout(function () {
      isUserScrolling = false;
    }, SCROLL_ACTIVE_MS);

    // Show sticky date immediately during scroll
    if (_stickyDateEl && _stickyDateEl.textContent) {
      _stickyDateEl.style.opacity = '1';
      clearTimeout(_stickyDateTimer);
      _stickyDateTimer = setTimeout(function () {
        _stickyDateEl.style.opacity = '0';
      }, 1500);
    }
  }

  // FAST-SCROLL ANTI-BLANK (runs in both prog and normal scrolls): if the
  // viewport has drifted OUTSIDE the current rendered range, repaint
  // SYNCHRONOUSLY. Without this, old content stays transformed to an
  // off-screen translateY for one or more frames, producing the "blank flash"
  // the during fast scroll. Running this even during prog-
  // scroll provides a self-correcting safety net — any leaked prog-scroll
  // flag can't leave the viewport visually broken.
  if (totalCount > 0 && renderedFirst >= 0) {
    var _fastFirst = findItemAtOffset(_getScrollTop());
    var _fastLast = findItemAtOffset(_getScrollTop() + chatArea.clientHeight);
    var _far = _fastLast < renderedFirst || _fastFirst > renderedLast;
    if (_far) {
      if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }
      renderVisible();   // synchronous repaint — cures the blank
      updateScrollDate();
    }
  }

  if (inProg) return;

  // Normal throttled path for small scrolls
  if (!rafId) {
    rafId = requestAnimationFrame(function () {
      rafId = 0;
      renderVisible();
      updateScrollDate();
    });
  }
  // Tile requests ONLY after scroll settles AND only if VISIBLE area has tombstones.
  // BUFFER-zone tombstones are ignored — they don't affect what the user sees.
  clearTimeout(scrollIdleTimer);
  if (!initialScrollGuard) {
    scrollIdleTimer = setTimeout(function () {
      // Always sweep stalled requests first — otherwise a stuck tile holds
      // its slot and the viewport-missing check bails out silently.
      sweepStalledTiles();
      if (tileLoadingCount >= MAX_CONCURRENT_TILES) return;
      // Strict viewport check — only the messages actually visible, NOT buffer
      var vpFirst = findItemAtOffset(_getScrollTop());
      var vpLast = findItemAtOffset(_getScrollTop() + chatArea.clientHeight);
      vpFirst = Math.max(0, vpFirst);
      vpLast = Math.min(totalCount - 1, vpLast);
      var hasMissing = false;
      // Sample every 5th item — TILE_SIZE is now 100, so ~20 checks / viewport
      for (var ci = vpFirst; ci <= vpLast; ci += 5) {
        if (!getMsg(ci)) { hasMissing = true; break; }
      }
      // Also check first and last (edges most likely to have tombstones)
      if (!hasMissing && !getMsg(vpFirst)) hasMissing = true;
      if (!hasMissing && !getMsg(vpLast)) hasMissing = true;
      if (hasMissing) requestVisibleTiles();
    }, SCROLL_IDLE_MS);
  }
}, { passive: true });  // Passive: unblock compositor thread for instant scroll

window.addEventListener('resize', function () {
  // FLAT / WINDOWED FLAT: resize changes viewport size, not what to render.
  // (Windowed will detect any drift on next scroll via _wFlatMaybeShift.)
  if (_flatRender || _flatWindowed) return;
  renderedFirst = -1;
  renderVisible();
});

// Detect REAL user interaction during programmatic scroll operations.
// The scroll handler is suppressed during _programmaticScroll, so isUserScrolling
// is never set. These input-event listeners set _userScrolledAway which the
// stability loop already checks, allowing it to stop and hand control back.
chatArea.addEventListener('wheel', function () {
  _lastUserInputAt = Date.now();
  if (_isProgScroll()) { _userScrolledAway = true; _endProgScroll(); }
}, { passive: true });
chatArea.addEventListener('mousedown', function () {
  _lastUserInputAt = Date.now();
  if (_isProgScroll()) { _userScrolledAway = true; _endProgScroll(); }
});
chatArea.addEventListener('touchstart', function () {
  _lastUserInputAt = Date.now();
  if (_isProgScroll()) { _userScrolledAway = true; _endProgScroll(); }
}, { passive: true });
chatArea.addEventListener('keydown', function (e) {
  // Arrow keys / Page Up/Down / Home / End — real user scroll inputs
  if (e.key === 'ArrowUp' || e.key === 'ArrowDown' || e.key === 'PageUp'
      || e.key === 'PageDown' || e.key === 'Home' || e.key === 'End') {
    _lastUserInputAt = Date.now();
  }
});

// ---- Keyboard navigation: ArrowUp/Down = 1 msg, PageUp/Down = ~viewport ----
document.addEventListener('keydown', function (e) {
  if (totalCount === 0) return;
  var step = 0;
  var stepH = 60; // approximate single message height for keyboard nav
  if (e.key === 'ArrowUp') step = -stepH;
  else if (e.key === 'ArrowDown') step = stepH;
  else if (e.key === 'PageUp') step = -(chatArea.clientHeight - stepH);
  else if (e.key === 'PageDown') step = chatArea.clientHeight - stepH;
  else if (e.key === 'Home') { _setScrollTop(0); e.preventDefault(); return; }
  else if (e.key === 'End') { _setScrollTop(scrollContent.offsetHeight); e.preventDefault(); return; }
  if (step) {
    _setScrollTop(_getScrollTop() + step);
    e.preventDefault();
  }
});

// ================================================================
// Public API
// ================================================================

function setTotalCount(n) {
  // If the count changes (new messages appended, or a new conversation with a
  // different length), the "highest bottom ever observed" floor for the
  // PREVIOUS count is meaningless — the true bottom sits at a different
  // pixel. Reset it so the floor rebuilds from the new renders.
  if (n !== totalCount) _maxRealBottom = 0;
  totalCount = n;
  heightDirty = true;
  rebuildHeights(0);
  _updateScrollHeight();
  // Decide rendering mode. Sticky for this conversation.
  _flatRender = (totalCount > 0 && totalCount <= FLAT_RENDER_MAX);
  _flatWindowed = (totalCount > FLAT_RENDER_MAX);  // large chats use windowed
  if (_flatRender) {
    chatArea.classList.add('flat-render');
    chatArea.classList.remove('flat-windowed');
    console.log('[JS] render mode: FLAT (full) for ' + totalCount + ' msgs');
  } else if (_flatWindowed) {
    chatArea.classList.add('flat-windowed');
    chatArea.classList.remove('flat-render');
    console.log('[JS] render mode: WINDOWED FLAT for ' + totalCount + ' msgs, window=' + WINDOW_SIZE);
  } else {
    chatArea.classList.remove('flat-render');
    chatArea.classList.remove('flat-windowed');
    console.log('[JS] render mode: VIRTUALIZED (legacy) for ' + totalCount + ' msgs');
  }
  // Kick off the tile watchdog (once per conversation). Runs independently
  // of user scroll, so a tile that stuck during a pause gets re-requested
  // without the user having to jiggle the wheel.
  if (!_tileWatchdogTimer) {
    _tileWatchdogTimer = setInterval(function () {
      if (totalCount === 0) return;
      sweepStalledTiles();
      // PROACTIVE TILE FILL: every 1.5 s, if the viewport contains any
      // tombstones (FLAT or virtualized), request the tiles that cover
      // them.  This is the safety net for "user dragged the scrollbar
      // to the top in 1 frame" — the scroll-idle timer fires once and
      // its 80 ms window can race against the very fast drag, so the
      // single SCROLL_IDLE_MS callback may slip through with stale state.
      // The watchdog catches any orphan tombstone within 1.5 s.
      //
      // Gates (in order):
      //   1. initialScrollGuard - first-load setup still in progress
      //   2. _pendingScrollMsgId - scroll target hasn't landed yet
      //   3. _scrollSettleTargetId & _scrollSettleUntil - scroll-to-msg
      //      JUST landed and the 4-s settle window is active; the
      //      scroll might still drift as tile patches grow heights and
      //      _maybeSettleScrollTarget is fighting that.  Firing tile
      //      requests inside this window would chase the wrong probe
      //      area (whatever the scroll happens to be RIGHT NOW vs the
      //      stable position the settle is converging to).
      //   4. isUserScrolling - user is actively dragging; the scroll
      //      handler's own SCROLL_IDLE_MS path will pick it up the
      //      moment they release.
      try {
        if (initialScrollGuard) return;
        if (_pendingScrollMsgId !== null) return;
        if (_scrollSettleTargetId && Date.now() < _scrollSettleUntil) return;
        if (isUserScrolling) return;
        if (_flatWindowed)    _wFlatRequestTiles();
        else if (_flatRender) _flatRequestVisibleTiles();
        else                  requestVisibleTiles();
      } catch (e) {}
    }, 1500);
  }
  // BOTTOM PIN WATCHDOG: catches the case where bubble heights grow
  // (images / payment-card measurements) AFTER initial doScroll lands
  // at the bottom. The renderer's own RO-flush path sometimes doesn't
  // re-snap — this watchdog is the safety net that forces the user
  // back onto the last message when they're NOT actively scrolling.
  if (!window.__bottomPinWatchdog) {
    window.__bottomPinWatchdog = setInterval(function () {
      if (totalCount === 0 || !scrollContent || !chatArea || !container) return;
      // FLAT / WINDOWED FLAT: browser handles scrolling correctly — no drift
      // to correct, no coordinate math to reconcile. Do NOT pin to bottom.
      if (_flatRender || _flatWindowed) return;
      if (renderedLast !== totalCount - 1) return;        // not showing last msg
      if (isUserScrolling) return;                        // user is actively scrolling
      if (_isProgScroll()) return;                        // already programmatic
      if (Date.now() - _lastScrollTime < 500) return;
      // #messages is position:absolute so it doesn't contribute to the scroll
      // bound — scrollContent.style.height IS the authoritative max. If it's
      // stale (smaller than measured container bottom), scrollTop gets
      // clamped short of the last message. Run _updateScrollHeight FIRST to
      // grow scrollContent to match the real container bottom, THEN pin.
      _updateScrollHeight();
      var newMax = scrollContent.offsetHeight - chatArea.clientHeight;
      if (newMax <= 0) return;
      var gap = newMax - chatArea.scrollTop;
      if (gap > 2) {
        _beginProgScroll();
        chatArea.scrollTop = newMax;
        _endProgScroll();
        _userScrolledAway = false;
      }
    }, 250);
  }
}

function setLoadGeneration(gen) {
  _loadGeneration = gen || 0;
  if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }
  clearTimeout(scrollIdleTimer);
  clearTimeout(scrollActiveTimer);
  clearTimeout(_roTimer);
  pendingTileRequests = {};
  tileRequestTimestamps = {};
  tileLoadingCount = 0;
  _pendingScrollMsgId = null;
  _stayAtBottom = false;
  _stayAtBottomUntil = 0;
  _userScrolledAway = false;
  _endProgScroll();
}

function loadMessages(globalStart, msgs, gen) {
  if (gen !== undefined && gen !== null && gen !== _loadGeneration) {
    console.log('[JS] stale loadMessages ignored: start=' + globalStart + ' gen=' + gen + ' current=' + _loadGeneration);
    return;
  }
  // First batch of a new chat has arrived — hide the loading
  // spinner that ``chat_viewer_page.load_conversation`` raised
  // before kicking off its synchronous setup.  Idempotent on
  // subsequent tile fetches (showLoading(false) on an already-
  // hidden spinner is a no-op).
  showLoading(false);
  // Empty msgs means Python encountered an error fetching this tile.
  // Clear the pending slot so a future scroll can re-request it.
  if (!msgs || msgs.length === 0) {
    var errTi = Math.floor(globalStart / TILE_SIZE);
    if (pendingTileRequests[errTi]) {
      tileLoadingCount = Math.max(0, tileLoadingCount - 1);
      delete pendingTileRequests[errTi];
      delete tileRequestTimestamps[errTi];
      console.log('[JS] loadMessages: empty tile ' + errTi + ' — slot freed');
    }
    return;
  }
  console.log('[JS] loadMessages: start=' + globalStart + ' count=' + msgs.length + ' total=' + totalCount + ' initDone=' + initialScrollDone);

  var tileIdx = Math.floor(globalStart / TILE_SIZE);
  if (globalStart === tileIdx * TILE_SIZE && msgs.length <= TILE_SIZE) {
    var oldCount = tileMap[tileIdx] ? tileMap[tileIdx].length : 0;
    tileMap[tileIdx] = msgs;
    totalDataCount += (msgs.length - oldCount);
    touchTile(tileIdx);
    if (pendingTileRequests[tileIdx]) tileLoadingCount = Math.max(0, tileLoadingCount - 1);
    delete pendingTileRequests[tileIdx];
    delete tileRequestTimestamps[tileIdx];
  } else {
    var idx = 0, curGlobal = globalStart;
    while (idx < msgs.length) {
      var ti = Math.floor(curGlobal / TILE_SIZE);
      var posInTile = curGlobal - (ti * TILE_SIZE);
      var space = TILE_SIZE - posInTile;
      var count = Math.min(space, msgs.length - idx);
      if (!tileMap[ti]) { tileMap[ti] = []; totalDataCount += count; }
      else { totalDataCount += count; }
      for (var j = 0; j < count; j++) {
        var pos = posInTile + j;
        while (tileMap[ti].length <= pos) tileMap[ti].push(null);
        tileMap[ti][pos] = msgs[idx + j];
      }
      touchTile(ti);
      if (pendingTileRequests[ti]) tileLoadingCount = Math.max(0, tileLoadingCount - 1);
      delete pendingTileRequests[ti];
      delete tileRequestTimestamps[ti];
      idx += count; curGlobal += count;
    }
  }

  for (var i = 0; i < msgs.length; i++) {
    var m = msgs[i];
    if (m && m.id && m.id > 0) idToGlobal[m.id] = globalStart + i;
    if (m && m.source_key) keyToGlobal[m.source_key] = globalStart + i;
  }

  evictTiles();

  // Rebuild prefix sums — suppress tile requests during this entire block
  // to prevent the cascade: loadMessages → renderVisible → scroll → requestVisibleTiles → loadMessages
  var wasGuard = initialScrollGuard;
  initialScrollGuard = true;

  rebuildHeights(globalStart);
  _updateScrollHeight();

  if (!initialScrollDone) {
    initialScrollDone = true;
    initialScrollGuard = true;

    // If there's a pending scrollToMessage, skip doScroll entirely —
    // scrollToMessage will handle positioning after data loads.
    // BUT if the newly-loaded tile ALREADY contains the target (id is now
    // in idToGlobal), kick scrollToMessage IMMEDIATELY. Previously the
    // retry lived below after this early-return, making it unreachable on
    // the initial-load-with-pending path (confirmed in tests/scroll_harness).
    if (_pendingScrollMsgId !== null) {
      console.log('[JS] skipping doScroll — pendingScrollMsgId=' + _pendingScrollMsgId);
      _stayAtBottom = false;
      var _pendingGi = idToGlobal[_pendingScrollMsgId];
      if (_pendingGi !== undefined) {
        var _retryId = _pendingScrollMsgId;
        _pendingScrollMsgId = null;
        setTimeout(function () {
          initialScrollGuard = false;
          scrollToMessage(_retryId);
        }, 50);
      } else {
        // Target not yet in this tile — Python will deliver the right one
        // via set_messages_at; the retry block below (after the else arm)
        // will catch it when that happens.
        setTimeout(function () { initialScrollGuard = false; requestVisibleTiles(); }, 200);
      }
      return;
    }

    // WINDOWED FLAT MODE (large chats): render window centred on last msg,
    // then scroll to bottom. Tiny DOM (~WINDOW_SIZE msgs + 2 spacers).
    if (_flatWindowed) {
      var wFlatDoScroll = function (attempt) {
        if (chatArea.clientHeight <= 0) {
          if (attempt < 25) setTimeout(function () { wFlatDoScroll(attempt + 1); }, 80);
          return;
        }
        _wFlatRenderWindow(totalCount - 1);   // window centred on last msg
        requestAnimationFrame(function () {
          chatArea.scrollTop = chatArea.scrollHeight;
          requestAnimationFrame(function () {
            chatArea.scrollTop = chatArea.scrollHeight;   // stability pass
            initialScrollGuard = false;
            _endProgScroll();
            // Arm the bottom-stick window. Every subsequent tile patch
            // re-pins scrollTop to the new scrollHeight AND extends the
            // stick deadline by 2 more seconds (see _maybeStickToBottom).
            // Start at 10 s so a slow initial tile-burst can't outlast the
            // stick; extension-on-patch keeps us pinned as long as tiles
            // are still arriving. Cancelled the moment the user scrolls
            // away from bottom (scroll handler clears _bottomStickUntil).
            _bottomStickUntil = Date.now() + 10000;
            _userScrolledAway = false;
            console.log('[JS] wflat doScroll done: scrollTop=' + chatArea.scrollTop + ' scrollH=' + chatArea.scrollHeight);
            _wFlatRequestTiles();
          });
        });
      };
      _beginProgScroll();
      wFlatDoScroll(0);
      return;
    }

    // FLAT RENDER MODE (small chats): paint every .msg once, scroll to bottom.
    if (_flatRender) {
      var flatDoScroll = function (attempt) {
        if (chatArea.clientHeight <= 0) {
          if (attempt < 25) setTimeout(function () { flatDoScroll(attempt + 1); }, 80);
          return;
        }
        // Ensure the flat paint has happened at least once. In normal flow
        // _flatPatchRange triggered it already; this is a safety net for the
        // initialScrollDone=false + no-data-yet path.
        if (!_flatRendered) _flatRenderAll();
        requestAnimationFrame(function () {
          var lastEl = container.children.length ? container.children[container.children.length - 1] : null;
          if (lastEl) lastEl.scrollIntoView({ block: 'end', behavior: 'instant' });
          else chatArea.scrollTop = chatArea.scrollHeight;
          // Stability pass: after the browser lays out the near-bottom
          // elements (images, content-visibility-recovered heights), the true
          // bottom may have shifted. Re-snap once on the next frame.
          requestAnimationFrame(function () {
            var lastEl2 = container.children.length ? container.children[container.children.length - 1] : null;
            if (lastEl2) lastEl2.scrollIntoView({ block: 'end', behavior: 'instant' });
            initialScrollGuard = false;
            _endProgScroll();
            console.log('[JS] flat doScroll done: scrollTop=' + chatArea.scrollTop + ' scrollH=' + chatArea.scrollHeight + ' lastEl.bottom=' + (lastEl2 ? Math.round(lastEl2.getBoundingClientRect().bottom) : 'n/a'));
          });
        });
      };
      _beginProgScroll();
      flatDoScroll(0);
      return;
    }

    var doScroll = function (attempt) {
      // GUARD: WebEngine page layout must be complete before we can scroll.
      if (chatArea.clientHeight <= 0) {
        if (attempt < 25) setTimeout(function () { doScroll(attempt + 1); }, 80);
        else { console.warn('[doScroll] layout never completed'); _endProgScroll(); }
        return;
      }

      console.log('[JS] doScroll attempt=' + attempt + ' clientH=' + chatArea.clientHeight + ' scrollH=' + chatArea.scrollHeight + ' total=' + totalCount);

      // Cancel any pending rAF from previous scroll events — a stale
      // renderVisible() would override our explicit renderRange.
      if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }

      // STEP 1: Explicitly render the last chunk using renderRange.
      // renderRange GUARANTEES the last message is in the DOM (no estimates).
      var lastIdx = totalCount - 1;
      var firstIdx = Math.max(0, lastIdx - BUFFER * 2);
      renderRange(firstIdx, lastIdx);

      // STEP 2: Measure rendered elements to calibrate height estimates.
      measureRenderedHeights();
      _updateScrollHeight();

      // STEP 3: Scroll the last DOM element into view.
      // _programmaticScroll is already TRUE (set before rAF) — scroll handler
      // will cancel any rAF it receives and return early.
      var lastEl = container.children.length ? container.children[container.children.length - 1] : null;
      if (lastEl) {
        lastEl.scrollIntoView({ block: 'end', behavior: 'instant' });
      } else {
        _setScrollTop(heightTotal - chatArea.clientHeight);
      }

      // STEP 4: Grow scrollContent if actual content overflows estimate.
      if (lastEl && scrollContent) {
        var _txm = (container.style.transform || '').match(/translateY\(([-\d.e+]+)/);
        var _ty = _txm ? parseFloat(_txm[1]) : 0;
        var realBottom = _ty + container.offsetHeight;
        var curH = parseFloat(scrollContent.style.height || '0');
        console.log('[JS] doScroll realBottom=' + Math.round(realBottom) + ' scrollContentH=' + Math.round(curH) + ' lastGI=' + (lastEl.dataset ? lastEl.dataset.globalIdx : '?'));
        if (realBottom > curH) {
          scrollContent.style.height = Math.ceil(realBottom) + 'px';
          lastEl.scrollIntoView({ block: 'end', behavior: 'instant' });
        }
      }

      // Cancel any rAF that scrollIntoView may have queued via async scroll event
      if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }

      var remaining = (heightTotal - _getScrollTop()) - chatArea.clientHeight;
      var atBottom = remaining < 10;
      console.log('[JS] doScroll final: scrollTop=' + Math.round(_getScrollTop()) + ' remaining=' + Math.round(remaining) + ' atBottom=' + atBottom + ' renderedRange=' + renderedFirst + '-' + renderedLast);
      if (!atBottom && attempt < 4) {
        setTimeout(function () { doScroll(attempt + 1); }, 120);
      } else {
        // One final snap
        var finalLast = container.children.length ? container.children[container.children.length - 1] : null;
        if (finalLast) {
          finalLast.scrollIntoView({ block: 'end', behavior: 'instant' });
        } else {
          _setScrollTop(heightTotal - chatArea.clientHeight);
        }
        if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }
        console.log('[JS] doScroll DONE: scrollTop=' + Math.round(_getScrollTop()) + ' scrollH=' + Math.round(chatArea.scrollHeight) + ' clientH=' + chatArea.clientHeight);

        // Keep re-snapping to bottom for 4 seconds after initial scroll
        _stayAtBottom = true;
        _userScrolledAway = false;
        _stayAtBottomUntil = Date.now() + 700;

        // STABILITY LOOP: _programmaticScroll stays TRUE throughout.
        // The scroll handler cancels any rAF it receives during this time.
        // Real user interaction (wheel/mousedown/touchstart) sets
        // _userScrolledAway and releases _programmaticScroll via the
        // input-event listeners registered on chatArea.
        var _stableCount = 0;
        var _attempts = 0;
        var _stabilityCheck = function () {
          if (_userScrolledAway || !_stayAtBottom) {
            // Real user took over — release everything
            initialScrollGuard = false;
            _endProgScroll();
            requestVisibleTiles();
            return;
          }
          _attempts++;
          var gap = (heightTotal - _getScrollTop()) - chatArea.clientHeight;
          if (gap > 20) {
            var fs = container.children.length ? container.children[container.children.length - 1] : null;
            if (fs) fs.scrollIntoView({ block: 'end', behavior: 'instant' });
            else _setScrollTop(heightTotal - chatArea.clientHeight);
            if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }
            _stableCount = 0;
          } else {
            _stableCount++;
          }
          // Done when stable for 3 consecutive checks OR exceeded 20 attempts (2s)
          if (_stableCount >= 1 || _attempts >= 5) {
            initialScrollGuard = false;
            var fin = container.children.length ? container.children[container.children.length - 1] : null;
            if (fin) fin.scrollIntoView({ block: 'end', behavior: 'instant' });
            if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }
            _endProgScroll();  // Release — normal scroll handling resumes
            _stayAtBottom = false;
            console.log('[JS] doScroll stable after ' + _attempts + ' attempts');
            requestVisibleTiles();
            return;
          }
          setTimeout(_stabilityCheck, 100);
        };
        setTimeout(_stabilityCheck, 100);

      }
    };
    // Hold _programmaticScroll TRUE for the ENTIRE doScroll operation.
    // This prevents async scroll events from scrollIntoView from firing
    // renderVisible() via rAF — which would use findItemAtOffset (estimate-based)
    // and override our explicit renderRange with a wrong range.
    _beginProgScroll();
    if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }
    requestAnimationFrame(function () { doScroll(0); });
  } else {
    // Re-render if data overlaps viewport — but NOT during a programmatic
    // scroll operation (doScroll/scrollToMessage). In those cases the explicit
    // renderRange is authoritative and renderVisible would override it with
    // an estimate-based range, potentially kicking the target out of the DOM.
    if (_flatWindowed) {
      // Windowed flat: patch if tile overlaps window (else it will naturally
      // render when the window shifts to that region — data is already in
      // tileMap). Then pipeline the next prefetch batch.
      _wFlatPatchRange(globalStart, globalStart + msgs.length - 1);
      _wFlatRequestTiles();
    } else if (_flatRender) {
      // Full flat: patch just the affected DOM nodes.
      _flatPatchRange(globalStart, globalStart + msgs.length - 1);
    } else if (!_isProgScroll()) {
      var vpFirst = findItemAtOffset(_getScrollTop()) - BUFFER;
      var vpLast = findItemAtOffset(_getScrollTop() + chatArea.clientHeight) + BUFFER;
      if (globalStart + msgs.length > vpFirst && globalStart < vpLast) {
        renderedFirst = -1;
        renderVisible();
      }
    }
    // Restore guard AFTER renderVisible — prevents cascade
    initialScrollGuard = wasGuard;

    // SAFETY RE-SNAP: if the bottom tile just arrived and we're near the expected
    // bottom (within 200px), heights may have shifted after measurement.
    // Re-snap to chatArea.scrollHeight so the last message is always visible.
    // Skip if user explicitly scrolled away or programmatic scroll in progress.
    var _isBottomTile = (globalStart + msgs.length >= totalCount);
    if (false && _isBottomTile && !wasGuard && !isUserScrolling && !_userScrolledAway && !_isProgScroll()) {
      var _snapGap = (heightTotal - _getScrollTop()) - chatArea.clientHeight;
      if (_snapGap < 200 && _snapGap >= 0) {
        // We're near the bottom — re-snap after heights settle
        setTimeout(function () {
          if (!isUserScrolling && !_userScrolledAway && !_isProgScroll()) {
            _beginProgScroll();
            _setScrollTop(heightTotal - chatArea.clientHeight);
            _endProgScroll();
            if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }
            renderedFirst = -1;
            renderVisible();
          }
        }, 80);
      }
    }

    // After a short delay, allow new tile requests for the actual viewport
    if (!wasGuard) {
      setTimeout(function () {
        if (initialScrollGuard) return;
        // Flat/windowed use their own DOM-authoritative tile request path —
        // the estimate-based requestVisibleTiles would ask for the wrong
        // tiles (estimates don't match real scroll position).
        if (_flatWindowed) { _wFlatRequestTiles(); return; }
        if (_flatRender)   { _flatRequestVisibleTiles(); return; }
        requestVisibleTiles();
      }, 100);
    }
  }

  // If there's a pending scroll target and the newly loaded tile contains it,
  // retry the scroll now that the data is available in idToGlobal.
  if (_pendingScrollMsgId !== null && idToGlobal[_pendingScrollMsgId] !== undefined) {
    var _retryId = _pendingScrollMsgId;
    _pendingScrollMsgId = null;
    // Use setTimeout so the current setMessagesAt finishes first
    setTimeout(function () { scrollToMessage(_retryId); }, 50);
  } else if (_pendingScrollMsgId !== null && msgs && msgs.length > 0) {
    // Safety net: pending scroll target wasn't in this tile (target lives
    // in a sibling tile that hasn't arrived yet, or the target_gi the
    // Python side computed has drifted from this viewer's filter state).
    //
    // Without this, ``renderVisible`` keeps bailing out at the
    // _viewportHasData guard while the browser sits at chatArea.scrollTop=0
    // and the user stares at an empty chat area indefinitely.  Land at
    // the start of the tile we just loaded so they see SOMETHING; we
    // keep ``_pendingScrollMsgId`` set so if the right tile arrives
    // later, its loadMessages callback can still retry the precise scroll.
    //
    // NB: the scroll container is ``chatArea``, NOT the window — using
    // ``window.scrollTo`` here was a no-op (the document itself doesn't
    // overflow, only chatArea does), which is why earlier the user
    // reported the chat was still empty after this branch supposedly
    // ran.
    if (start >= 0 && start < totalCount && getMsg(start)) {
      var _y = getItemTop(start);
      chatArea.scrollTop = Math.max(0, _y - 100);   // -100 for a little headroom
      console.log('[JS] Pending target ' + _pendingScrollMsgId
                  + ' not in this tile; landed at gi=' + start + ' as fallback');
    }
  }
}

function clearMessages() {
  // Unobserve all elements
  var ch = container.children;
  for (var ci = 0; ci < ch.length; ci++) _resizeObserver.unobserve(ch[ci]);

  tileMap = {}; _tileLastAccess = {}; _tileAccessCounter = 0; totalDataCount = 0; totalCount = 0;
  idToGlobal = {}; keyToGlobal = {};
  _pendingScrollMsgId = null;
  _highlightTargetMsgId = null; _highlightUntil = 0;
  _scrollSettleTargetId = null; _scrollSettleUntil = 0;
  // Reset scroll-anchor calibration so the new conversation doesn't
  // inherit stale (gi, scrollTop) data from the previous one.
  _scrollAnchorGi = null; _scrollAnchorTop = 0; _scrollAnchorAt = 0;
  _firstUnreadMsgId = 0;
  _bottomStickUntil = 0;
  renderedFirst = -1; renderedLast = -1;
  initialScrollDone = false; initialScrollGuard = false;
  activeAudioId = 0; activeAudioProgress = 0;
  _returnToMsgId = null; _hideReturnBtn();
  _renderCount = 0; pendingTileRequests = {}; tileRequestTimestamps = {}; tileLoadingCount = 0;
  measuredHeights = {}; heightPositions = null; heightTotal = 0; heightDirty = true;
  _estScale = 1.0;   // reset calibration so each new chat starts from neutral estimates
  _maxRealBottom = 0;  // reset max-rendered-bottom floor so a new conversation starts fresh
  _flatRender = false; _flatRendered = false;
  _flatWindowed = false; _windowFirst = -1; _windowLast = -1;
  if (chatArea) {
    chatArea.classList.remove('flat-render');
    chatArea.classList.remove('flat-windowed');
  }
  provenanceCache = {};
  isUserScrolling = false;
  _userScrolledAway = false;
  _suppressAnchor = false;
  _endProgScroll();
  _lastScrollTime = 0;
  clearTimeout(scrollIdleTimer); clearTimeout(scrollActiveTimer); clearTimeout(_roTimer);
  container.innerHTML = '';
  if (scrollContent) scrollContent.style.height = '0px';
  hideForensicInfo(); showLoading(false);
}

function setConfig(cfg) { isGroup = cfg.is_group || false; ownerLabel = cfg.owner_label || ''; }

// First unread message id. When renderMsg sees this id it prepends an
// "Unread messages" divider row above it. Cleared to 0 when the
// conversation changes (clearMessages resets it).
//
// IMPORTANT: this setter is called EARLY in the chat-open sequence (before
// loadMessages delivers tiles). It must NOT force a re-render — if it did,
// we'd render 267 empty tombstones with no data yet, then flatDoScroll's
// "if (!_flatRendered) _flatRenderAll()" would skip (we're already marked
// rendered), and the user would stare at 267 empty tombstones. Just update
// the id — the first natural render picks it up. A re-render is only
// needed if the DOM already has real msgs; in that case patch the single
// affected node if it's in the DOM.
var _firstUnreadMsgId = 0;
function setFirstUnreadMsgId(msgId) {
  var prev = _firstUnreadMsgId;
  _firstUnreadMsgId = msgId | 0;
  // Only patch in place if we've already rendered real content AND the
  // divider anchor msg actually exists in the current DOM.
  if (!_flatRendered) return;
  if (prev === _firstUnreadMsgId) return;
  // Re-render just the two affected msg nodes (old anchor and new anchor).
  // In flat/windowed, a node replacement via _*PatchRange is cheap.
  if (_flatRender) {
    if (prev)               _flatPatchRange(idToGlobal[prev] | 0, idToGlobal[prev] | 0);
    if (_firstUnreadMsgId)  _flatPatchRange(idToGlobal[_firstUnreadMsgId] | 0, idToGlobal[_firstUnreadMsgId] | 0);
  } else if (_flatWindowed) {
    if (prev)               _wFlatPatchRange(idToGlobal[prev] | 0, idToGlobal[prev] | 0);
    if (_firstUnreadMsgId)  _wFlatPatchRange(idToGlobal[_firstUnreadMsgId] | 0, idToGlobal[_firstUnreadMsgId] | 0);
  }
}

function updateSingleMessage(msgId, msgArray) {
  // Update a single message's data in the tile map and re-render in place
  if (!msgArray || msgArray.length === 0) return;
  var gi = idToGlobal[msgId];
  if (gi === undefined) return;
  var ti = Math.floor(gi / TILE_SIZE);
  var li = gi - ti * TILE_SIZE;
  if (tileMap[ti]) {
    tileMap[ti][li] = msgArray[0];
    // Force re-render to pick up updated data (e.g., downloaded media)
    renderedFirst = -1;
    renderVisible();
  }
}

function scrollToMessage(msgId, _isReturn, _placement) {
  // _placement: 'center' (default) or 'start'.
  //   'start' is used by the first-unread auto-jump on chat open — the
  //   unread divider lands at the top of the viewport with unread
  //   messages stacked below it (matches WhatsApp's native UX).  Without
  //   this, scroll-settle keeps re-centering the divider to viewport's
  //   1/3 point, so when older-message tiles arrive the user sees the
  //   scroll position move "up to a random place" as the divider gets
  //   pushed down by content above and the watchdog snaps it back.
  var _ph = (_placement === 'start') ? 'start' : 'center';
  var gi = idToGlobal[msgId];
  if (gi === undefined) {
    // Store as pending — will be retried when setMessagesAt delivers the tile
    _pendingScrollMsgId = msgId;
    if (bridge) bridge.onScrollToUnloaded(String(msgId));
    return;
  }
  // Target found — clear any pending scroll
  _pendingScrollMsgId = null;

  // Mark as the sticky highlight target so renderVisible re-applies the
  // .highlight-pulse class to each fresh DOM node created during the 4-5
  // render waves that follow (see `_highlightTargetMsgId` at file top).
  _highlightTargetMsgId = msgId;
  _highlightUntil = Date.now() + 2500;
  // Scroll-settle window: for the next 4 seconds, any tile arrival in
  // windowed/flat mode will re-snap this target if it has drifted off.
  // The failure mode was "search go-to-msg went there, then
  // scrolled back somewhere" — caused by tombstone→real replacements above
  // the viewport changing layout without overflow-anchor catching up.
  _scrollSettleUntil = Date.now() + 4000;
  _scrollSettleTargetId = msgId;
  _scrollSettlePlacement = _ph;   // remember: 'start' or 'center'

  // IDEMPOTENCE — Python schedules a retry timer after delivering a tile,
  // and JS's loadMessages has its own pending-retry. When these overlap,
  // back-to-back scrollToMessage calls on the same target used to rebuild
  // the DOM and cancel each other, producing the "dancing like hell"
  // behaviour. If the element is already rendered AND already within the
  // top-third of the viewport, this call is a no-op (we're already there).
  var _already = container.querySelector('[data-msg-id="' + msgId + '"]');
  if (_already) {
    var _crI = chatArea.getBoundingClientRect();
    var _rI = _already.getBoundingClientRect();
    var _relTop = _rI.top - _crI.top;
    // Good-position thresholds depend on placement: for 'center' anywhere in
    // the top 8%–60% counts as good; for 'start' we want it pinned in the
    // top 0%–18% so the unread divider really sits at the top.
    var _okMin = (_ph === 'start') ? 0 : _crI.height * 0.08;
    var _okMax = (_ph === 'start') ? _crI.height * 0.18 : _crI.height * 0.60;
    if (_relTop >= _okMin && _relTop <= _okMax) {
      // Refresh highlight for repeat clicks — restart the CSS animation
      _already.classList.remove('highlight-pulse');
      void _already.offsetWidth;
      _already.classList.add('highlight-pulse');
      // Anchor calibration even on the no-op path: this gi is at the
      // viewport's good position, so subsequent shift predictions
      // should reference it (otherwise a stale anchor from a previous
      // scroll-to-message could mislead _wFlatMaybeShift).
      _scrollAnchorGi  = gi;
      _scrollAnchorTop = chatArea.scrollTop;
      _scrollAnchorAt  = Date.now();
      return;
    }
  }
  // CRITICAL: disable stay-at-bottom so the FAILSAFE re-snap doesn't
  // override our scroll position back to the bottom after 2 seconds
  _stayAtBottom = false;
  _stayAtBottomUntil = 0;
  _userScrolledAway = true;  // Prevent any scheduled FAILSAFE from firing
  // Suppress tile requests during the multi-pass correction to avoid cascade
  initialScrollGuard = true;

  // Hold _programmaticScroll TRUE for the entire scroll-to-message operation.
  // This prevents async scroll events from scrollIntoView from firing
  // renderVisible() via rAF, which would use findItemAtOffset (estimate-based)
  // and override our explicit renderRange — kicking the target out of the DOM.
  _beginProgScroll();
  // Cancel any pending rAF from previous scroll events
  if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }

  // ROBUST scroll-to-message — DOM-authoritative, no estimate dependency.
  // Uses renderRange to EXPLICITLY put the target in the DOM, then
  // scrollIntoView on the real DOM element for pixel-perfect positioning.
  renderRange(gi - BUFFER, gi + BUFFER);

  // Belt-and-braces scroll: chatArea.scrollTop = explicit pixel offset.
  //
  // In windowed-flat mode the rendered messages live INSIDE a 700k+ px
  // top spacer.  Qt WebEngine has bugs where ``scrollIntoView`` on an
  // element nested under a tall flex/spacer layout silently no-ops if
  // the call happens before the layout has settled (which it does in
  // the very first scrollToMessage of a chat-open).  When that no-op
  // happened the user saw chatArea.scrollTop=0 — empty white spacer —
  // even though our rendered window was correctly positioned 700k px
  // below.  Setting scrollTop directly from getItemTop(gi) is layout-
  // independent and always lands.  scrollIntoView still runs in the
  // rAF below as a refinement (object-relative for sub-px alignment).
  var _ph_offset = (_ph === 'start') ? 0 : (chatArea.clientHeight / 3);
  chatArea.scrollTop = Math.max(0, getItemTop(gi) - _ph_offset);

  requestAnimationFrame(function () {
    var el = container.querySelector('[data-msg-id="' + msgId + '"]');
    if (!el) { _endProgScroll(); initialScrollGuard = false; return; }
    el.scrollIntoView({ block: _ph, behavior: 'instant' });
    // Cancel any rAF that the scroll event may have queued
    if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }
    // Highlight directly — we trust this exact DOM node is still live
    el.classList.remove('highlight-pulse');
    void el.offsetWidth;  // reflow to restart CSS animation
    el.classList.add('highlight-pulse');
    // Stability pass at +200 ms: if ResizeObserver-triggered measurements
    // shifted the element noticeably, re-snap once more. Re-queries DOM.
    setTimeout(function () {
      var el3 = container.querySelector('[data-msg-id="' + msgId + '"]');
      if (el3) {
        var r3 = el3.getBoundingClientRect();
        var cr3 = chatArea.getBoundingClientRect();
        // For 'start' placement, target should sit near top (≤ 10% of
        // viewport).  For 'center', target should sit at ~1/3 from top.
        var _desiredTop = (_ph === 'start') ? 0 : (cr3.height / 3);
        var off3 = (r3.top - cr3.top) - _desiredTop;
        if (Math.abs(off3) > 80) {
          el3.scrollIntoView({ block: _ph, behavior: 'instant' });
          if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }
        }
      }
      // Calibration anchor: lock the (gi, scrollTop) we landed on.
      // _wFlatMaybeShift uses this for incremental centerGi prediction,
      // so any accumulated AVG_MSG_H estimate error cancels out — the
      // user can scroll without the window snapping 200-300 indices
      // away from where they actually are.  Captured AFTER scrollIntoView
      // and the 300 ms stability pass so the scrollTop reflects the
      // final settled landing position.
      _scrollAnchorGi  = gi;
      _scrollAnchorTop = chatArea.scrollTop;
      _scrollAnchorAt  = Date.now();
      // Release _programmaticScroll — normal scroll handling resumes
      _endProgScroll();
      initialScrollGuard = false;
    }, 300);
  });

  // Show return-to-reply button after quote navigation
  if (!_isReturn && _returnToMsgId != null) {
    _showReturnBtn(_returnToMsgId);
  }
}

function scrollToKey(keyId) {
  var gi = keyToGlobal[keyId];
  if (gi === undefined) {
    if (bridge) bridge.onScrollToKeyUnloaded(keyId);
    return;
  }
  var msg = getMsg(gi);
  if (msg) scrollToMessage(msg.id);
}

// ---- Return-to-reply floating button ----
function _showReturnBtn(returnMsgId) {
  _hideReturnBtn();
  var btn = document.createElement('div');
  btn.className = 'return-btn';
  btn.textContent = '\u21A9 Return to reply';
  btn.onclick = function () {
    _hideReturnBtn();
    if (returnMsgId != null) {
      scrollToMessage(returnMsgId, true);
    }
    _returnToMsgId = null;
  };
  chatArea.parentNode.appendChild(btn);
  // Force reflow then animate in
  btn.offsetHeight;
  btn.classList.add('visible');
  // Auto-hide after 8 seconds
  _returnBtnTimer = setTimeout(function () { _hideReturnBtn(); _returnToMsgId = null; }, 8000);
}

function _hideReturnBtn() {
  clearTimeout(_returnBtnTimer);
  var existing = chatArea.parentNode.querySelector('.return-btn');
  if (existing) existing.remove();
}

function highlightSearch(text) {
  container.querySelectorAll('.search-hl').forEach(function (el) { var p = el.parentNode; p.replaceChild(document.createTextNode(el.textContent), el); p.normalize(); });
  if (!text || text.length < 2) return;
  var walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT, null); var nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  var lower = text.toLowerCase();
  for (var ni = 0; ni < nodes.length; ni++) {
    var node = nodes[ni]; var c = node.textContent; var idx = c.toLowerCase().indexOf(lower); if (idx === -1) continue;
    var span = document.createElement('span'); span.className = 'search-hl'; span.textContent = c.substring(idx, idx + text.length);
    var p = node.parentNode;
    if (c.substring(0, idx)) p.insertBefore(document.createTextNode(c.substring(0, idx)), node);
    p.insertBefore(span, node);
    if (c.substring(idx + text.length)) p.insertBefore(document.createTextNode(c.substring(idx + text.length)), node);
    p.removeChild(node);
  }
}

function updateAudioProgress(msgId, progress) {
  activeAudioId = msgId; activeAudioProgress = progress;
  var wf = container.querySelector('[data-waveform-id="' + msgId + '"]'); if (!wf) return;
  var bars = wf.querySelectorAll('.bar'); var fc = Math.floor(progress * bars.length);
  bars.forEach(function (b, i) { b.classList.toggle('filled', i < fc); });
  var card = container.querySelector('[data-audio-id="' + msgId + '"]');
  if (card) { var btn = card.querySelector('.play-btn'); if (btn) btn.textContent = progress > 0 && progress < 1 ? '\u23F8' : '\u25B6'; }
}

function updateAudioStopped(msgId) {
  activeAudioId = 0; activeAudioProgress = 0;
  var wf = container.querySelector('[data-waveform-id="' + msgId + '"]'); if (!wf) return;
  wf.querySelectorAll('.bar').forEach(function (b) { b.classList.remove('filled'); });
  var card = container.querySelector('[data-audio-id="' + msgId + '"]');
  if (card) { var btn = card.querySelector('.play-btn'); if (btn) btn.textContent = '\u25B6'; }
}

// Font-size button bridge.  The chat stylesheet uses ABSOLUTE pixel
// sizes (e.g. font-size: 13px on .bubble .text), so just changing
// document.body.style.fontSize does not resize anything inside the
// bubbles.  Inject a per-class !important override that scales the
// key chat text classes proportionally to the chosen size.
function setFontSize(size) {
  size = Math.max(8, Math.min(22, size | 0));
  var ratio = size / 14.0;
  document.body.style.fontSize = size + 'px';
  var styleEl = document.getElementById('__chat-font-scale');
  if (!styleEl) {
    styleEl = document.createElement('style');
    styleEl.id = '__chat-font-scale';
    document.head.appendChild(styleEl);
  }
  // Scale ratios derived from chat_styles.css base values (rounded
  // to one decimal so the rendered px snaps to the closest device px).
  var fz = function (base) { return (base * ratio).toFixed(1) + 'px'; };
  styleEl.textContent = ''
    + 'body{font-size:' + size + 'px !important;}'
    + '.bubble .text,.bubble .caption,.quoted-text,.ghost-text,'
    +   '.system-event,.ghost-recovered-text,.poll-question,'
    +   '.vcard-name,.doc-info-name'
    +     '{font-size:' + fz(13) + ' !important;}'
    + '.bubble .sender-name,.poll-option-text'
    +     '{font-size:' + fz(11) + ' !important;}'
    + '.bubble .timestamp,.bubble .meta-time,.ts'
    +     '{font-size:' + fz(10.5) + ' !important;}'
    + '.bubble .reactions,.fp-key,.fp-val'
    +     '{font-size:' + fz(11) + ' !important;}'
    + '.day-divider,.unread-divider'
    +     '{font-size:' + fz(11.5) + ' !important;}';
}
function setTheme(dark) { document.body.classList.toggle('dark', dark); }
function showLoading(visible) { var el = document.getElementById('loadingSpinner'); if (el) el.classList.toggle('visible', visible); }
function updateTaggedMessages(taggedIdsJson) { var ids = new Set(JSON.parse(taggedIdsJson)); container.querySelectorAll('[data-msg-id]').forEach(function (el) { el.classList.toggle('tagged', ids.has(parseInt(el.dataset.msgId))); }); }

// Called from Python (ChatVideoThumbWorker.thumb_ready) once a
// video's first frame has been extracted from the on-disk file
// and cached as a JPEG.  Swaps the bubble's <img src> to the new
// file:// URL.  Safe to call before the bubble exists in the DOM
// (it's a no-op then; the next render will pick up msg.thumb on
// its own once the cache is warm).
//
// Two callsites for the data-vid-msg-id attribute:
//   * standalone video bubble — class="media-thumb" on the <img>
//   * album cell child — plain <img> with no class
// We use a generic ``img`` selector (not ``img.media-thumb``) so
// album cells get updated too.  The album cell only ever holds
// one <img> for the poster, so no risk of matching the wrong
// element.
function updateVideoThumb(msgId, fileUrl) {
  if (!msgId || !fileUrl) return;
  var containers = document.querySelectorAll('[data-vid-msg-id="' + msgId + '"]');
  for (var i = 0; i < containers.length; i++) {
    var img = containers[i].querySelector('img');
    if (img) {
      img.src = fileUrl;
      img.classList.remove('video-thumb-pending');
    }
  }
}

function setSearchTarget(msgId) {
  var prev = container.querySelector('.search-target');
  if (prev) prev.classList.remove('search-target');
  if (msgId) {
    var el = container.querySelector('[data-msg-id="' + msgId + '"]');
    if (el) el.classList.add('search-target');
  }
}

// Update a message after download completes (replace download badge with actual media)
function updateMessageMedia(msgId, fieldsJson) {
  var gi = idToGlobal[msgId]; if (gi === undefined) return;
  var msg = getMsg(gi); if (!msg) return;
  var fields = typeof fieldsJson === 'string' ? JSON.parse(fieldsJson) : fieldsJson;
  // Merge new fields into the message object
  for (var key in fields) { if (fields.hasOwnProperty(key)) msg[key] = fields[key]; }
  // If this is an album child, also update the parent album's children array
  if (msg.album_parent_id) {
    var parentGi = idToGlobal[msg.album_parent_id];
    if (parentGi !== undefined) {
      var parent = getMsg(parentGi);
      if (parent && parent.album_children) {
        for (var ci = 0; ci < parent.album_children.length; ci++) {
          if (parent.album_children[ci].id === msgId) {
            for (var key2 in fields) {
              if (fields.hasOwnProperty(key2)) parent.album_children[ci][key2] = fields[key2];
            }
            break;
          }
        }
      }
    }
  }
  // Force re-render of current viewport to pick up changes
  renderedFirst = -1;
  heightDirty = true;  // Force height recalculation
  renderVisible();
  // Also re-render after a short delay to ensure DOM catches up
  setTimeout(function () { renderedFirst = -1; renderVisible(); }, 200);
  console.log('[JS] updateMessageMedia: msg ' + msgId + ' updated with ' + Object.keys(fields).join(','));
}

// ---- Forensic Provenance ----
function showForensicInfo(msgId) {
  if (provenanceCache[msgId]) { _renderForensicPanel(msgId, provenanceCache[msgId]); return; }
  if (bridge) bridge.onForensicInfo(msgId); _renderForensicPanel(msgId, null);
}
function receiveProvenance(msgId, data) {
  var prov = null; try { prov = typeof data === 'string' ? JSON.parse(data) : data; } catch (e) { }
  provenanceCache[msgId] = prov;
  // Always render — panel may have been opened by context menu or "i" button
  _renderForensicPanel(msgId, prov);
}
function _renderForensicPanel(msgId, prov) {
  var gi = idToGlobal[msgId]; var msg = (gi !== undefined) ? getMsg(gi) : null;
  var panel = document.getElementById('forensicPanel');
  if (!panel) { panel = document.createElement('div'); panel.id = 'forensicPanel'; document.body.appendChild(panel); }
  panel.dataset.msgId = msgId;
  var html = '<div class="fp-header"><span>Forensic Info \u2014 Message #' + msgId + '</span><button onclick="_copyForensicPanel()" title="Copy to clipboard" style="margin-right:8px;font-size:11px">\u{1F4CB}</button><button onclick="hideForensicInfo()">\u2715</button></div><div class="fp-body">';
  if (msg) {
    html += '<div class="fp-section"><div class="fp-title">Message Identity</div>';
    if (msg.src_id && msg.src_id > 0) html += '<div class="fp-row"><span class="fp-key">msgstore.db _id</span><span class="fp-val" style="font-weight:700;color:#1565c0">' + msg.src_id + '</span></div>';
    else html += '<div class="fp-row"><span class="fp-key">Source</span><span class="fp-val" style="color:#e65100;font-weight:600">\u26A0 Synthesized by WAInsight <span style="font-weight:400">(reconstructed from call_record)</span></span></div>';
    html += '<div class="fp-row"><span class="fp-key">Key ID</span><span class="fp-val" style="font-family:monospace;font-size:10px">' + esc(msg.source_key || 'N/A') + '</span></div>';
    html += '<div class="fp-row"><span class="fp-key">Type</span><span class="fp-val">' + msg.type + ' (' + esc(msg.type_label) + ')</span></div>';
    if (msg.is_revoked) {
      html += '<div class="fp-row"><span class="fp-key">Revoked</span><span class="fp-val" style="color:#e53935;font-weight:600">\u26D4 Revoked (original type: ' + esc(msg.type_label || 'unknown') + ')</span></div>';
    }
    html += '<div class="fp-row"><span class="fp-key">From Me</span><span class="fp-val">' + (msg.from_me ? 'Yes (Outgoing)' : 'No (Incoming)') + '</span></div>';
    var _fpSender = (msg.from_me && ownerLabel) ? ownerLabel : (msg.sender || 'N/A');
    if (msg.sender_phone && _fpSender.indexOf(msg.sender_phone) < 0) _fpSender += ' (+' + msg.sender_phone + ')';
    html += '<div class="fp-row"><span class="fp-key">Sender</span><span class="fp-val">' + esc(_fpSender);
    if (msg.is_verified) html += ' <span style="background:#1da1f2;color:white;padding:1px 5px;border-radius:8px;font-size:9px;font-weight:700">\u2713 Meta Verified</span>';
    else if (msg.is_biz) html += ' <span style="color:#128c7e;font-size:10px">[WhatsApp Business]</span>';
    html += '</span></div>';
    // JIDs with source table info
    if (msg.phone_jid_full) {
      var _pjid = msg.phone_jid_full;
      if (_pjid && _pjid.indexOf('@') < 0) _pjid += '@s.whatsapp.net';
      html += '<div class="fp-row"><span class="fp-key">Phone JID</span><span class="fp-val" style="font-family:monospace;font-size:10px">' + esc(_pjid) + ' <span style="color:#888;font-size:9px">(jid table \u2192 wa_contacts)</span></span></div>';
    }
    if (msg.lid_jid_full) {
      var _ljid = msg.lid_jid_full;
      if (_ljid && _ljid.indexOf('@') < 0) _ljid += '@lid';
      html += '<div class="fp-row"><span class="fp-key">LID JID</span><span class="fp-val" style="font-family:monospace;font-size:10px">' + esc(_ljid) + ' <span style="color:#888;font-size:9px">(jid_map \u2192 lid)</span></span></div>';
    }
    // Raw msgstore.db row IDs for forensic cross-referencing
    if (msg.sender_jid_row_id != null) {
      html += '<div class="fp-row"><span class="fp-key">Sender JID Row ID</span><span class="fp-val" style="font-family:monospace">' + msg.sender_jid_row_id + ' <span style="color:#888;font-size:9px">(msgstore.db jid._id)</span></span></div>';
    }
    if (msg.source_chat_row_id != null) {
      html += '<div class="fp-row"><span class="fp-key">Chat Row ID</span><span class="fp-val" style="font-family:monospace">' + msg.source_chat_row_id + ' <span style="color:#888;font-size:9px">(msgstore.db chat._id)</span></span></div>';
    }
    if (msg.ts) {
      html += '<div class="fp-row"><span class="fp-key">Timestamp</span><span class="fp-val">' + msg.ts + ' (Unix ms) \u2014 ' + fmtFullTs(msg.ts) + '</span></div>';
    }
    // Origination flags — decoded per.
    // The PDF enumerates the "most frequent" values. We match by EXACT value
    // first (matches PDF verbatim). Values not listed in the PDF are shown
    // raw so the user can cross-reference the book rather than trust any
    // extrapolated guess.
    var _of = msg.oflags || 0;
    if (_of > 0) {
      // Verbatim PDF value table (page 73).
      var _PDF_OFLAGS = {
        0:          'non-temporal (may be a system message)',
        1:          '\u21AA Forwarded, non-temporal',
        64:         '\u{1F5BC} Image sent to multiple contacts, non-temporal',
        65:         '\u21AA Forwarded image to multiple contacts, non-temporal',
        256:        '\u23F3 Temporal (disappearing)',
        257:        '\u23F3 Temporal forwarded',
        320:        '\u23F3 Image to multiple contacts, temporal',
        321:        '\u23F3 Forwarded image to multiple contacts, temporal',
        512:        '\u{1F4E2} System-originated / broadcast-pipeline',
        2048:       '\u{1F4C4} Document/URL link/Status video, non-temporal',
        2049:       '\u21AA Forwarded URL, non-temporal',
        2304:       '\u23F3 Document/URL/channel invite, temporal',
        2305:       '\u23F3 Forwarded URL, temporal',
        32768:      '\u{1F3A4} Voice note, non-temporal',
        32769:      '\u21AA Forwarded voice note, non-temporal',
        33024:      '\u23F3 Voice note, temporal',
        33025:      '\u23F3 Forwarded voice note, temporal',
        131072:     '\u270F Edited, non-temporal (or Meta AI reply)',
        131328:     '\u270F Edited, temporal',
        67108864:   '\u{1F5BC} Multimedia album, non-temporal',
        67109120:   '\u23F3 Multimedia album, temporal',
        537001984:  '\u{1F4C5} Event created+edited, non-temporal',
        537002240:  '\u23F3 Event created+edited, temporal',
      };
      var _label = _PDF_OFLAGS[_of];
      var _body;
      if (_label) {
        _body = _label;
      } else {
        _body = 'not listed in PDF (raw: ' + _of + ' = 0x' + _of.toString(16) + ')';
      }
      html += '<div class="fp-row"><span class="fp-key">Origin Flags</span><span class="fp-val">' + _body + ' <span style="color:#888;font-size:9px">(raw: ' + _of + ')</span></span></div>';
    }
    html += '</div>';
    // System event details
    if (msg.type === 7 || msg.event_label) {
      html += '<div class="fp-section"><div class="fp-title">System Event</div>';
      if (msg.event_label) html += '<div class="fp-row"><span class="fp-key">Event Type</span><span class="fp-val" style="font-weight:600">' + esc(msg.event_label) + '</span></div>';
      if (msg.event_data) html += '<div class="fp-row"><span class="fp-key">Event Data</span><span class="fp-val" style="font-family:monospace;font-size:10px;word-break:break-all">' + esc(msg.event_data) + '</span></div>';
      if (msg.system_text) html += '<div class="fp-row"><span class="fp-key">System Text</span><span class="fp-val">' + esc(msg.system_text) + '</span></div>';
      // Number change details
      if (msg.event_label === 'number_changed' && (msg.nc_old_phone || msg.nc_new_phone)) {
        html += '<div class="fp-row"><span class="fp-key">Old Number</span><span class="fp-val" style="font-family:monospace;font-weight:600;color:#e53935">' + esc(msg.nc_old_phone || 'N/A') + (msg.nc_old_name ? ' (' + esc(msg.nc_old_name) + ')' : '') + '</span></div>';
        html += '<div class="fp-row"><span class="fp-key">New Number</span><span class="fp-val" style="font-family:monospace;font-weight:600;color:#2e7d32">' + esc(msg.nc_new_phone || 'N/A') + (msg.nc_new_name ? ' (' + esc(msg.nc_new_name) + ')' : '') + '</span></div>';
      }
      // Actor (who performed the action) — with JID
      if (msg.se_actor) {
        html += '<div class="fp-row"><span class="fp-key">Actor</span><span class="fp-val">' + esc(msg.se_actor) + '</span></div>';
        if (msg.se_actor_jid) html += '<div class="fp-row"><span class="fp-key">Actor JID</span><span class="fp-val" style="font-family:monospace;font-size:10px">' + esc(msg.se_actor_jid) + '</span></div>';
        if (msg.se_actor_lid) html += '<div class="fp-row"><span class="fp-key">Actor LID</span><span class="fp-val" style="font-family:monospace;font-size:10px">' + esc(msg.se_actor_lid) + '</span></div>';
      }
      // Target (who was affected) — with JID
      if (msg.se_target) {
        html += '<div class="fp-row"><span class="fp-key">Target</span><span class="fp-val">' + esc(msg.se_target) + '</span></div>';
        if (msg.se_target_jid) html += '<div class="fp-row"><span class="fp-key">Target JID</span><span class="fp-val" style="font-family:monospace;font-size:10px">' + esc(msg.se_target_jid) + '</span></div>';
        if (msg.se_target_lid) html += '<div class="fp-row"><span class="fp-key">Target LID</span><span class="fp-val" style="font-family:monospace;font-size:10px">' + esc(msg.se_target_lid) + '</span></div>';
      }
      html += '</div>';
    }
    // Media / File section
    if (msg.file_path || msg.type_label) {
      // Distinguish forensic provenance up-front:
      //   * recovery_method='hash_linked' = THIS MESSAGE was never
      //     downloaded by the device owner.  Another message in the
      //     case has the same SHA-256, so the file we display is
      //     content-equivalent but NOT what arrived on this device for
      //     this message.  Critical to surface clearly: the analyst
      //     could otherwise mistake "\u2714 Found" for proof of receipt.
      //   * recovery_method='downloaded' = our tool fetched the file
      //     from the WhatsApp CDN AFTER extraction; user did not
      //     receive it on the phone either.
      //   * No recovery_method + file_exists = original (transferred
      //     by WhatsApp on the device, i.e. user received it).
      var _isHashLinked      = msg.recovery_method === 'hash_linked';
      var _isHashDeleted     = msg.recovery_method === 'hash_linked_after_delete';
      var _isOrphanRecovered = msg.recovery_method === 'orphan_recovered';
      var _isDownloaded      = msg.recovery_method === 'downloaded';
      var _isOriginal        = msg.file_exists && !msg.recovery_method;

      html += '<div class="fp-section"><div class="fp-title">Media / File</div>';
      if (msg.type_label) html += '<div class="fp-row"><span class="fp-key">Type</span><span class="fp-val">' + esc(msg.type_label) + '</span></div>';
      if (msg.source_media_row_id != null) html += '<div class="fp-row"><span class="fp-key">Media Row ID</span><span class="fp-val" style="font-family:monospace">' + msg.source_media_row_id + ' <span style="color:#888;font-size:9px">(msgstore.db message_media._id)</span></span></div>';
      if (msg.mime) html += '<div class="fp-row"><span class="fp-key">MIME</span><span class="fp-val">' + esc(msg.mime) + '</span></div>';

      // The Path row label changes based on provenance so the analyst
      // never confuses an original path with a recovered one.
      if (msg.file_path) {
        var _pathLbl, _pathExtra = '';
        if (_isHashDeleted) {
          _pathLbl = 'Path (hash-linked, original was deleted)';
          _pathExtra = '<div style="color:#e65100;font-size:10px;margin-top:2px">\u26A0 Originally received in this chat, but the local file was deleted. The path above is from another message with the same SHA-256.</div>';
        } else if (_isOrphanRecovered) {
          _pathLbl = 'Path (rescued from orphaned file on disk)';
          _pathExtra = '<div style="color:#2e7d32;font-size:10px;margin-top:2px">\uD83D\uDCBE The chat record had no file (cleared chat / reinstall / autoclean), but a file in the WhatsApp media folder has the same SHA-256. The path above is that orphaned file \u2014 it IS the original bytes.</div>';
        } else if (_isHashLinked) {
          _pathLbl = 'Path (hash-linked)';
          _pathExtra = '<div style="color:#7b1fa2;font-size:10px;margin-top:2px">\u26A0 This message had NO downloaded file on the device. The path above is from a DIFFERENT message that has the same SHA-256.</div>';
        } else if (_isDownloaded) {
          _pathLbl = 'Path (recovered by tool)';
          _pathExtra = '<div style="color:#00897b;font-size:10px;margin-top:2px">Tool downloaded this file from WhatsApp CDN after extraction.</div>';
        } else {
          _pathLbl = 'Path (on device)';
        }
        html += '<div class="fp-row"><span class="fp-key">' + _pathLbl + '</span><span class="fp-val" style="word-break:break-all;font-size:10px">' + esc(msg.file_path) + _pathExtra + '</span></div>';
      }
      if (msg.file_size) html += '<div class="fp-row"><span class="fp-key">Size</span><span class="fp-val">' + fmtSize(msg.file_size) + '</span></div>';

      // "Full File on Disk" - readable, forensic-meaningful labels.
      // The earlier "Original (transferred to device)" was jargon-y;
      // these read as plain English even for non-forensic reviewers.
      if (msg.file_exists) {
        if (_isHashDeleted) {
          html += '<div class="fp-row"><span class="fp-key">Full File on Disk</span>'
            + '<span class="fp-val" style="color:#e65100">\u26a0 Originally received here, file later deleted <span style="font-weight:400;color:#888">(same SHA-256 still exists in another message; that file is shown)</span></span></div>';
        } else if (_isOrphanRecovered) {
          html += '<div class="fp-row"><span class="fp-key">Full File on Disk</span>'
            + '<span class="fp-val" style="color:#2e7d32">\ud83d\udcbe Rescued from orphaned file on disk <span style="font-weight:400;color:#888">(chat record was lost \u2014 cleared chat or reinstall \u2014 but the file with this SHA-256 was still in the WhatsApp media folder)</span></span></div>';
        } else if (_isHashLinked) {
          html += '<div class="fp-row"><span class="fp-key">Full File on Disk</span>'
            + '<span class="fp-val" style="color:#7b1fa2">\u2714 Hash-linked from another message <span style="font-weight:400;color:#888">(this message did NOT receive a file; the displayed file came from a different message with the same SHA-256)</span></span></div>';
        } else if (_isDownloaded) {
          html += '<div class="fp-row"><span class="fp-key">Full File on Disk</span>'
            + '<span class="fp-val" style="color:#00897b">\u2714 Recovered by tool from WhatsApp CDN <span style="font-weight:400;color:#888">(downloaded after the phone was extracted)</span></span></div>';
        } else {
          html += '<div class="fp-row"><span class="fp-key">Full File on Disk</span>'
            + '<span class="fp-val" style="color:#2e7d32">\u2714 Present in phone extraction <span style="font-weight:400;color:#888">(received in this chat, found in WhatsApp folder)</span></span></div>';
        }
      } else {
        html += '<div class="fp-row"><span class="fp-key">Full File on Disk</span><span class="fp-val" style="color:#e53935">\u2718 Not Found</span></div>';
        if (msg.thumb) html += '<div class="fp-row"><span class="fp-key">DB Thumbnail</span><span class="fp-val" style="color:#1565c0">Embedded image from database (shown in chat)</span></div>';
      }

      if (msg.cdn_url) {
        // Parse CDN URL: extract expiry from oe= param (UTC timestamp)
        var _cdnUrl = msg.cdn_url;
        var _oeMatch = _cdnUrl.match(/oe=([0-9A-Fa-f]+)/);
        var _expTs = _oeMatch ? parseInt(_oeMatch[1], 16) : 0;
        var _expDate = _expTs ? new Date(_expTs * 1000) : null;
        var _isExpired = _expDate ? (_expDate.getTime() < Date.now()) : false;
        var _host = _cdnUrl.match(/https?:\/\/([^\/]+)/);
        html += '<div class="fp-row"><span class="fp-key">CDN Host</span><span class="fp-val">' + (_host ? esc(_host[1]) : 'N/A') + '</span></div>';
        // Show the FULL URL (was truncated to 120 chars + "..." which
        // hid the oe= expiry hex and the tail _nc_sid query parameter
        // - both forensically relevant when proving when the URL was
        // valid).  word-break + max-height keeps the panel from blowing
        // up vertically; the user can still scroll to see the tail.
        html += '<div class="fp-row"><span class="fp-key">CDN URL</span>'
          + '<span class="fp-val" style="word-break:break-all;font-size:9px;font-family:monospace;display:block;max-height:140px;overflow:auto;background:rgba(127,127,127,0.07);padding:4px;border-radius:3px">' + esc(_cdnUrl) + '</span></div>';
        if (_expDate) {
          var _expUtc = _expDate.toISOString().replace('T', ' ').substring(0, 19) + ' UTC';
          var _daysLeft = ((_expDate.getTime() - Date.now()) / 86400000).toFixed(1);
          var _detail = _isExpired ? ' (EXPIRED ' + Math.abs(_daysLeft) + ' days ago)' : ' (VALID \u2014 ' + _daysLeft + ' days left)';
          html += '<div class="fp-row"><span class="fp-key">URL Expires</span><span class="fp-val" style="color:' + (_isExpired ? '#e53935' : '#2e7d32') + ';font-weight:600">' + _expUtc + _detail + '</span></div>';
        }
      } else if (msg.has_url) {
        html += '<div class="fp-row"><span class="fp-key">Server URL</span><span class="fp-val" style="color:#2e7d32">Available</span></div>';
      }
      if (msg.has_key) html += '<div class="fp-row"><span class="fp-key">Decrypt Key</span><span class="fp-val" style="color:#2e7d32">Available (32 bytes)</span></div>';
      else if (msg.has_url) html += '<div class="fp-row"><span class="fp-key">Decrypt Key</span><span class="fp-val" style="color:#e53935">Missing (wiped by WhatsApp)</span></div>';
      // Media provenance \u2014 plain-English summary
      // Two distinct "missing" states based on whether msgstore says
      // the user actually downloaded this file at receipt time:
      //   * was_transferred = 1 -> "was downloaded, file deleted later"
      //   * was_transferred = 0 / NULL -> "never downloaded by user"
      // This forensic distinction matters legally \u2014 proof of receipt
      // (downloaded then deleted) is NOT the same as never received.
      var _wasTransferred = (msg.was_transferred === 1);
      var _wasNotTransferred = (msg.was_transferred === 0);
      var _prov = '';
      if (_isOriginal) _prov = '<span style="color:#2e7d32">\u{1F7E2} Received in this chat \u2014 file was on the phone at extraction</span>';
      else if (_isDownloaded) _prov = '<span style="color:#00897b">\u2B07 Recovered by tool \u2014 downloaded from WhatsApp CDN after extraction</span>';
      else if (_isOrphanRecovered) _prov = '<span style="color:#2e7d32">\uD83D\uDCBE Rescued from orphaned file on disk \u2014 chat record was lost (cleared/reinstalled) but the original file with this SHA-256 was still in the WhatsApp media folder</span>';
      else if (_isHashDeleted) _prov = '<span style="color:#e65100">\u26A0 Originally received in this chat, but the local file was later deleted \u2014 same SHA-256 found in another message; that file is shown</span>';
      else if (_isHashLinked) {
        var _srcDl2 = (msg.file_path || '').indexOf('recovered_media') >= 0 || (msg.file_path || '').indexOf('Recovered_') >= 0;
        if (_srcDl2) _prov = '<span style="color:#7b1fa2">\u{1F517} Hash-linked from a tool-recovered file in another chat \u2014 NOT received in this message</span>';
        else _prov = '<span style="color:#7b1fa2">\u{1F517} Hash-linked \u2014 same SHA-256 was received in another chat, but NOT in this message</span>';
      }
      else if (!msg.file_exists && msg.has_url && msg.has_key) {
        // Distinguish "was downloaded then deleted, redownloadable" from
        // "never downloaded, available on CDN" \u2014 the former is proof of
        // receipt even though the file is gone right now.
        if (_wasTransferred) {
          _prov = '<span style="color:#e65100">\u26A0 Was downloaded and later deleted \u2014 CDN URL still valid, file can be re-fetched. <span style="font-weight:400;color:#888">(transferred=1: msgstore confirms the file WAS on the phone)</span></span>';
        } else {
          _prov = '<span style="color:#1565c0">\u{1F535} Not downloaded \u2014 still available on CDN, can be recovered <span style="font-weight:400;color:#888">(transferred=0: user never received the file)</span></span>';
        }
      }
      else if (msg.media_status === 'expired') {
        if (_wasTransferred) {
          _prov = '<span style="color:#c62828">\u26A0 Was downloaded and later deleted \u2014 CDN URL expired, no longer fetchable. The bytes existed on this phone at one point.</span>';
        } else {
          _prov = '<span style="color:#e65100">\u{1F7E0} CDN URL expired \u2014 link no longer valid for download (and user never had the file)</span>';
        }
      }
      else if (msg.media_status === 'no_key') _prov = '<span style="color:#f57f17">\u{1F512} No decryption key \u2014 key was wiped by WhatsApp, file cannot be decrypted</span>';
      else if (msg.media_status === 'thumb_only') _prov = '<span style="color:#757575">\u{1F5BC} Thumbnail only \u2014 no full file and no CDN URL</span>';
      else if (!msg.file_exists) {
        if (_wasTransferred) {
          _prov = '<span style="color:#c62828">\u26A0 Missing (was downloaded, file deleted) \u2014 no current file, no CDN URL, no key. msgstore.transferred=1 confirms the file WAS on this phone at some point.</span>';
        } else if (_wasNotTransferred) {
          _prov = '<span style="color:#c62828">\u274C Not downloaded \u2014 user never received this file (transferred=0); no CDN URL or key available now either.</span>';
        } else {
          _prov = '<span style="color:#c62828">\u274C Missing \u2014 no file, no URL, no key</span>';
        }
      }
      if (_prov) html += '<div class="fp-row"><span class="fp-key">Media Status</span><span class="fp-val">' + _prov + '</span></div>';

      // SHA-256 in hex (canonical form for VirusTotal / hashlookup /
      // timeline tools).  msgstore stores the hash as base64; we
      // decode and render hex only - the base64 row was redundant.
      if (msg.file_hash) {
        try {
          var _hexHash = b64ToHexLower(msg.file_hash);
          if (_hexHash) {
            html += '<div class="fp-row"><span class="fp-key">SHA-256</span>'
              + '<span class="fp-val" style="font-family:monospace;font-size:9px;word-break:break-all">' + esc(_hexHash) + '</span></div>';
          }
        } catch (e) {}
      }
      if (msg.media_width && msg.media_height) html += '<div class="fp-row"><span class="fp-key">Resolution</span><span class="fp-val">' + msg.media_width + 'x' + msg.media_height + '</span></div>';
      if (msg.duration_ms) html += '<div class="fp-row"><span class="fp-key">Duration</span><span class="fp-val">' + fmtDur(msg.duration_ms) + '</span></div>';
      if (msg.media_name) html += '<div class="fp-row"><span class="fp-key">Filename</span><span class="fp-val">' + esc(msg.media_name) + '</span></div>';
      if (msg.page_count) html += '<div class="fp-row"><span class="fp-key">Pages</span><span class="fp-val">' + msg.page_count + '</span></div>';

      // HD-twin section: when this is the SD parent of a dual-quality
      // pair, show the HD twin's identity inline so the analyst can
      // cross-reference both rows.  WhatsApp's message_association
      // (association_type=7) ties them together; this surfaces the
      // forensic context without leaving the bubble's info panel.
      if (msg.hd_msg_id) {
        var _hdHashHex = '';
        if (msg.hd_hash) {
          try { _hdHashHex = b64ToHexLower(msg.hd_hash); } catch (e) {}
        }
        var _hdResStr = (msg.hd_w && msg.hd_h) ? (msg.hd_w + 'x' + msg.hd_h) : '';
        var _hdSizeStr = msg.hd_size ? fmtSize(msg.hd_size) : '';
        var _hdNote =
          'WhatsApp dual-quality send: this message is the <b>SD parent</b>; '
          + 'the HD twin is a separate row in msgstore '
          + '(<code>message_association.association_type=7</code>) '
          + 'rendered inline here for the higher-resolution view.  '
          + 'Reactions, replies, edits all attach to this SD message.';
        html += '<div class="fp-row"><span class="fp-key">HD Twin</span>'
          + '<span class="fp-val" style="color:#7b1fa2">'
          + 'msg #<code>' + msg.hd_msg_id + '</code>'
          + (_hdResStr ? ' &middot; ' + esc(_hdResStr) : '')
          + (_hdSizeStr ? ' &middot; ' + esc(_hdSizeStr) : '')
          + (msg.hd_exists ? ' &middot; <span style="color:#2e7d32">on disk</span>'
                            : ' &middot; <span style="color:#e65100">missing</span>')
          + '<div style="color:#666;font-size:9px;margin-top:3px">' + _hdNote + '</div>'
          + (_hdHashHex
              ? '<div style="font-family:monospace;font-size:9px;color:#7b1fa2;'
                + 'word-break:break-all;margin-top:2px">SHA-256(HD): '
                + esc(_hdHashHex) + '</div>'
              : '')
          + '</span></div>';
      }
      html += '</div>';
    }
    // Delivery section
    if (msg.from_me) {
      html += '<div class="fp-section"><div class="fp-title">Delivery</div>';
      html += '<div class="fp-row"><span class="fp-key">Status</span><span class="fp-val">' + msg.status + '</span></div>';
      if (msg.delivered_ts) html += '<div class="fp-row"><span class="fp-key">Delivered</span><span class="fp-val">' + fmtFullTs(msg.delivered_ts) + '</span></div>';
      if (msg.read_ts) html += '<div class="fp-row"><span class="fp-key">Read</span><span class="fp-val">' + fmtFullTs(msg.read_ts) + '</span></div>';
      html += '</div>';
    }
    // Device & Key ID Classification section
    var _hasDevInfo = msg.device_label || msg.platform || msg.source_key;
    if (_hasDevInfo) {
      html += '<div class="fp-section"><div class="fp-title">Device Classification</div>';
      if (msg.device_label) html += '<div class="fp-row"><span class="fp-key">Device Label</span><span class="fp-val">' + esc(msg.device_label) + '</span></div>';
      if (msg.platform) {
        var _platDisplay = { 'android': 'Android Phone', 'iphone': 'iPhone', 'android_linked': 'Linked Android', 'iphone_linked': 'Linked iPhone/iPad', 'companion': 'Web/Desktop (Companion)', 'newsletter': 'Newsletter/Channel', 'channel_bot': 'Channel Bot' }[msg.platform] || msg.platform;
        html += '<div class="fp-row"><span class="fp-key">Platform (key_id)</span><span class="fp-val" style="font-weight:600;color:#1565c0">' + esc(_platDisplay) + '</span></div>';
      }
      if (msg.device_num !== undefined && msg.device_num >= 0) {
        html += '<div class="fp-row"><span class="fp-key">Device Number</span><span class="fp-val">' + msg.device_num + (msg.device_num === 0 ? ' (Primary Phone)' : ' (Companion #' + msg.device_num + ')') + '</span></div>';
      }
      // Key ID analysis
      if (msg.source_key) {
        var _sk = msg.source_key;
        var _skLen = _sk.length;
        var _skPfx = _sk.substring(0, 4).toUpperCase();
        html += '<div class="fp-row"><span class="fp-key">Key ID Length</span><span class="fp-val">' + _skLen + ' chars (' + (_skLen / 2) + ' bytes)</span></div>';
        html += '<div class="fp-row"><span class="fp-key">Key ID Prefix</span><span class="fp-val" style="font-family:monospace">' + esc(_skPfx) + '</span></div>';
      }
      html += '</div>';
    }
    // Raw text (before mention resolution)
    if (msg.raw_text && msg.text && msg.raw_text !== msg.text) {
      html += '<div class="fp-section"><div class="fp-title">Original Text (Raw)</div>';
      html += '<div class="fp-row"><span class="fp-val" style="font-family:monospace;font-size:10px;white-space:pre-wrap;word-break:break-all">' + esc(msg.raw_text.substring(0, 500)) + '</span></div>';
      html += '</div>';
    }
    // Mentions detail
    if (msg.mentions) {
      html += '<div class="fp-section"><div class="fp-title">Mentions</div>';
      var _mEntries = msg.mentions.split(';;').filter(Boolean);
      _mEntries.forEach(function (me) {
        var mp = me.split('::');
        var mName = mp[0] || 'Unknown';
        var mPhone = mp[2] || '';
        var mLid = mp[3] || '';
        var mDisplay = mp[4] || '';
        html += '<div class="fp-row" style="flex-direction:column;align-items:flex-start">';
        html += '<span class="fp-val" style="font-weight:600">' + esc(mName) + '</span>';
        if (mPhone) html += '<span class="fp-val" style="font-family:monospace;font-size:9px">' + esc(mPhone) + '@s.whatsapp.net</span>';
        if (mLid) html += '<span class="fp-val" style="font-family:monospace;font-size:9px">' + esc(mLid) + '@lid</span>';
        if (mDisplay && mDisplay !== mName) html += '<span class="fp-val" style="font-size:9px;color:#888">Display: ' + esc(mDisplay) + '</span>';
        html += '</div>';
      });
      html += '</div>';
    }
  }
  if (prov) { Object.keys(prov).forEach(function (secName) { html += '<div class="fp-section"><div class="fp-title">' + esc(secName.replace(/_/g, ' ').toUpperCase()) + '</div>' + renderProvSection(prov[secName], 0) + '</div>'; }); }
  else if (prov === null && !provenanceCache[msgId]) { html += '<div class="fp-section"><div class="fp-title">Provenance</div><div class="fp-row"><span class="fp-val" style="color:#999">Loading...</span></div></div>'; }
  else { html += '<div class="fp-section"><div class="fp-title">Provenance</div><div class="fp-row"><span class="fp-val" style="color:#999">No data available.</span></div></div>'; }
  html += '</div>'; panel.innerHTML = html; panel.classList.add('visible');
}
function renderProvSection(data, depth) {
  if (depth > 4) return '<span class="fp-val">...</span>';
  if (data === null || data === undefined) return '';
  if (typeof data !== 'object') return '<span class="fp-val">' + esc(String(data)) + '</span>';
  if (Array.isArray(data)) { var h = ''; for (var i = 0; i < data.length && i < 20; i++) h += '<div class="fp-row" style="margin-left:' + (depth * 12) + 'px">' + renderProvSection(data[i], depth + 1) + '</div>'; if (data.length > 20) h += '<div class="fp-row"><span class="fp-val">... (' + data.length + ' items)</span></div>'; return h; }
  var h = ''; Object.keys(data).forEach(function (key) { var val = data[key]; if (val === null || val === undefined || val === '') return; if (typeof val === 'object') { h += '<div class="fp-row" style="margin-left:' + (depth * 12) + 'px"><span class="fp-key">' + esc(key) + '</span></div>' + renderProvSection(val, depth + 1); } else { h += '<div class="fp-row" style="margin-left:' + (depth * 12) + 'px"><span class="fp-key">' + esc(key) + '</span><span class="fp-val">' + esc(String(val)) + '</span></div>'; } });
  return h;
}
function hideForensicInfo() { var panel = document.getElementById('forensicPanel'); if (panel) panel.classList.remove('visible'); }
// Pulse a specific album cell (top-left "i/N" badge expands and the
// cell glows for ~3 s) so the user can identify which photo of a
// multi-photo album they came from after a Go-to-Chat from the media
// gallery.  Tries up to 4 s with 200 ms retries because the album
// parent might still be in the rendering queue when this fires.
function highlightAlbumChild(parentMsgId, childMsgId, pos1Based) {
  var deadline = Date.now() + 4000;
  function attempt() {
    var parentEl = document.querySelector('[data-msg-id="' + parentMsgId + '"]');
    if (parentEl) {
      // Try cell selectors in order of specificity
      var cell = parentEl.querySelector('[data-album-pos="' + pos1Based + '/' + (parentEl.querySelectorAll('.album-item').length || pos1Based) + '"]');
      if (!cell) {
        var items = parentEl.querySelectorAll('.album-item');
        if (items && items.length >= pos1Based) cell = items[pos1Based - 1];
      }
      if (cell) {
        cell.classList.remove('album-cell-pulse');
        // Force reflow so the animation restarts even on repeat clicks
        void cell.offsetWidth;
        cell.classList.add('album-cell-pulse');
        // Briefly outline the parent bubble too, so the user sees the
        // whole album scope, not just the lone cell.
        parentEl.classList.remove('album-host-pulse');
        void parentEl.offsetWidth;
        parentEl.classList.add('album-host-pulse');
        setTimeout(function () {
          cell.classList.remove('album-cell-pulse');
          parentEl.classList.remove('album-host-pulse');
        }, 3500);
        return;
      }
    }
    if (Date.now() < deadline) setTimeout(attempt, 200);
  }
  attempt();
}

function bAlbumDownload(msgId) {
  // Download all items in album — delegates to bridge per child
  var gi = idToGlobal[msgId]; var msg = (gi !== undefined) ? getMsg(gi) : null;
  if (!msg || !msg.album_children) return;
  for (var i = 0; i < msg.album_children.length; i++) {
    var c = msg.album_children[i];
    if (!c.file_exists && c.has_url && c.id) {
      bDl(c.id);
    }
  }
}

// Open one album item via the lightbox AND tell the bridge that this is
// part of album N at position i — bridge can then load the playlist of
// all album children and let user navigate forward/back through the
// whole album with arrow keys, even if the album has 100 items.
function bAlbumOpen(msgId, posIdx) {
  var gi = idToGlobal[msgId]; var msg = (gi !== undefined) ? getMsg(gi) : null;
  if (!msg || !msg.album_children || !msg.album_children[posIdx]) return;
  var c = msg.album_children[posIdx];
  // Build a normalized playlist of {path, id, isVideo, posLabel} so
  // bridge.onMediaClick can construct a lightbox carousel without an
  // extra DB roundtrip.
  var playlist = [];
  for (var i = 0; i < msg.album_children.length; i++) {
    var ch = msg.album_children[i];
    playlist.push({
      path: ch.file_path || '',
      id: ch.id || 0,
      isVideo: ch.type_label === 'video' || (ch.mime && ch.mime.startsWith('video/')),
      posLabel: (i + 1) + '/' + msg.album_children.length,
      file_exists: !!ch.file_exists,
      has_url: !!ch.has_url,
    });
  }
  if (bridge) {
    bridge.onMediaClick(JSON.stringify({
      path: c.file_path || '',
      id: c.id || 0,
      albumParentId: msgId,
      albumPos: posIdx,
      albumPlaylist: playlist,
    }));
  }
}

// Toggle expanded state for albums with > 9 items.  We mutate the msg
// dict in place and re-render only that one bubble for snappy UX.
function bAlbumExpand(msgId) {
  var gi = idToGlobal[msgId]; var msg = (gi !== undefined) ? getMsg(gi) : null;
  if (!msg) return;
  msg._album_expanded = true;
  rerenderMsg(msgId);
}
function bAlbumCollapse(msgId) {
  var gi = idToGlobal[msgId]; var msg = (gi !== undefined) ? getMsg(gi) : null;
  if (!msg) return;
  msg._album_expanded = false;
  rerenderMsg(msgId);
}

// Re-render a single message bubble in place (used by expand/collapse).
// Falls back to a full repaint if the bubble isn't in the DOM yet.
function rerenderMsg(msgId) {
  var el = document.querySelector('[data-msg-id="' + msgId + '"]');
  if (!el) {
    // Bubble not currently mounted - next viewport render picks up the new state
    if (typeof renderVisible === 'function') renderVisible();
    return;
  }
  var gi = idToGlobal[msgId]; var msg = (gi !== undefined) ? getMsg(gi) : null;
  if (!msg) return;
  // renderMsg signature: (msg, gi, prev?)
  var prev = gi > 0 ? getMsg(gi - 1) : null;
  var newHtml = renderMsg(msg, gi, prev);
  // renderMsg returns a full <div class="msg ..."> so we replace outerHTML.
  el.outerHTML = newHtml;
}
// Convert a base64-encoded SHA-256 (msgstore.message_media.file_hash
// format) to lower-case hex digest.  WhatsApp stores the hash as the
// 32-byte SHA-256 base64-encoded ("O7QnNap71CLM...="); analysts often
// need the hex form to paste into VirusTotal or hashlookup tools.
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

function _copyForensicPanel() {
  var panel = document.getElementById('forensicPanel');
  if (!panel) return;
  var body = panel.querySelector('.fp-body');
  if (!body) return;
  // Extract text from all fp-key/fp-val pairs
  var lines = [];
  body.querySelectorAll('.fp-section').forEach(function (sec) {
    var title = sec.querySelector('.fp-title');
    if (title) lines.push('\n=== ' + title.textContent + ' ===');
    sec.querySelectorAll('.fp-row').forEach(function (row) {
      var key = row.querySelector('.fp-key');
      var val = row.querySelector('.fp-val');
      if (key && val) lines.push(key.textContent + ': ' + val.textContent);
      else if (val) lines.push(val.textContent);
    });
  });
  var text = lines.join('\n');
  // Route through the bridge - QWebEngineView restricts navigator.clipboard
  // (only fires on a real user gesture from inside the page, often
  // silently drops the write).  Bridge owns the host clipboard handle
  // and the call always succeeds.  Fallback to navigator.clipboard +
  // execCommand only when the bridge isn't ready yet (e.g. during
  // QWebChannel handshake).
  var copied = false;
  try {
    if (typeof bridge !== 'undefined' && bridge && bridge.onCopyToClipboard) {
      bridge.onCopyToClipboard(text);
      copied = true;
    }
  } catch (e) {}
  if (!copied) {
    try {
      if (navigator.clipboard) { navigator.clipboard.writeText(text); copied = true; }
    } catch (e) {}
  }
  if (!copied) {
    try {
      var ta = document.createElement('textarea');
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
      copied = true;
    } catch (e) {}
  }
  // Visual ack so the user knows the click registered - flash the
  // copy button briefly.
  try {
    var btn = panel.querySelector('.fp-header button[title="Copy to clipboard"]');
    if (btn) {
      var _orig = btn.textContent;
      btn.textContent = copied ? '✓' : '✗';
      btn.style.background = copied ? '#2e7d32' : '#c62828';
      btn.style.color = '#fff';
      setTimeout(function () {
        btn.textContent = _orig;
        btn.style.background = '';
        btn.style.color = '';
      }, 900);
    }
  } catch (e) {}
}

// ---- QWebChannel init ----
if (typeof QWebChannel !== 'undefined') {
  new QWebChannel(qt.webChannelTransport, function (ch) {
    bridge = ch.objects.bridge;
    if (bridge && bridge.onReady) bridge.onReady();
  });
}
