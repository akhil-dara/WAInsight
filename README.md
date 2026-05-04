<div align="center">

<img src="logo.png" alt="WAInsight logo" width="120"/>

# WAInsight

### A forensic analysis suite for already-acquired WhatsApp Android databases

> **WAInsight does not extract anything from a phone.**  Phone acquisition is a separate step done with whatever forensic acquisition tool the analyst already uses.  WAInsight starts where that step ends — point it at the *folder of files* that came out of the acquisition (`msgstore.db`, `wa.db`, the `Media/` directory, the `Avatars/` directory) and it does the rest.

What it does, in one paragraph: ingests those files into a normalised, **read-only** case database, then opens a 30-page desktop UI where every conversation is **fully browseable just like the WhatsApp home screen** — chat list with avatars / unread / pinned / muted / archived, click any chat, scroll the timeline (bubbles, edits, revokes, replies, reactions, receipts, forwarded flags), with click-to-jump search, calendar filtering, mention chips, pinned-message strip, and a forensic-info side panel on every bubble.  On top of that browsing surface sit 30 forensic pages: media gallery, perceptual visual search, media recovery, ghost / edit / revoke browsers, calls page, locations, polls, links, contact + group reports, offline export bundles, and the folder-shaped Media Dashboard.

Built for digital forensics teams, law-enforcement examiners, and incident responders.  **Source `msgstore.db` is opened with `?mode=ro&immutable=1`** — WAInsight never writes to evidence.

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](#license) [![Python 3.10+](https://img.shields.io/badge/Python-3.10+-blue.svg)](#tech-stack) [![PySide6](https://img.shields.io/badge/Qt-PySide6-41cd52.svg)](#tech-stack) [![Status: active](https://img.shields.io/badge/status-active-brightgreen.svg)]()

</div>

---

## Table of contents

- [What WAInsight does](#what-wainsight-does)
- [Screenshots](#screenshots-suggested-shots-to-add)
- [Quick start](#quick-start)
- [Highlights](#highlights)
- [Pages](#pages)
- [Reports](#reports)
- [Offline HTML & dashboard exports](#offline-html--dashboard-exports)
- [Architecture](#architecture)
- [Forensic integrity](#forensic-integrity)
- [Tech stack](#tech-stack)
- [Repository layout](#repository-layout)
- [Roadmap](#roadmap)
- [License](#license)

---

## What WAInsight does

You point WAInsight at a folder containing **already-acquired** WhatsApp Android files (the tool does not pull anything from a phone — that's the acquisition step, done separately with whatever tool the analyst already uses).  Specifically it expects:

- `msgstore.db` (chats, messages, media, calls, polls, mentions, …)
- `wa.db` (saved contacts, business / verified state, avatars)
- `Media/` and `Avatars/` directories

…it ingests those files in **29 sequential stages**, normalises everything into a single `analysis.db` with **47 indexed tables**, and presents a desktop UI where the analyst can:

- **browse every chat exactly like opening WhatsApp itself** — home-screen-style conversation list, click any chat, scroll the timeline of bubbles, see edits / revokes / reactions / replies / receipts / forwarded badges inline
- triage **200k+ media files** through a folder-shaped offline dashboard with cascading filters
- recover deleted-from-device media via CDN re-download or hash-linking
- find every chat where the same SHA‑256 was shared (cross-chat propagation)
- run perceptual-hash visual search to find similar images across the whole case
- pivot between contacts, groups, calls, links, locations, polls, status updates, scheduled events
- export a single offline HTML bundle the case officer can hand off (no Python, no server, just `index.html`)
- generate landscape-A4 PDF / HTML forensic reports per group or per contact

Everything happens locally. No telemetry, no internet calls except optional CDN media re-download (and that requires the analyst's explicit click).

---

## Screenshots

> All screenshots are taken against a real case with PII blurred. Click any image to view full size. Source files live under [`docs/screenshots/`](docs/screenshots/).

### Hero

![WAInsight chat viewer](docs/screenshots/04_chat_viewer.png)

*Chat viewer rendered inside QWebEngine: bubbles, edit pencil pill, reply badges, forensic ℹ button per message, sender avatars, status ticks.*

### Getting in

| | |
|---|---|
| ![Case picker](docs/screenshots/01_case_picker.png) | ![Dashboard](docs/screenshots/02_dashboard.png) |
| **Case picker** — discover existing `.wfacase` packages or ingest a new extraction. | **Dashboard** — case-wide totals + activity heatmap as soon as a case is loaded. |
| ![Conversations](docs/screenshots/03_conversations.png) | ![Calendar heatmap filter](docs/screenshots/06_calendar_heatmap_filter.png) |
| **Conversations list** — WhatsApp-style with avatars, unread badges, pinned / muted / archived markers, search. | **Calendar heatmap filter** — every day shows its message count like an airline-fare grid; click + drag to filter. |

### Reading a chat

| | |
|---|---|
| ![Forensic info panel](docs/screenshots/05_forensic_info.png) | ![Edit history popup](docs/screenshots/08_edit_history_popup.png) |
| **Forensic info panel** — every bubble's ℹ button opens this panel: msgstore source IDs, origination flags, SQL provenance, raw JID. | **Edit history popup** — every revision of an edited message side-by-side, with pre-edit text fully visible. |
| ![Replies sidebar](docs/screenshots/07_replies_sidebar.png) | ![Receipt details](docs/screenshots/09_receipt_details.png) |
| **Replies sidebar** — click a reply chain badge → see every reply to that message + a "Go to original" button. | **Receipt details** — click any tick → per-recipient delivered/read/played timeline with millisecond lag. |
| ![Image right-click menu](docs/screenshots/10_chat_image_context_menu.png) | ![Find Copies popup](docs/screenshots/11_find_copies_popup.png) |
| **Right-click context menu on a media bubble** — Find Copies (exact SHA-256), Find Similar Images (perceptual), copy IDs / file path / key, open file location. | **Find Copies popup** — every chat that ever shared the same SHA-256, with "Go to chat" buttons that jump to the exact message. |
| ![View-once download](docs/screenshots/12_view_once_download.png) | |
| **View-once recovery** — voice notes / images marked "view-once" stay downloadable from the bubble even after the on-device file expired (uses CDN URL + media_key from msgstore). | |

### Browse & search

| | |
|---|---|
| ![Media gallery cascading](docs/screenshots/13_media_gallery_cascading.png) | ![Media recovery dashboard](docs/screenshots/14_media_recovery_dashboard.png) |
| **Media Gallery** — cascading filters (sender × conversation × date × type × status) on a fast thumbnail grid. | **Media Recovery** — per-conversation breakdown of On-Disk / Downloadable / Expired / Missing-No-Key media, one-click bulk re-download. |
| ![Image similarity page](docs/screenshots/15_image_similarity_page.png) | ![Image similarity results](docs/screenshots/16_image_similarity_results.png) |
| **Image Similarity** — drop a screenshot, browse, or paste from clipboard; pick Exact (SHA-256) or Visual (pHash) match mode. | **Match results** — Exact / Near-Exact / Near-Duplicate / Template-Match tiers across the whole case (89 651 indexed images here). |
| ![Calls page](docs/screenshots/17_calls_page.png) | ![Locations page](docs/screenshots/18_locations_page.png) |
| **Calls page** — call records with type / direction / result filters; the calendar popup shows per-day call count badges. | **Locations** — every static + live location share, with start/final coordinates and live-share durations. |
| ![Polls page](docs/screenshots/19_polls_page.png) | |
| **Polls** — every poll with options + vote tallies; click a row to see the per-option breakdown chart. | |

### Group + contact intelligence

| | |
|---|---|
| ![Group Info — owner banner](docs/screenshots/24_group_info_owner_banner.png) | ![Group members + former](docs/screenshots/26_group_members_former.png) |
| **Group Info — device-owner banner** — clearly states whether the case-owner is admin / member / removed in this group, plus the decoded `chat.participation_status` source. | **Group members + former** — current roster with role / message / media counts; Former Members section sources from `group_past_participant`, `group_member.is_current=0`, AND message-only inference. |
| ![Group edit history](docs/screenshots/25_group_edit_history.png) | |
| **Group Edit History** — every name / DP / description / settings change with diff view, sortable by type. | |
| ![Device history](docs/screenshots/22_device_history.png) | ![Contact report](docs/screenshots/23_contact_report.png) |
| **Per-contact device sessions** — every device this contact has used (Primary Android, iPhone, 14 Web/Desktop companions here), first/last seen, message split, confidence score. | **Contact Activity Report** (HTML) — full forensic identity + per-group activity + 1-on-1 timeline + reactions + groups in common. |

### Reports

| | |
|---|---|
| ![Group report dialog](docs/screenshots/27_group_report_dialog.png) | ![Top contributors snippet](docs/screenshots/28_report_top_contributors.png) |
| **Group Report dialog** — pick output format (HTML / PDF), restrict to a date range, tick exactly which sections to include. | **Top Contributors snippet** — example section from a generated report: ranked bar chart with full JID per contributor, owner-aware. |

### Tagged-messages bundle export

| | |
|---|---|
| ![Tagged export dialog](docs/screenshots/20_tagged_export_dialog.png) | ![Tagged export viewer](docs/screenshots/21_tagged_export_viewer.png) |
| **Tagged export dialog** — three modes: full conversations / tagged only / tagged ± N day buffer. Bundles the offline HTML viewer + (optionally) the actual media files; output is a single ZIP. | **Tagged export viewer (browser)** — the exported `index.html` opened offline in Chrome: chat list + the tagged conversation; compaction markers count messages hidden between tagged ones. |

---

## Quick start

```bash
# 1. Clone + install dependencies (Python 3.10+ required)
git clone https://github.com/<you>/WAInsight.git
cd WAInsight
python -m pip install -r requirements.txt

# 2. Launch the GUI
python wainsight.py
```

On first run the launch screen asks you to either **create a new case** (point at a folder containing `msgstore.db` + friends — the ingester runs all 29 stages with progress events) or **open an existing `.wfacase` package** if one already exists.

> **Forensic note:** WAInsight opens `msgstore.db` with `?mode=ro&immutable=1`. The original evidence file is never modified. Every ingestion stage is logged to `chain_of_custody.jsonl` inside the `.wfacase` package, with SHA-256 hashes of every source database read.

### System requirements

- **OS:** Windows 10/11, macOS 12+, modern Linux
- **Python:** 3.10 or newer
- **RAM:** 8 GB minimum, 16 GB recommended for cases with > 1 M messages
- **Disk:** roughly 2× the size of the source `Media/` folder (for the read-only mirror + thumbnails + indexes)
- **Optional:** `ffmpeg` on `PATH` for video thumbnails in the Media Dashboard, `pymupdf` (`pip install pymupdf`) for PDF first-page thumbnails

---

## Highlights

**Read-only by construction.** Source `msgstore.db` is mounted with SQLite's `?mode=ro&immutable=1` flag, the case folder is the only writeable surface, and every operation is journaled to `chain_of_custody.jsonl` with timing + source hashes.

**29-stage ingestion pipeline.** Messages → media → calls → mentions → albums → reactions → polls → links → locations → status → revokes → edits → group metadata → past participants → admin events → vcards → comments → pin events → contact resolution → key-id platform classification → device sessions → orphan media discovery → hash-link auto-pass → HD/SD twin linking → motion-photo association → FTS5 indexing → daily/hourly stats rollup → owner identification → source DB hashing.

**Owner-aware everywhere.** WhatsApp messages from the device owner have `sender_id IS NULL` — every report and every page that joins to `contact` injects the device-owner identity from `case_metadata` so owner activity never surfaces as "Unknown" or blank rows.

**Per-message platform attribution — visible while you browse.** Every bubble carries an inline tag — **Android · iPhone · Web/Desktop · Companion #N** — derived at ingest time from the WhatsApp `key_id` length + prefix patterns + companion-device key ID lookups. You see the device that sent each individual message right next to the timestamp, not buried in a separate page. Same column drives the Group Report's *Device Platform Usage* breakdown, the contact's *Device Sessions* tab (Primary vs companions, with first/last seen, message split, confidence score), and the Calls page's call-side classification. Crucial when a case rests on whether a given WhatsApp message came from the suspect's phone or one of their linked Web sessions.

**Folder-shaped Media Dashboard.** A single output folder with `index.html`, sharded AVIF thumbnails (`thumbs/aa/bb/<sha>.avif`, ~3 KB each), chunked metadata (`data/meta_NNN.js`), and a vendored `app.js` that runs a bitset crossfilter + virtual list + IntersectionObserver in the browser. Handles **200k media rows** at file:// with sub-millisecond facet filtering. Cascading filters by conversation × sender × MIME × extension × status × date, with per-day histogram (flight-fare style), CSV / XLSX / HTML exports, and "find every chat that shared this hash".

**Offline HTML viewer bundle.** Hand off a single ZIP — opens from `file://`, no Python, no server. WhatsApp-Web-style chat list, full message rendering, FTS5-equivalent search, tagged-messages sidebar, compaction markers between non-adjacent included messages.

**Cross-Contact Analysis.** Pick 2 + contacts → instantly see what they share: groups they're all in, calls between them, file SHA-256 hashes any of them have shared in common, cross @-mentions, every conversation any of them appears in. Owner is a first-class pickable contact.

**Perceptual visual search.** Drop a query image → three confidence tiers of matches across the whole case (pHash + dHash + edge-map). Catches re-shares of the same content even after recompression.

**Media recovery.** Missing-on-device media with valid CDN URL + decrypt key gets one-click re-download (`pycryptodome` for the AES-CBC). Hash-linked recovery: a missing message that shares a SHA-256 with a present one is auto-resolved to the sibling's bytes (and tagged `recovery_method='hash_linked'` so it's never confused with a real local copy).

**Forensic info panel on every bubble.** ℹ button on any message → side panel with msgstore source IDs, every SQL row that contributed to the rendered bubble, origination flags decoded, per-device receipt timeline.

---

## Feature catalog

Every item below was personally designed and built into the tool — this is the deliberate feature set, not a wishlist.  Grouped by what the analyst is actually trying to do.

### Core browsing

|   | Feature |
|---|---|
| 1 | **Robust LID + JID parsing** — phone, group, broadcast, newsletter, bot, device suffixes, agent variants, LID privacy-restricted addressing, the lot. |
| 2 | **WhatsApp-style conversation home** — avatars, last-message preview, time, unread badge, pinned / muted / archived / locked markers. |
| 3 | **Group name visible inline** in the chat header AND in the in-chat sender label — you always know which group a bubble lives in. |
| 4 | **Pinned-messages strip** with WhatsApp-style prev/next browser → click any pin to jump straight to the pinned message. |
| 5 | **Forensic ℹ button on every bubble** — opens a side panel with msgstore source IDs, every SQL row that fed the bubble, origination flags decoded, per-device receipt timeline. |

### Message timeline integrity

|   | Feature |
|---|---|
| 6 | **Mentions parsed** for every conversation, rendered as click-to-profile chips — clicks open the contact detail page. |
| 7 | **Ghost-message reconstruction** — deleted-for-everyone messages recovered from `message_quoted_text` and rendered inline next to the revoked bubble. |
| 8 | **Edit history** per message — pencil pill on every edited bubble opens a side-by-side revision timeline (built from FTS index + quoted-text reconstruction). |
| 9 | **Reply chains** — every quoted message gets a "↰ N replies" badge; click → sidebar listing every reply + a "Go to original" button (cross-conversation jumps too). |
| 10 | **60+ system events decoded** — group / security / admin / calls / privacy / business / ephemeral / disappearing-settings — all rendered as readable text instead of opaque type codes. |
| 11 | **Per-message receipts** — every bubble shows delivered + read ticks; click any tick → per-recipient timeline with delivery / read / played millisecond lag. |
| 12 | **Forwarded-flag indicator** on every forwarded bubble. |

### Search

|   | Feature |
|---|---|
| 13 | **FTS5 global search** with sender / conversation / date / ghost filters; results panel as a sidebar inside the chat with click-to-jump highlights. |
| 14 | **Calendar filter with per-day message counts** — every cell shows that day's volume, flight-fare style; click + drag to filter. |

### Media analysis

|   | Feature |
|---|---|
| 15 | **Media Gallery** with cascading checkbox filters — sender × conversation × date × type × status — over a fast thumbnail grid. |
| 16 | **One-click + bulk download / decrypt of missing media** — driven by CDN URL + `media_key` + expiry timestamp; AES-CBC decrypt via pycryptodome. **View-once media** (images / voice notes) re-downloadable from the bubble even after the on-device file expired. |
| 17 | **Re-downloaded media is flagged** — bubble shows a "Downloaded ✓ (recovered)" badge so the analyst can tell original-on-device bytes apart from CDN-recovered ones. |
| 18 | **Hash-link auto-rescue** — if a message's media is missing locally but another message's media has the same SHA-256 on disk, the missing row resolves to the sibling's file and is tagged `recovery_method='hash_linked'` (never confused with a real local copy). |
| 19 | **Thumbnail-only fallback** — when even the bytes are gone, the WhatsApp `thumbnail_blob` is rendered with a "Thumbnail only" status pill. |
| 20 | **HD / SD twin pair surfaced** — every bubble shows both copies with file sizes, "↗ HD #X" / "↘ SD #Y" cross-jumps, and a "Download HD" CTA when only the SD bytes are local. |
| 21 | **Motion / Live photos** — still parent shows a "▶ Live" badge that plays the 1-2 s motion clip on click. |
| 22 | **Cross-chat share chain** — right-click any media → SHA-256 + encrypted-hash matches across every chat in the case, sorted chronologically, with go-to-message buttons.  Says where the bytes were *first seen*, not just where they were forwarded. |
| 23 | **Cross-chat share badge in the gallery** — every tile labels how many other chats hold the same SHA-256, click → jump list. |
| 24 | **Perceptual visual-hash search** — drop a screenshot or pick from a chat: returns Exact / Near-Exact / Near-Duplicate / Template-Match tiers across the whole case.  *Example workflow:* select a PhonePe payment screenshot → find every PhonePe screenshot anyone has ever shared.  Or: pick a camera original from `DCIM/` → find which chats received it. |
| 25 | **Orphaned-media browser** — files in `Media/` with no surviving message row (cleared chats / reinstall / lost data) plus auto-rescue back-fill against surviving message hashes. |

### Identity & devices

|   | Feature |
|---|---|
| 26 | **Per-message device platform attribution** — every bubble carries an inline tag (Android · iPhone · Web/Desktop · Companion #N) derived at ingest from `key_id` length + prefix patterns + companion key-ID lookups, with a confidence score. |
| 27 | **Per-contact device sessions** in the contact detail page — Primary vs companions (Web/Desktop, linked Android, etc.), first-seen / last-seen, personal vs group message split, confidence per session. |
| 28 | **Unified contact registry** merged from 5 sources — `jid_map`, `wa_contacts`, `lid_display_name`, group labels, mention names — so every JID resolves to a single canonical identity. |

### Calls

|   | Feature |
|---|---|
| 29 | **Calls page** with filters by 1-on-1 / multi-person / voice / video / answered / declined / missed; per-day count badges in the calendar picker. |
| 30 | **Synthetic voice-chat / orphan-call reconstruction** — calls that have no `message` row in their conversation get virtual rows reconstructed so they render in every participant's chat timeline. |
| 31 | **Group voice chats appear inside the group chat** — even when WhatsApp didn't write a `message` row for them, the call still shows up at its real position in the group's timeline. |

### Communities & groups

|   | Feature |
|---|---|
| 32 | **Community membership** with LID resolution — every member surfaces with phone JID + LID side-by-side, even when the community is privacy-restricted. |
| 33 | **Community channel comments** — comment authors and their phone numbers resolved through the JID map even when WhatsApp only stored their LID. |
| 34 | **Past participants reconstructed from 3 sources** — `group_past_participant`, `group_member.is_current=0`, AND message-presence inference (catches members WhatsApp's own roster purged after a long enough gap). |
| 35 | **Owner can-post banner** on every Group Info page — explicit Yes / No with the underlying source row (`chat.participation_status`, group admin flags) so the analyst sees *why*. |

### Reports & exports

|   | Feature |
|---|---|
| 36 | **Per-contact forensic report** (HTML or PDF) with full identity, devices, stats, calls, groups in common, mentions, reactions, media & links — choosable sections + save location. |
| 37 | **Offline ZIP chat export** — WhatsApp-Web-style conversational viewer, opens from `file://`, no Python / server, with global cross-conversation search. |
| 38 | **Media Forensics Dashboard** — folder-shaped offline artifact (sharded AVIF thumbnails, chunked metadata, vendored UI engine) that scales to 200k+ media rows; cascading filters, per-day histogram, in-browser CSV / XLSX / HTML export, "find every chat that shared this hash" popup. |

---

## Pages

The sidebar groups 30 pages into **Overview**, **Forensics**, and **More**.

### Overview
| Page | What it does |
|---|---|
| **Dashboard** | Case-wide rollup: totals, top contacts (owner-aware), hourly heatmap, day-of-week breakdown |
| **Conversations** | Home-style chat list with avatars, unread, pinned/muted/archived markers, search, calendar date filter |
| **Status Updates** | Status posts with author, view count, reply chain |
| **Contacts** | Full roster with platform tags, business markers, message counts; per-contact detail page with devices + report button |
| **Media Gallery** | Cascading-filter thumbnail grid (sender × conversation × date × type × status); right-click → find similar / find shared |
| **Documents** | Files-only browser with extension rail, risky-extension flagging, find-shared popup, right-click context menu |
| **Calls** | Call records with type / direction / result filters, search, **per-day count badges in the calendar picker** |
| **Scheduled Events** | WhatsApp Events: title, time, participants, response counts |
| **Search** | Global FTS5 with sender / date / conversation / ghost filters + click-to-jump |
| **Analytics** | Avg/day, peak day, busiest hour, top contacts (owner included), hourly heatmap, day-of-week bars |

### Forensics
| Page | What it does |
|---|---|
| **Cross-Contact Analysis** | Pick 2 + contacts, see shared groups, calls between them, files in common, cross @-mentions, common conversations |
| **Ghost Messages** | Deleted-for-everyone messages recovered from `message_quoted_text` with go-to-message |
| **Edit History** | Every edited message + every revision, click-to-jump |
| **Revoked Messages** | All revoked messages with revocation actor + timestamp |
| **System Events** | 60 + decoded event types (group, security, admin, calls, privacy, business, ephemeral) |
| **Media Recovery** | Missing-but-downloadable media with status pills + one-click CDN re-download |
| **Image Similarity** | Drop a query image → 3-tier matches (pHash / dHash / edge-map) across the whole case |
| **Orphaned Media** | Files in `Media/` with no surviving message row + auto-rescue back-fill |
| **Starred Messages** | WhatsApp-starred messages with bundle / CSV / HTML export |
| **Tagged Messages** | Investigator-applied tags with notes + bundle export (full / tagged-only / tagged + buffer modes) |

### More
| Page | What it does |
|---|---|
| **Locations** | Static + live locations with start / final coordinates, map preview thumbnails, Google Maps links |
| **Links** | Forensic links browser: domain rail, risky-only filter, top-domain bar chart, sender/conv/date filters, find-shared popup, CSV/HTML export |
| **Polls** | Poll questions, options, vote tallies, voter identity |
| **Export** | Offline HTML viewer bundle generator |

---

## Reports

### Group Forensic Report

Per-group landscape-A4 PDF or HTML, generated from any Group Info page via the **Report** button. Includes:

- **Case & Evidence Provenance** banner: case id, examiner, source database paths + SHA‑256 hashes, ingestion timestamp
- **Group Identity**: name, JID, chat_id, conversation_id, type, addressing mode (LID / phone), creator, first/last message
- **Device Owner & Send Policy**: owner role in this group + decoded send/edit/membership rules from `wa_group_admin_settings`
- **Summary** cards: messages / members / admins / media / links / forwards
- **Group Edit History** with profile-picture diff
- **Current Members** (compact landscape table): DP · stacked Identity (name + phone + JID + LID) · Role · Msgs · Media · Links · Mentions · stacked Activity (joined / first / last). **Owner sorts first** with amber-highlighted row.
- **Top Contributors** + **Top Forwarders** (with category breakdown)
- **Device Platforms** (Android/iPhone/Web split per member, owner-aware)
- **Mentions Network** (most-mentioned, most-active mentioners, edge list — all owner-aware)
- **Activity** (hourly bars + daily mini-chart)
- **Calls** with category badges + per-call duration + result
- **Locations** with live-location START + FINAL coordinate cells (Google Maps links)
- **Media & Links**: 60+-entry message-type taxonomy (Type 64 / 82 / 90 / 92 / 112 / 116 etc. mapped to readable labels) + top link domains
- **Bot Activity** (Meta AI etc., with per-bot top-summoner ranking)
- **Former Members**: 3-source resolution (`group_past_participant` ∪ `group_member.is_current=0` ∪ message-only inference) — never silently empty

### Contact Forensic Report

Per-contact PDF or HTML via the contact detail page. Section picker dialog lets the analyst toggle: identity / overall stats / activity patterns / per-group activity / 1-on-1 summary / calls / groups in common / mentions / reactions / media & links. **Format selector** (HTML / PDF) and **Save location** picker built into the same dialog. Owner-aware mention rows.

### Media Forensics Dashboard

A separate folder-shaped offline artifact (see [Offline HTML & dashboard exports](#offline-html--dashboard-exports)) that scales to **228k+ media rows** in any case.

---

## Offline HTML & dashboard exports

Two distinct offline-handoff formats:

### 1. Viewer Bundle (`Export` page)

A single ZIP containing:
- `index.html` — opens from `file://`, no Python, no server
- WhatsApp-Web-style chat list
- Full message rendering (incl. ghost / edits / revokes / reactions / quotes / forwarded badges)
- FTS5-equivalent search across all included messages
- Tagged-messages sidebar tab when bundle was made from the Tagged Messages page
- Per-conversation Ctrl+F search bar
- Compaction markers showing how many messages were collapsed between non-adjacent included messages

### 2. Media Dashboard

A folder with:
```
output_dir/
  index.html                ← opens in any modern browser at file://
  vendor/  app.css, app.js  ← bundled UI engine, no CDN
  data/    manifest.js, meta_000.js … meta_NNN.js
  thumbs/  aa/bb/<sha>.avif (sharded by hash prefix, deduped)
```

Cascading filters (conversation × sender × MIME × extension × status × date), per-day histogram, top-domains chart, virtual list with IntersectionObserver-driven thumb loading, in-browser CSV / XLSX / HTML exports, "find every chat that shared this hash" popup. **Thumbnails:** AVIF when PIL has the plugin (≈3 KB / thumb at 224 px), JPEG fallback. **Disk-priority:** when the original file is on disk we re-render from it for near-original quality; PDF first pages come from the WhatsApp blob (no PyMuPDF dependency required).

### 3. Group / Contact PDF reports

Landscape-A4 PDFs rendered through `QWebEngineView.printToPdf` with an off-screen 1400×1800 viewport so wide tables compute proper column widths before printing.

---

## Architecture

```
┌──────────────────────── .wfacase package ────────────────────────┐
│                                                                  │
│  case.json            chain_of_custody.jsonl                     │
│  analysis.db          analysis.db-shm   analysis.db-wal          │
│  sources/             read-only copy of msgstore.db, wa.db, …    │
│  media/               resolved on-disk media tree                │
│  _gallery_thumbcache.db   per-case L2 thumbnail cache            │
│  exports/             HTML viewer bundles, dashboards, PDFs      │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
            ▲                                       ▲
            │ written by                            │ read-only by
            │                                       │
┌───────────┴──────────────┐         ┌──────────────┴────────────┐
│  backend/                 │         │  gui/  (PySide6)          │
│   • Orchestrator          │         │   • 30-page navigation    │
│   • 29 sequential stages  │         │   • Chat viewer = QWebEng │
│   • progress events       │         │     + QWebChannel bridge  │
│  CLI: python run_ingest.py│         │  Entry: python wainsight.py│
└───────────────────────────┘         └───────────────────────────┘
```

### Two-process model

The ingester (`backend/run_ingest.py`) and the viewer (`gui/main.py`) are fully decoupled:

- The ingester only writes to `analysis.db` and emits JSON progress events to stdout. It can run headlessly on a server.
- The viewer only reads from `analysis.db` (`?immutable=1` + `PRAGMA query_only=1`) and the case directory. Multiple viewers can open the same case simultaneously.

### Chat viewer

For chats with > 5 000 messages the viewer switches to a **windowed-flat virtual scroller** (Chrome / QtWebEngine). It keeps a sliding window of 500 fully-rendered messages around the viewport centre and uses sharded `<script>`-loaded tiles (100 messages each) plus `data-global-idx` anchors so jumping to message #47162 in a 47k-message chat is O(1). Tombstones for not-yet-loaded windows render as skeleton-shimmer placeholders so scrolling never shows blank gaps.

### Offline-rendering for huge media cases

The Media Dashboard is **folder-shaped, never one giant `.html`** so V8 string limits (1 GiB) and renderer-process memory caps (4 GiB) don't bite. The folder layout (sharded thumbs, chunked metadata, vendored UI engine) is the established pattern for offline forensic artifacts that need to scale into the hundreds of thousands of rows while still opening from `file://` on any modern browser.

---

## Forensic integrity

| Property | How it's enforced |
|---|---|
| Source `msgstore.db` is never written | `sqlite3.connect("file:msgstore.db?mode=ro&immutable=1", uri=True)` everywhere |
| Source files are SHA-256 hashed at ingest | `_stage_hash` writes `source_hash_<filename>` rows into `case_metadata` |
| Every ingest action is journaled | `chain_of_custody.jsonl` — one JSON object per stage with timing + status |
| Recovered media is tagged | `media.recovery_method` = `original` / `downloaded` / `hash_linked` / `hash_linked_after_delete` / `orphan_recovered` (12-state taxonomy preserved in every report and the Media Dashboard) |
| Owner identity is explicit | Stored in `case_metadata` as `device_owner_name / phone / jid / lid_jid` and threaded through every report + page section so owner messages never surface as "Unknown" |
| Original IDs preserved | `message.source_msg_id`, `media.source_media_row_id`, `contact.source_jid_row_id`, etc. — every analysis row links back to its msgstore.db / wa.db origin row |
| Timestamps double-encoded | Every report shows local time + UTC in brackets so the case timezone is unambiguous |

---

## Tech stack

| Layer | Tool |
|---|---|
| GUI framework | **PySide6** (Qt 6 official Python bindings) |
| Theming | **qt-material** + custom QSS for light + dark parity |
| Chat rendering | **QWebEngineView** (Chromium) + `QWebChannel` bridge to Python |
| Data layer | **SQLite** with FTS5 + custom `analysis.db` schema (47 tables) |
| Image processing | **Pillow** (with built-in AVIF / WebP / JPEG-XL plugins on Pillow 12+) |
| Crypto | **pycryptodome** for view-once / encrypted attachment decrypt |
| Spreadsheet export | **openpyxl** |
| Protobuf parsing | **protobuf** (used for some msgstore inner blobs) |
| Optional: video thumbnails | **ffmpeg** on PATH |
| Optional: PDF thumbnails | **PyMuPDF** (`pip install pymupdf`) |
| Tested OS | Windows 10 / 11 (primary), macOS 12+, Ubuntu 22.04 |

---

## Repository layout

```
WAInsight/
├── wainsight.py                     # GUI launcher
├── requirements.txt
├── README.md                        # ← you are here
├── LICENSE
│
├── backend/                         # Pure-Python: no Qt imports
│   ├── run_ingest.py                # Headless ingest CLI
│   └── app/
│       ├── ingestion/               # 29 stage modules + orchestrator
│       │   ├── orchestrator.py
│       │   ├── message_ingester.py
│       │   ├── media_ingester.py
│       │   ├── call_ingester.py
│       │   ├── revoke_ingester.py
│       │   ├── edit_ingester.py
│       │   ├── contact_resolver.py
│       │   ├── orphaned_media_ingester.py
│       │   ├── keyid_classifier.py
│       │   └── …
│       ├── db/schema.py             # 47-table analysis schema
│       ├── reports/                 # Report generators
│       │   ├── group_report.py
│       │   ├── contact_report.py
│       │   ├── media_report.py      # Folder-shaped dashboard generator
│       │   └── dashboard_assets/    # index.html + app.css + app.js
│       └── export/                  # Offline HTML viewer bundle
│           ├── viewer_bundle_exporter.py
│           └── viewer_assets/       # index.html / viewer.js / viewer.css
│
├── gui/                             # PySide6 only
│   ├── main.py
│   └── app/
│       ├── views/
│       │   ├── pages/               # 30 page modules
│       │   │   ├── chat_viewer_page.py
│       │   │   ├── group_info_page.py
│       │   │   ├── contact_detail_page.py
│       │   │   ├── media_gallery_page.py
│       │   │   ├── documents_page.py
│       │   │   ├── calls_page.py
│       │   │   ├── links_page.py
│       │   │   ├── cross_contact_page.py
│       │   │   └── …
│       │   ├── widgets/             # Chat renderer JS, calendar heatmap, etc.
│       │   │   ├── chat_renderer.js   # Windowed-flat virtual scroller
│       │   │   ├── chat_styles.css
│       │   │   ├── chat_web_view.py   # QWebEngineView host
│       │   │   ├── chat_bridge.py     # QWebChannel bridge
│       │   │   └── calendar_heatmap.py
│       │   └── dialogs/             # Report / export / tag dialogs
│       ├── services/                # Database, ThemeManager, MediaCrypto, ImageSimilarity
│       └── resources/themes/        # light.qss, dark.qss
│
└── shared/                          # Used by both backend + gui
    ├── system_event_formatter.py    # 60+ system event types → human text
    └── forensic_provenance.py       # Bubble's ℹ side-panel data builder
```

---

## Roadmap

Currently planned (no firm dates):

- **WhatsApp Business** support — the Business app uses a similar but distinct schema (extra columns for catalogue, orders, quick-replies, labels, business-profile metadata).  Adding a parallel ingester so cases mixing personal + Business accounts can be analysed in the same UI.
- Server-mode ingestion (long-running process listening on a UNIX socket)
- Optional GPU acceleration for the perceptual-hash search on very large cases
- Timeline pivot (scroll any page → other pages snap to the same time window)

Pull requests are welcome. The codebase is heavily commented in the doc-string-driven style — most files start with a multi-paragraph "why this exists" header so newcomers can find their feet quickly.

---

## Acknowledgements

The schema research, the 29-stage ingestion pipeline, and the 30 analysis pages here are my own work — built up over many months of reverse-engineering `msgstore.db` + `wa.db`, the `Media/` layout, WhatsApp's `key_id` patterns and the device-companion key-ID space, then iterating against real cases.

Part of that work was usefully **cross-checked** against the published research of:

> **Francisco Arenaz Benito** — *Análisis forense de la aplicación WhatsApp en sistemas Android e iOS*
> Ediciones Universidad de Salamanca · Ágora Policial · ISBN **978-84-1091-202-1**
> [eusal.es / 978-84-1091-202-1](https://eusal.es/producto/analisis-forense-de-la-aplicacion-whatsapp-en-sistemas-android-e-ios/)

His book confirmed several hypotheses I'd already formed and saved time on validation — credit and thanks where due.  Anyone serious about WhatsApp forensics on Android should read it.

The other accelerator during development was my own companion tool, open-sourced separately:

> **SQLite GUI Analyzer** — [github.com/akhil-dara/sqlite-gui-analyzer](https://github.com/akhil-dara/sqlite-gui-analyzer)

That GUI is what made the schema-mapping work tractable.  My actual workflow when reverse-engineering a WhatsApp table looked like:

1. Open `msgstore.db` in the analyzer.
2. Search a known value globally — e.g. paste a poll's `_id`, a message's `key_id`, a JID, a SHA-256 — and let the analyzer scan **every column of every table** for matches.
3. Double-click each hit to open that row in its own panel; line up several panels **side-by-side** so I can see the same value lighting up in three or four related tables at once.
4. Right-click → **Copy schema** for each table I'm comparing, paste into a scratch buffer, annotate.
5. Pick an exact-match search on the next ID, validate the foreign-key relationship directly against my own evidence DB instead of re-reading PRAGMA output.

That click-search-validate loop is faster than reading the schema text on its own and is what surfaced things like the `message_quoted_text` ghost-recovery path, the album parent/child linkage, the `chat.participation_status` owner-role encoding, and the HD/SD twin association — all things you'd otherwise miss if you only looked at the static schema.

If you're trying to ingest a different app's SQLite store, that's the tool I'd recommend starting with.

### Per-message platform attribution — separate research

The Android / iPhone / Web / companion-device tag that ships on every message bubble is a **separate piece of empirical research**, *not* something the SQLite GUI Analyzer surfaced.  I built the classifier the hard way:

- Started by noticing a recurring prefix pattern in the `key_id` column on every message I'd sent from my own Android handset.
- Asked friends on iPhone to send me chat exports + collected JIDs of friends who messaged me from iPhones — different `key_id` shape, with its own consistent prefix length / charset.
- Repeated the exercise for **WhatsApp Web / Desktop** sessions and for linked-companion devices (the secondary Android / iPad / Web sessions WhatsApp lets you attach).
- Cross-referenced enough samples to write a robust classifier with a confidence score per message — that's what powers the inline **Android · iPhone · Web/Desktop · Companion #N** tag on every bubble + the per-contact *Device Sessions* table on the contact detail page.

Acknowledging this honestly: this part wasn't accelerated by any tool — it was patient sample collection from real users on real devices, then iterating until the rule set held up against new data.

---

## License

[MIT](LICENSE) — see the LICENSE file for the full text.

WAInsight is provided **as-is** for legitimate digital-forensic and incident-response work. Use of this tool against extractions you do not have legal authority to analyse is your responsibility. The authors disclaim liability for misuse.

---

<div align="center">

**WAInsight** — built with care for the forensic community.

Found a bug?  Open an issue.

</div>
