# System Design

Table, column, and path names come from [data-model.md](data-model.md); endpoints come from [api-specification.md](api-specification.md).

## 1. Module breakdown (`src/doc_intel/`)

| Module | Responsibility | Key files |
|---|---|---|
| `core/` | settings, logging, errors, auth, tenant context, URL signing | `config.py` (`Settings`), `logging.py`, `errors.py` (`AppError`), `security.py` (bearer check, HMAC signing), `tenancy.py` (`TenantContext(user_id, project_id)`) |
| `db/` | async SQLAlchemy 2.0 models and tenant-scoped repositories | `session.py`, `models.py`, `repo.py` |
| `storage/` | `StorageBackend` protocol + local implementation | `base.py`, `local.py` |
| `ingestion/` | upload validation, parse → chunk | `schemas.py` (`ParsedElement`, `ParsedDocument`, `ChunkPayload`), `upload.py` (format/magic/size checks, sha256, storage keys), `docling_parser.py`, `native_parsers.py`, `chunker.py`, `pipeline.py` |
| `embeddings/` | dense via Ollama, BM25 sparse, optional reranker | `schemas.py`, `ollama.py`, `sparse.py`, `reranker.py` |
| `vector/` | **only** Qdrant access point | `qdrant_store.py` |
| `llm/` | provider-agnostic gateway, prompts | `gateway.py`, `prompts/*.md` |
| `templates/` | Stage 1 anchor extraction, Stage 2 IR build + validation | `anchors_pptx.py`, `anchors_docx.py`, `anchors_xlsx.py`, `ir_builder.py`, `schemas.py`, `library.py` (library `register`, copy-on-use `adopt` Phase 9, `plan_sync` / `sync_templates_dir` ADR-017) |
| `mapping/` | Stage 3 retrieval, extraction, verification, status resolution | `retriever.py`, `extractor.py`, `verifier.py`, `engine.py`, `schemas.py` |
| `writers/` | Stage 4 surgical writers + markers | `pptx_writer.py`, `docx_writer.py`, `xlsx_writer.py`, `markers.py`, `runs.py` (shared `w:`/`a:` paragraph and run rules) |
| `chat/` | Pipe chat orchestration: intent routing, Q&A, summaries, library choice, edits | `router.py`, `qa.py`, `summary.py`, `library.py` (`library_choice`), `edits.py` |
| `jobs/` | Postgres queue worker + handlers per `jobs.kind` | `worker.py`, `handlers.py` |
| `api/v1/` | FastAPI routers, request/response models | `health.py`, `pipe.py`, `projects.py`, `documents.py`, `jobs.py`, `query.py`, `templates.py`, `mapping_runs.py`, `artifacts.py`, `library.py` |
| `main.py` | app factory, lifespan (DB pool, Qdrant client, worker start) | |
| `cli.py` | operator CLI for the template library: `seed-template`, `list-templates`, `show-template`, `remove-template`, `reset-library` (the same library is also managed over HTTP, `api/v1/library.py`, ADR-014) | |

Outside the package: `integrations/openwebui/doc_intel_pipe.py` (the Pipe Function, pasted into Open WebUI), `frontend/` (the custom React UI, served at `/` when built) and `migrations/` (Alembic).

**Dependency direction.** `api` → `chat`/`jobs` → `mapping`/`templates`/`ingestion`/`writers` → `llm`/`embeddings`/`vector`/`storage`/`db` → `core`. Lower layers never import upper ones.

## 2. Ingestion

1. `POST …/documents` or `POST /v1/pipe/files`:
   - validate the extension against the format allow-list, check the magic bytes, enforce `MAX_UPLOAD_MB`; an OOXML file over 512 MB unzipped, or with an `.xml`/`.rels` part over 32 MB, is `413` (Phase 12);
   - compute `sha256` and deduplicate on `(project_id, sha256)`;
   - `StorageBackend.put_bytes(uploads/{document_id}/original.{ext})`;
   - insert `documents` with `status = 'uploaded'`;
   - enqueue `ingest_document`.
2. The worker sets `parsing`, then picks a parser by extension. Docling stops at `MAX_PAGES = 1000` pages (the document fails with `parse_error`), so one huge file cannot hold the single worker for hours (Phase 12):

| Ext | Parser | `page_number` | `section_heading` |
|---|---|---|---|
| pdf | Docling (layout, TableFormer, RapidOCR) | PDF page (1-based) | nearest heading item |
| docx | Docling | null (DOCX has no pages) | nearest heading |
| pptx | Docling | slide number | slide title |
| xlsx | `openpyxl` read-only, `data_only=True` | null | sheet name |
| csv | stdlib `csv` | null | null |
| txt | plain text, split on blank lines | null | null |
| md | plain text, split on `#` headings | null | heading text |

Docling tables (Phase 22): each cell is written once at its top-left slot from `table_cells` (spanned slots stay empty; `data.grid` would repeat the text), and a table that starts on the next page with the same header row as the table right before it is merged into it (first table's page and heading; chains join).

3. Write the parser output to `parsed/{document_id}.json` with `overwrite=True`, so a retry after a crash can rewrite it.
   Headings (Docling section headers/titles, Markdown `#` lines) set `section_heading` **and** are emitted as text, because the layout model sometimes labels body text as a header.
4. **Chunker** (`chunker.py`):
   - text items under the same heading are packed up to `CHUNK_MAX_TOKENS=512` tokens (`CHUNK_TOKENIZER`) (target ~400) and never cross a section boundary;
   - each table is its own chunk (`element_type = 'table'`), serialized as Markdown; tables longer than 512 tokens are split by rows, with the header row repeated;
   - `chunk_index` is 0-based and contiguous per document;
   - provenance ids (`P1-T2-R04` table rows, `P1-B03` text blocks, in document order) go in `ChunkPayload.rows` (`{id, cells}`) and `block_ids`, never in `text`; `rows` is `null` for tables split as plain text.
5. Delete the document's existing points (a retry may yield fewer chunks). Then, 256 chunks at a time (`INDEX_BATCH` in `jobs/handlers.py`, so only one batch of vectors is in memory), embed (dense from Ollama `qwen3-embedding` on raw chunk text, 32 texts per request; sparse BM25 locally), upsert into `doc_chunks`, and set `progress = {"stage": "embedding", "done", "total"}`. Finally set `status = 'indexed'` with `page_count` and `chunk_count`. File reads and writes run in `asyncio.to_thread`, because the worker shares the API's event loop. A document deleted meanwhile has its points and parsed JSON removed, and the job ends without retrying.

Any failure sets `status = 'failed'` and `error_code` (e.g. `parse_error`, `ocr_error`, `embed_error`). No document text is logged.

## 3. Retrieval (shared by Q&A and mapping)

`Retriever(embedder, store, reranker | None).search(ctx: TenantContext, query: str, document_ids: list[UUID] | None = None, k=20, top_n=8)` (`mapping/retriever.py`):
1. Embed the query: dense from Ollama with Qwen3's retrieval instruction prefixed, sparse BM25 from the raw query words.
2. `qdrant_store.hybrid_search`: prefetch `dense` limit 40 and `sparse` limit 40, both with the tenant filter, fused with RRF, limit `k`.
3. If `RERANKER_ENABLED`, rerank to top `top_n`; otherwise take the top `top_n` fused hits (Q&A: 8; mapping: see § 7).
4. Return a `Hit(chunk_id, document_id, filename, page_number, section_heading, element_type, text, score, chunk_index)` per result (`chunk_index` is `null` only for a point written before the payload carried it).

`Retriever.with_neighbors(ctx, hits, budget)` (mapping only) cuts `hits` to `budget`, then adds the chunk at `chunk_index - 1` of every hit (rank order), then the chunk at `+ 1` of every hit, until `budget` chunks (a value's heading usually sits in the chunk before it; cause: in the Phase 14 eval a value chunk made the cut without its heading and was read as the next section's); they are fetched by id with `qdrant_store.retrieve` under the tenant filter. The result is in reading order: documents by their best hit's rank, chunks by `chunk_index` (hits without `chunk_index` last). Nothing is fetched when the budget is spent.

`Retriever.table_chunks(ctx, document_ids)` (mapping only, `derived` slots, Phase 24) returns every `element_type = table` chunk of the documents (`qdrant_store.table_chunks`: tenant filter, all scroll pages, ordered by document and `chunk_index`; `[]` for no documents). `Evidence.row_id` (optional) is the provenance id (`P1-T2-R04`) of the table row a `derived` value's quote is the line of.

`Retriever.opening(ctx, document_ids, n=2)` (mapping only) fetches by id (`qdrant_store.retrieve`, tenant filter) the chunks `chunk_index` 0 to `n - 1` of each document, documents in the given order, chunks by index (title, author and date usually sit there and rarely match a slot's query); chunks that do not exist are skipped.

`select_context(hits, budget_tokens=None, pinned=0)` (`mapping/retriever.py`, used by Q&A and by mapping after `with_neighbors`; `budget_tokens` replaces `RETRIEVAL_CONTEXT_TOKENS` for a batch, § 7) keeps the hits in their order, drops a hit whose lowercased `\w+` word set has Jaccard >= 0.9 with a kept hit, and keeps the `format_hit` texts within `RETRIEVAL_CONTEXT_TOKENS` (counted with `CHUNK_TOKENIZER`, `curate`): the first `pinned` hits (`select_context(..., pinned=n)`: a batch's `Retriever.opening` chunks) claim the budget first, then scored hits in their order, then the 0.0-score `with_neighbors` chunks fill what is left; each pass stops at the first chunk that does not fit, the first claimed chunk is always kept, and the kept chunks stay in input order (Phase 19, cause: a finance run sent ~4.5k-token prompts where neighbor chunks crowded out ranked hits). It logs `retrieval` with chunk ids, scores (4 decimals), token total and count, never text, labels or queries.

`OllamaQwen3Embedder.embed_query` caches results in a per-instance LRU of 512 entries keyed by `(model, dimensions, text)`. Chunks are embedded as `filename > section_heading\ntext` (`embed_text`, `ingestion/chunker.py`; the section is omitted when `null`); the Qdrant payload keeps the raw chunk text, which quote verification uses. Ingestion also prefixes Docling list items with `- ` and drops text repeated on >= 3 pages and on at least half of the pages (running headers and footers).

`format_hit` (`mapping/retriever.py`) renders each hit for both Q&A and mapping; header fields are flattened to one line with `[`, `]`, `|` removed. For a table hit whose `rows` match its body lines, each line is prefixed `[<row id>] `; `LLMQuote` strips such ids from quotes so they still verify against the chunk text.

## 4. AnchorMap

Stage 1, deterministic, no LLM. Stored in `templates.anchor_map`.

```json
{
  "schema_version": "1.0",
  "format": "pptx",
  "anchors": [
    {
      "anchor_id": "pptx:s3:sp7",
      "kind": "shape_text",
      "path": ["Slide 3", "Situation Overview", "Content Placeholder 2"],
      "text": "Click to add situation summary",
      "tokens": [],
      "is_placeholder": true,
      "table": null,
      "pos": [50, 40],
      "context": "Situation summary",
      "card": null,
      "fit_chars": 110
    },
    {
      "anchor_id": "pptx:s5:sp4:r1c2",
      "kind": "table_cell",
      "path": ["Slide 5", "Stakeholders", "Table 3"],
      "text": "",
      "tokens": [],
      "is_placeholder": true,
      "table": {"row": 1, "col": 2, "header": "Owner"},
      "pos": [50, 70],
      "context": "Owner",
      "card": null,
      "fit_chars": null
    }
  ]
}
```

**`anchor_id` grammar.** Positional, stable for one immutable template file. Rows and columns are 0-based; slides and sections are 1-based.

| Format | Kinds | Pattern |
|---|---|---|
| pptx | `shape_text`, `table_cell`, `chart` | `pptx:s{slide}:sp{shape_id}[_{k}]`, `pptx:s{slide}:sp{shape_id}[_{k}]:r{row}c{col}` |
| docx | `paragraph`, `table_cell`, `header_paragraph`, `footer_paragraph`, `content_control` | `docx:body:p{i}`, `docx:body:t{t}:r{row}c{col}`, `docx:sec{s}:header:p{i}`, `docx:sec{s}:footer:p{i}`, `docx:sdt:{tag}` |
| xlsx | `sheet_cell` | `xlsx:{sheet_name}!{cell_ref}` (split on the **last** `!`) |

**Selection rules.** Indexes are positions in the library's native collection, not counts of emitted anchors, so writers resolve them directly: `docx:body:p{i}` is `document.paragraphs[i]`, `t{t}` is `document.tables[t]`, and pptx uses `shape.shape_id`. Paragraph indexes are 0-based. Real decks can repeat a `shape_id` on one slide, so pptx shapes are keyed by `(shape_id, kind)` in document order (`keyed_shapes()` in `anchors_pptx.py`, used by both the extractor and the writer): the first text shape and the first table with a given id are both `sp{shape_id}` (the writer tells them apart by `has_text_frame` vs `has_table`, and a chart's `has_chart` by the field's `fill_chart` mode; charts are a third kind, so adding one never shifts a text or table id), and the k-th (k ≥ 2) shape of the same kind with that id is `sp{shape_id}_{k}`.
- pptx: every shape with a text frame (group shapes are recursed) and every non-spanned table cell, except slide chrome and decoration: slide-number, footer, date and header placeholders without tokens, and empty non-placeholder text shapes (decorative boxes, icons, badges). Skipped shapes still count for `keyed_shapes()`, so keys never shift. An empty placeholder carries its layout prompt text as `text`. `pos` (pptx only, else `null`) is the shape's centre as `[x, y]` percent of the slide (table cells carry the table's), so the IR model can pair a placeholder with the heading next to it (`shortcut: group children use the group's child coordinates, transform via chOff/chExt if scaled groups mislead the IR`).
- pptx `chart` (Phase 18): every native category chart (`shape.has_chart`; XY scatter and bubble charts, charts without a plot and charts without a series are skipped, `shortcut: XY and bubble charts have no category axis, add them when a template needs numeric x values filled`). `text` is a summary for the IR model, at most 200 characters: `Chart BAR_CLUSTERED; title: …; series: A, B; categories: c1, c2, …`; `tokens` are the tokens of the title, category labels and series names; `is_placeholder` = tokens present; `chart` = `{"chart_type": XL_CHART_TYPE name, "series": [names], "categories": count}` (`ChartRef`, `null` for other anchors); `context` as for `shape_text`; no `card`.
- docx: headings are not anchors (they form `path`). A body paragraph is an anchor if it has text, or is empty directly under a heading. Table cells are deduplicated across merges (one anchor at the merge origin). Header and footer paragraphs with text count only for sections not linked to the previous one (first-page and even-page headers are skipped). Tagged `w:sdt` content controls in the body are anchors; untagged ones are skipped, and the first of a duplicate tag is kept. An inline content control overlaps its paragraph's anchor. Content controls around tables, rows or cells are not anchors, and a table whose rows all sit in row-level content controls has no anchors (its index still counts).
- xlsx: candidates are non-formula cells holding a token, plus empty right and below neighbours of text label cells. Merged non-origin cells are never anchors.

`is_placeholder` means unfilled: tokens present, own text empty, or a content control.

`context` is the static heading or label printed next to the anchor (a card title, a row label, a column header), found by code so repeated cards on one slide stay apart; `null` when none. Static text is a non-empty line of at most 120 characters with no token (whitespace collapsed). Rules (`templates/anchors.py` `static_text`, `cell_context`):
- pptx `shape_text`: the nearest static text shape on the same slide, never the anchor's own shape or the slide title. A heading above must start above the anchor, end above its vertical centre and overlap it horizontally; a label to the left must start left of it, end left of its horizontal centre and overlap it vertically. The smallest gap wins (ties go to above), and the gap may be at most 20% of the slide height (above) or width (left). Group children are walked like `keyed_shapes()` (`shortcut: group children use the group's child coordinates, like pos`). **Own label** (Phase 17, cause: six stage tiles `[# 1]` + "Requirement", `[# 2]` + "RFx", ... in one text box each got only the section heading as context): when the shape has tokens, its own label is the text around its tokens, if exactly one run of it has a letter, as static text with separators (`|:·-–—,;/` and spaces) stripped from both ends. It is appended to a neighbour context that has a letter (`Sourcing Pipeline / Requirement`, like a table's `row / column`), and replaces one that is absent or has no letter (a "01" or "2" marker box beside it). `Date [x] status` and `Risk: [A] | Action: [B]` (two runs) have no own label, so a card title stays their context.
- pptx and docx `table_cell`, body rows only: `row label / column header`, where the row label is the nearest static cell to the left in the same row and the header is row 0's text in that column if static; either part may be absent. In pptx the nearest heading above or left of the table (`_context`, as for shape text) is prefixed: `heading / row label / column header`, and the header row's cells carry the heading alone (Phase 19, cause: two side-by-side tables with equal headers under `LARGEST RECEIPTS` and `LARGEST PAYMENTS` got one label, query and prompt, so one took or starved the other's rows). Header cells have no context without a heading.
- docx `paragraph` that is a placeholder: the directly preceding non-empty body paragraph if it is static and not a heading (headings are already `path`); a table in between gives none.
- xlsx, header/footer paragraphs, content controls: none.

`card` (pptx only, Phase 17, else `null`) marks a slot inside a **repeating card**: `"{set}.{member}"`, e.g. `"1.3"` for the third card of the first set (`anchors_pptx._card_ids`). A shape's container is the smallest-area other shape on the slide (any type) whose box contains it with a strictly larger area, so a text box drawn exactly over a pill or marker of the same size is not inside it. A container that holds 2 or more anchor shapes (static ones count) is a card candidate, and candidates whose width and height are equal within 2% form a set; only a set of 2 or more is a card set. Sets are numbered in reading order of their first member, members in reading order (rows by top, a new row when a top is half a member height below the row's first, then left to right). Every anchor in a member gets its id, table cells included. A single panel or two panels of different sizes get none. `shortcut: tiles drawn without a container shape are not detected, group them by position if real decks need it.`

`fit_chars` (pptx `shape_text` only, else `null`; `Anchor.fit_chars`, at least 1) is a soft estimate of how many characters fit one token of the box (`anchors_pptx._fit_chars`): the box's width and height minus its text-frame insets (python-pptx defaults 0.1 in left/right, 0.05 in top/bottom), font size = the largest explicit run size in the shape, else 18 pt, `floor((width / (0.55 * size)) * (height / (1.2 * size)))` characters, minus the length of the box's static text (own text with the tokens removed, whitespace collapsed), divided by its distinct token count (at least 1). The 0.55 and 1.2 constants are shared with the writer's `_fit` (`GLYPH_EM`, `LINE_EM`). Only shapes with tokens or an empty placeholder get it; `null` when geometry is missing or the result is below 1. `shortcut: inherited font sizes are not read (18 pt), and table cells get no fit_chars; read the master or the column width if estimates are visibly off.`

`tokens` lists placeholder tokens found in the text: `{{…}}`, `[…]` (any non-space first character and at least one letter or digit inside, so `[# 1]`, `[$ 0.0]` and `[0.0]` are tokens; a bracket holding only digits, such as a `[1]` footnote marker, or only symbols, such as the unit in `Cost [€]` or `[%]`, is not; Phase 17), `<…>` (starts with a letter), `XXX` and `TBD` as whole words, and blanks of 4+ underscores (`____`). Each token is at most 100 characters inside its brackets.

**Limits.** `MAX_ANCHORS = 400`: more fails extraction with `validation_error`, never truncated; zero anchors also fails with `validation_error`. Before parsing, a package with more than 5000 zip parts, any part over 32 MB declared (whatever its name: `[Content_Types].xml` decides what is parsed as XML), or over 128 MB declared in total, fails with `validation_error`. An unreadable package fails with `parse_error`.

## 5. TemplateIR

Stage 2. The LLM proposes fields from the AnchorMap (prompt `llm/prompts/template_ir.md`). The prompt makes it skip visual and non-data slots (logos, icons, images), slide numbers and template-author instructions; code backs it up: `_repair` drops any `replace_token` field whose token is a picture slot (`[Company Logo 1]`, `[Logo]`, `[Client logo placeholder]`, `[Image 2]`, `{{photo}}`; `ir_builder._is_picture`, while `[Image caption]` stays text) and `_token_cells` never adds one, so the token stays in the output for the author's image (Phase 19 live test, cause: `[Company Logo 1]` rendered `[MISSING: Company Logo 1]`). Charts are data (Phase 18): a `chart` anchor with placeholder or sample data is one `fill_chart` field, and a `shape_text` anchor whose whole text asks for a chart (a chart token, an insert-a-chart instruction in any language, an empty chart placeholder's prompt) is one `insert_chart` field, both `expected_type` `chart`. The LLM also sets `mode` (Phase 19, cause: a finance run left summary, trend and net-movement slots `missing` because no chunk states them verbatim): `synthesis` for a slot whose value is written (summary, trend, pattern, observation, commentary, finding) or computed from two printed figures (a net movement), `derived` (Phase 24) for an aggregate over a table's rows that no quote states (a total, net movement, count, category breakdown; computed in code, see Derived slots in § 7; the IR builder sets only the mode, `derivation` is bound at mapping time), else `extractive` (names, ids, printed amounts, dates, periods, list and table items). `mode` is required, with no default and a description of both values, in the schema the IR model is given and validated by (`ir_builder.BatchReply` / `LLMIRField`; a reply without it gets the usual corrective retry), because with a default the model set it on none of the 116 fields of a finance template; an IR stored without `mode` reads as `extractive`. `IR_BUILDER_VERSION` 4 (Phase 21: code-made token fields for every anchor kind). Each `field_id`, `label` and `description` must name the slide topic and the nearest static heading (found via `pos`), or the exact ordinal for untitled ordered slots, never a generic "Value 1". `date` is only for a full calendar date; a month-and-year or quarter slot is `text`, so the source text is copied verbatim and no day is ever invented. `ir_builder.validate()` enforces:
- every `anchor_id` exists in the AnchorMap;
- no `anchor_id` is used twice, except by `replace_token` fields: an anchor with several tokens (e.g. `[Presenter Name], [Role] · [DD Month YYYY]`) gets one `replace_token` field per token, each `(anchor_id, constraints.token)` pair unique, and no other `write_mode` on that anchor;
- `field_id`s are unique;
- `write_mode` is allowed for the anchor kind (table below);
- `replace_token` fields have `constraints.token` present in the anchor's `tokens`;
- `fill_table_rows` anchors on table row 1, col 0;
- `expected_type` `chart` goes with `fill_chart`/`insert_chart` and nothing else, both ways (the token rule below does not apply to them: the whole chart or placeholder shape is replaced);
- a table filled by `fill_table_rows` has no other field on its cells;
- a `replace_text` or `fill_cell` field on a text anchor whose anchor has `tokens` uses `replace_token`, unless the anchor text is exactly one token (so the text around a token is never deleted). xlsx `sheet_cell` is exempt.

The AnchorMap is sent in **batches**, not one prompt: a whole AnchorMap can outgrow `LLM_NUM_CTX`, and Ollama silently drops the start of an over-long prompt, where the rules are. Measured: a 10-slide deck gave 212 anchors and a ~12.6k-token prompt. Batches are built like this:
- anchors are grouped by slide (pptx), by sheet (xlsx) or as one group (docx);
- a table's cells stay together;
- each batch holds at most `MAX_BATCH_ANCHORS = 40` anchors and at most `MAX_BATCH_CHARS = 12,000` characters of compact JSON (~3.5k tokens; `ir_builder.py`), and never mixes groups. A table larger than either cap is split in row order.

Each anchor's `context` (§ 4) is sent with it, and after the merged IR validates, every field's `context`, `card` and `fit_chars` are set to its anchor's by code (`IRField.fit_chars` is a soft length hint for the extraction prompt, hidden from the LLM schema like `card`, never copied into `constraints.max_chars` and never a warning or a constraint; it is not sent to the IR LLM) (both are hidden from the LLM's JSON schema, `SkipJsonSchema`, so the model never writes them; an IR stored without them stays valid; `card` is not sent to the LLM). Batches run sequentially, and each is validated against its own anchors only. Cells of a table already owned by a `fill_table_rows` field from an earlier batch are not sent again. The fields are then merged in order. A `field_id` already used by an earlier batch is renamed `{id[:60]}_{n}` with the smallest free `n ≥ 2`, and the merged IR is validated once more against the full AnchorMap. A `replace_token` field whose token starts with `#` (`[# 1]`, `[# items]`, `{{# n}}`) is a count, so code sets its `expected_type` to `number` whatever the LLM chose (Phase 18 live test, cause: `[# 1]`..`[# 6]` were typed `text` and got a description of the slide). **Tile captions** (cause: a KPI tile `[Value 1]` + `[Total spend]` typed currency got the measure's name "Total addressable spend" as the caption, which fails currency conversion, so the slot ended missing): after the merge, code groups `replace_token` fields of anchors with a `card` by (slide, card); a `number`, `currency` or `percent` field whose token (inner text, `{}[]<> ` stripped) has no trailing ordinal (`[\s_#-]*(?<!\d)\d{1,3}$`, so `FY2025` has none) is retyped `text` when another field of the same card has a token with one. A card with no numbered token (`[Revenue]` + `[Growth]`) and a `#` count token are untouched; the prompt says the same in one line. A `date` field whose token spells a month and a year but no day (`[Month YYYY]`, `[Mon YYYY]`, `[MM/YYYY]`, `[Mes AAAA]`; a `D`, `DD`, `Day` or `Día` keeps it a date) becomes `text`, so the source's `November 2026` is copied verbatim and never given a day (Phase 18 live test 3, cause: the IR model typed `[Month YYYY 1..3]` `date` despite the prompt, and a day-less date never verifies). **Empty tables:** after the merge, each native table (pptx/docx `table_cell` anchors) that no field touches, whose header row names every column (each has a letter) and whose body cells are all empty, gets a code-made field `table_rows_{n}` (smallest free `n`): `fill_table_rows` on its `r1c0` anchor, `expected_type` `table`, `required` false, `label` the headers joined by ` / `, `constraints.columns` the headers, `max_items` its body row count (Phase 18 live test 3, cause: the IR model skipped the empty Action / Owner / Due table on one run, so nothing filled it). A template of nothing but such a table is therefore valid. **Label/value grids and token cells** (Phase 19, cause: bracket tokens in a reconciliation table's cells stayed literal): a native table whose every body row is a static label in column 0 (no token, has a letter) plus at least one token cell is a label/value grid, and `_repair` drops any `fill_table_rows` field on it (so per-batch validation sees the repaired draft; removing a field never fabricates one). After the merge, every `(anchor_id, token)` of an anchor whose kind allows `replace_token` (`WRITE_MODE_KINDS`; an xlsx `sheet_cell` does not) that no field covers (no `replace_token` field with that token, no other-mode field on the anchor, table not owned by a `fill_table_rows` field) gets a code-made `replace_token` field; outside a `table_cell` only a named token (`{{x}}`, `[x]`, `<x>`) does, since a bare blank in prose (`status TBD`, `Signature ____`) may be the author's own text (Phase 21, cause: a slide repeated `[Opening Balance]` in a grid of text boxes, not a native table, the IR model skipped the copies and they stayed literal): when a `replace_token` field with the same token sits in the same slide or sheet (docx: the document) it copies that field's `label`, `description`, `expected_type` and `mode`, otherwise `expected_type` `text`, `label` the anchor's context (else the token's inner text); `required` false, and a `field_id` slugged from the inner text (`^[a-z][a-z0-9_]{0,63}$`, numeric suffix on collision, `cell` when the slug is empty). For a `fill_chart` field, code sets `constraints.columns` to the chart's series names (the LLM's value is overwritten); an `insert_chart` field keeps the LLM's series names, defaulting to `[label]`.

**Safe repair** (`ir_builder._repair`, cause: a real deck whose slide 2 "table" was drawn with separate `shape_text` boxes failed `ir_invalid` twice). Before validation, every LLM reply (first and retry) gets mechanical slips dropped, in this order:
1. `fill_table_rows` on an anchor that is not a `table_cell`;
2. `replace_token` on an anchor with no `tokens` that is not a placeholder (a static label);
3. exact duplicates of an earlier field's `(anchor_id, write_mode, constraints.token)`; the first one is kept;
4. any field on a `table_cell` anchor in row 0 that has static text and no `tokens`: a header label is read-only (a header with a token, and an empty row-0 cell of a headerless key/value table, stay fillable; `fill_table_rows` sits at `r1c0`).

Before step 3, a `replace_token` token that is not one of its anchor's tokens is replaced by the anchor's own token when exactly one matches it with surrounding brackets (`[]{}<>`) and whitespace stripped, inner whitespace collapsed and case folded (Phase 19 live test, cause: `mlx-community/Qwen3-14B-4bit` returned `[Strategic supplier scorecard – period]` as `Strategic supplier scorecard – period`, the retry repeated it and the procurement template failed `ir_invalid` at slide 6). No match or several matches stay a validation error.

Repair only removes fields or swaps in a token the anchor already has, so it can never fabricate one. Dropped fields leave their anchors as template text, and the count is logged as `ir_repaired` (`dropped`, `anchor_count`). Everything else stays a validation error and goes through the retry below. That includes invented anchors, unknown tokens on tokened anchors, `replace_token` on an empty placeholder, and mixed write modes on one anchor. The prompt also says that a table drawn with `shape_text` boxes is not a table: each fillable box is its own `shape_text` field, and its static label boxes are skipped.

Each invalid batch gets 1 LLM retry: the original prompt, the previous draft echoed back as the assistant turn (cut to `RETRY_ECHO_CHARS` = 1,500 characters so the retry still fits `LLM_NUM_CTX`), and the validation errors. If that retry also fails, the `extract_template` job fails with `ir_invalid`, and the errors are logged as `ir_invalid` (`errors`, `anchor_count`). The errors name only field ids, anchor ids, write modes, anchor kinds and counts, never document or token text. Job progress before each batch is `{"stage": "ir", "done": i, "total": n, "part": "slide 3"}`; `part` is `slide N` or `sheet N` (the sheet's position, never its name), and absent for docx.

| `write_mode` | Allowed anchor kinds | Behavior |
|---|---|---|
| `replace_text` | `shape_text`, `paragraph`, `header_paragraph`, `footer_paragraph`, `content_control`, `table_cell` | replace all text in the anchor |
| `replace_token` | any text kind (not `sheet_cell`) with non-empty `tokens` | replace `constraints.token` only |
| `fill_cell` | `table_cell`, `sheet_cell` | write one cell value (typed for xlsx) |
| `fill_table_rows` | `table_cell` at row 1, col 0 (the first body cell of a table) | write rows into **existing** empty body rows |
| `fill_chart` | `chart` (pptx) | replace the native chart's data (`chart.replace_data`), styling and type kept |
| `insert_chart` | `shape_text` (pptx) | add a native chart at the placeholder shape's box and remove the placeholder |

`sheet_cell` allows only `fill_cell`.

**JSON Schema** (`templates/schemas.py` mirrors it as Pydantic v2 models):

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "TemplateIR",
  "type": "object",
  "required": ["schema_version", "template_id", "version", "format", "fields"],
  "additionalProperties": false,
  "properties": {
    "schema_version": {"const": "1.0"},
    "template_id": {"type": "string", "format": "uuid"},
    "version": {"type": "integer", "minimum": 1},
    "format": {"enum": ["pptx", "docx", "xlsx"]},
    "fields": {
      "type": "array",
      "minItems": 1,
      "items": {
        "type": "object",
        "required": ["field_id", "anchor_id", "section_path", "label", "description",
                     "expected_type", "required", "write_mode", "constraints"],
        "additionalProperties": false,
        "properties": {
          "field_id": {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,63}$"},
          "anchor_id": {"type": "string"},
          "section_path": {"type": "array", "items": {"type": "string"}},
          "label": {"type": "string", "minLength": 1, "maxLength": 120},
          "description": {"type": "string", "maxLength": 500},
          "expected_type": {"enum": ["text", "paragraphs", "bullet_list", "number",
                                     "currency", "percent", "date", "table", "chart"]},
          "mode": {"enum": ["extractive", "synthesis", "derived"], "default": "extractive"},
          "derivation": {
            "type": ["object", "null"],
            "description": "derived only; not in the IR model's schema (SkipJsonSchema)",
            "additionalProperties": false,
            "required": ["op", "column"],
            "properties": {
              "op": {"enum": ["sum", "diff", "count", "min", "max", "group_sum", "top"]},
              "column": {"type": "string"},
              "minus": {"type": ["string", "null"]},
              "where": {"type": "object", "additionalProperties": {"type": "string"}},
              "tag": {"enum": ["CR", "DR", null]},
              "group_by": {"type": ["string", "null"]},
              "group_sep": {"type": ["string", "null"]},
              "top_n": {"type": ["integer", "null"], "minimum": 1},
              "rank": {"type": ["integer", "null"], "minimum": 1},
              "pick": {"type": ["string", "null"]},
              "exclude_rows": {"type": "array", "items": {"type": "string"}}
            }
          },
          "required": {"type": "boolean"},
          "write_mode": {"enum": ["replace_text", "replace_token", "fill_cell", "fill_table_rows",
                                  "fill_chart", "insert_chart"]},
          "constraints": {
            "type": "object",
            "additionalProperties": false,
            "properties": {
              "max_chars": {"type": "integer", "minimum": 1},
              "min_items": {"type": "integer", "minimum": 0},
              "max_items": {"type": "integer", "minimum": 1},
              "date_format": {"type": "string"},
              "unit": {"type": "string"},
              "allowed_values": {"type": "array", "items": {"type": "string"}},
              "token": {"type": "string"},
              "columns": {"type": "array", "items": {"type": "string"}}
            }
          },
          "context": {"type": ["string", "null"]},
          "card": {"type": ["string", "null"]},
          "fit_chars": {"type": ["integer", "null"], "minimum": 1}
        }
      }
    }
  }
}
```

Example:

```json
{
  "schema_version": "1.0",
  "template_id": "3f6c1a0e-8b1d-4b55-9a57-2f1c0d7e9b10",
  "version": 1,
  "format": "pptx",
  "fields": [
    {
      "field_id": "incident_summary",
      "anchor_id": "pptx:s3:sp7",
      "section_path": ["Situation Overview"],
      "label": "Incident summary",
      "description": "Two to three sentence summary of what happened, when, and where.",
      "expected_type": "paragraphs",
      "required": true,
      "write_mode": "replace_text",
      "constraints": {"max_chars": 600}
    },
    {
      "field_id": "affected_customers",
      "anchor_id": "pptx:s4:sp9",
      "section_path": ["Impact"],
      "label": "Affected customers",
      "description": "Number of customers affected by the incident.",
      "expected_type": "number",
      "required": true,
      "write_mode": "replace_token",
      "constraints": {"token": "{{affected_customers}}"}
    }
  ]
}
```

### 5.1 `templates/` folder sync

`TEMPLATES_DIR` is the library's source of truth (ADR-017). `library.sync_templates_dir` makes `library_templates` match it:

1. **Discovery.** Regular files directly in the folder (not recursive) with a `pptx`, `docx` or `xlsx` suffix; dotfiles and `~$` lock files are ignored. Each is hashed. A missing folder logs a warning and ends the sync: a wrong path never prunes the library.
2. **Plan** (`plan_sync`, pure). Rows match files by `filename`. A file with no row is added; a row whose `sha256` or `ir_builder_version` differs from the file / `IR_BUILDER_VERSION` is updated; a row with no file is pruned; an unchanged row is kept. A file whose bytes an earlier file or a kept row of another name already owns is skipped (`sha256` is UNIQUE).
3. **Execute**, prune first (it frees names and hashes for renamed files), then adds, then updates. A new row is named after the file stem; an updated row keeps its name, description and category. An update builds the AnchorMap and IR first, then deletes the old row and inserts the new one in one transaction (`store(replacing=...)`), and removes the old blob after the commit, so a failed rebuild leaves the old row. A failure on one file is logged as `template_sync_failed` (error type and code only) and the others continue. One `template_sync` event logs the counts.
4. **Concurrency and triggers.** A single `asyncio.Lock` (`app.state.template_lock`) serializes the sync and upload scans. The sync starts as a background task at startup and on `POST /v1/templates/sync`; progress appears as scans in `GET /v1/library/templates`.

`IR_BUILDER_VERSION` (`templates/ir_builder.py`, now 7) is bumped when `build_ir` output changes, so the next sync re-extracts every template.

**Code-forced `derived`** (Phase 25, ADR-019, end of `build_ir`): a `number` or `currency` field whose label or id holds a net-movement term (`_NET_TERMS`, EN/ES) and every field in `_ranking_slots` (a numbered slot whose label, id, section path or context holds a ranking or breakdown word, and whose numbered group holds a number or currency slot) get `mode` `derived`, whatever the LLM said. `shortcut: English/Spanish term and word lists, add words per language.`

**Direction rules and `derivation_intent`** (Phase 26, version 7, same place in `build_ir`): one EN/ES direction vocabulary (`_IN_WORDS`: deposit, credit, receipt, inflow, income, "money in", ingreso, abono, cobro, ...; `_OUT_WORDS`: withdrawal, debit, payment, outflow, expense, "money out", retiro, cargo, pago, gasto, ...) is matched with `_padded` over label, field id and context (and the section path for a numbered slot); a name hitting both or neither has no direction; non-direction phrases are removed first (`_NOT_DIRECTION`: credit card, credit limit, available credit, credit line, debit card, payment due, minimum payment, income statement, tarjeta/limite de credito), and the Spanish OUT word is the plural `cargos` only (bare `cargo` is freight). The context counts only for a bare label (label empty after removing amount, total words and numbers: "Value", "Total"), so a cell under a "NET CASH FLOW" heading is not the net; a total slot whose label or id holds a count word (number, count, cantidad, numero) is not a total; a numbered copy (`total_deposits_2`) gets the same intent as the original. Also forced to `derived` (`number`/`currency` only): a total slot (a `total`/`suma` word and a direction: "TOTAL MONEY IN", "Total Withdrawals"), an un-numbered largest slot (a `largest`/`biggest`/`highest`/`top`/`mayor` word and a direction: "Largest Credit Amount"), and a net-movement slot named in its context too. In a numbered ranking grid a slot whose label or id names an amount word (amount, value, total, importe, monto, valor) but is typed `text` is re-typed `currency` first only when its names or section path also hold a category/breakdown word ("Value N" under "OUR TOP VALUES" stays text) (`_retype_amounts`), so `_ranking_slots` sees the numeric slot and the whole category grid (`Category N` names stay `text`) becomes `derived`. `ir_builder.derivation_intent(field) -> (op, tag) | None` is the deterministic meaning of such a slot: net -> `("diff", None)`; total + direction -> `("sum", CR|DR)`; numbered category/breakdown -> `("group_sum", direction tag or None)`; numbered or singleton largest -> `("top", direction tag)`; anything else None. It is code, not stored in the IR.

## 6. FieldMappingResult

Stage 3 output, one per field, stored in `mapping_runs.results[field_id]`.

**JSON Schema** (`mapping/schemas.py` mirrors it; the invariants in `allOf` are enforced by a Pydantic `model_validator`):

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "FieldMappingResult",
  "type": "object",
  "required": ["field_id", "status", "value", "evidence", "confidence", "missing_reason",
               "checked_documents", "conflict_details", "provenance"],
  "additionalProperties": false,
  "$defs": {
    "Evidence": {
      "type": "object",
      "required": ["document_id", "chunk_id", "filename", "page_number", "section_heading", "quote"],
      "additionalProperties": false,
      "properties": {
        "document_id": {"type": "string", "format": "uuid"},
        "chunk_id": {"type": "string", "format": "uuid"},
        "filename": {"type": "string"},
        "page_number": {"type": ["integer", "null"]},
        "section_heading": {"type": ["string", "null"]},
        "quote": {"type": "string", "minLength": 8, "maxLength": 1000},
        "item": {"type": ["integer", "null"], "minimum": 0},
        "row_id": {"type": ["string", "null"]}
      }
    },
    "Value": {"type": ["string", "number", "array", "null"]}
  },
  "properties": {
    "field_id": {"type": "string"},
    "status": {"enum": ["populated", "partial", "missing", "conflict"]},
    "value": {"$ref": "#/$defs/Value"},
    "evidence": {"type": "array", "items": {"$ref": "#/$defs/Evidence"}},
    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    "missing_reason": {
      "enum": [null, "no_relevant_chunks", "not_found_by_llm", "evidence_failed_verification",
               "insufficient_evidence", "llm_error", "cleared_by_user"]
    },
    "checked_documents": {"type": "array", "items": {"type": "string", "format": "uuid"}},
    "conflict_details": {
      "type": ["object", "null"],
      "required": ["candidates"],
      "properties": {
        "candidates": {
          "type": "array",
          "minItems": 2,
          "items": {
            "type": "object",
            "required": ["value", "evidence"],
            "properties": {
              "value": {"$ref": "#/$defs/Value"},
              "evidence": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/Evidence"}}
            }
          }
        }
      }
    },
    "provenance": {"enum": ["rag", "user"]}
  },
  "allOf": [
    {"if": {"properties": {"status": {"const": "populated"}, "provenance": {"const": "rag"}}},
     "then": {"properties": {"evidence": {"minItems": 1}, "value": {"not": {"type": "null"}}}}},
    {"if": {"properties": {"status": {"const": "partial"}}},
     "then": {"properties": {"evidence": {"minItems": 1}, "value": {"not": {"type": "null"}}}}},
    {"if": {"properties": {"status": {"const": "missing"}}},
     "then": {"properties": {"value": {"type": "null"}, "evidence": {"maxItems": 0},
                             "missing_reason": {"type": "string"}}}},
    {"if": {"properties": {"status": {"const": "conflict"}}},
     "then": {"properties": {"value": {"type": "null"}, "conflict_details": {"type": "object"}}}}
  ]
}
```

Example (conflict):

```json
{
  "field_id": "affected_customers",
  "status": "conflict",
  "value": null,
  "evidence": [],
  "confidence": 0.0,
  "missing_reason": null,
  "checked_documents": ["a1f0…", "b2e1…"],
  "conflict_details": {
    "candidates": [
      {"value": 12000, "evidence": [{"document_id": "a1f0…", "chunk_id": "c9…", "filename": "ops_report.pdf",
        "page_number": 4, "section_heading": "Impact", "quote": "approximately 12,000 customers were affected"}]},
      {"value": 15400, "evidence": [{"document_id": "b2e1…", "chunk_id": "d7…", "filename": "press_release.docx",
        "page_number": null, "section_heading": "Statement", "quote": "15,400 customers experienced disruption"}]}
    ]
  },
  "provenance": "rag"
}
```

**`confidence`** is a deterministic tier for display only; it never drives `status`:
- `populated` + typed value matched literally in a quote → `0.9`
- `populated` text, list or table whose every item (cell) appears in a verified quote → `0.8`
- `populated` list synthesized from verified quotes (step 5 below) → `0.7`
- `partial` → `0.5`
- `missing` / `conflict` → `0.0`
- `provenance = user` → `1.0`

Engine results always have `provenance = rag`.

Calibration (Phase 12, eval set of 5 templates, 39 fields, `qwen3:14b`): tier 0.9 was correct for 12 of 12 values and tier 0.8 for 9 of 9. No `partial` rows occurred. The tiers stay as they are: the order holds and no tier over-promises. `shortcut: calibrated on 21 values; recalibrate once the eval set or a live run gives partial rows or a wrong 0.9 value.`

## 7. Mapping engine (Stage 3)

### Run state machine (`mapping_runs.status`)

```
queued ─▶ retrieving ─▶ extracting ─▶ verifying ─▶ rendering ─▶ completed
   │           │             │             │            │
   └───────────┴─────────────┴─────────────┴────────────┴──▶ failed
```

`run_mapping` sets `extracting` when the field loop starts, then commits `results`, `summary` and `verifying` in one transaction. It then renders: it sets `rendering` (commit), writes the file, and commits the `artifacts` row together with `completed`, `finished_at` and the writer warnings appended to `summary.warnings`. `retrieving` is not used (each field is retrieved, extracted and verified in turn). A retried job whose run is `verifying`/`rendering` only renders; a `completed` run returns its artifact. `render_artifact` (payload `{mapping_run_id}`) runs the same render for a `verifying`/`rendering` run (Phase 11 edits enqueue it). Failure hooks: `run_mapping` marks any run that is not `completed`/`failed` as `failed`; `render_artifact` only fails `verifying`/`rendering` runs. `finished_at` is set at `completed` or `failed`.

Fields are mapped in IR order (`engine.map_fields`). Fields that ask for the same fact share one `share_key` `(fact name, expected_type, context)` and are mapped once per run: later ones reuse the first result under their own `field_id`, so every copy gets the same value at the cost of one retrieval and one LLM call. The fact name is a named placeholder's inner text (`{{…}}`, `[…]`, `<…>`) for a `replace_token` field, label ignored: a named placeholder is one variable, as in mail merge, and `ir_builder` labels each slide differently. Generic blanks (`____`, `XXX`, `TBD`) repeat with different meanings and tokenless fields have no name, so for them the fact name is the label. Fact names are compared after `normalize`, with `_` and `-` read as spaces (`{{opening_balance}}` and `[Opening Balance]` are one fact). Copies are shared only across units (a pptx slide, an xlsx sheet, a docx heading section; a docx table cell counts as the section above its table): a key that appears twice in one unit is a repeating slot (three risk cards with `[Risk]`), so none of its fields are shared (`shared_facts`). The normalized `context` must be equal too: one token under two different card headings (or row labels) is two facts, while copies with equal (or no) context share. A template that reuses one named placeholder for different facts on different slides, with no heading to tell them apart, gets one value in every copy.

**Row groups** (`engine.row_groups`, `map_group`; prompts `llm/prompts/slot_records.md` and `table_rows.md`; cause: grid templates mapped cell by cell got conflicts or cells from different source records in one row; Phase 17: repeating cards and numbered lists mapped slot by slot gave `missing`/`conflict` on sibling slots and duplicate sentences). The run loop is `engine.map_fields` (`map_run` calls it). It maps these groups with one retrieval and one structured call each, at the position of the group's earliest field in the IR; group fields are never shared facts. A field joins one group, in this precedence:
- **Tiles** (Phase 17): fields with a `card` (§ 4), not `table`, bucketed by unit and card set; each member with 2 or more fields is a row (fields in IR order; rows may differ in length), and a bucket with 2 or more rows is a group. A member with one field stays loose. **Keyed cards** (Phase 18 live test, cause: four risk cards each under their own static heading went into one records call and only card 1 came back) map one card at a time: when every row has a context with a letter and they all differ, each card with 2 or more fields is its own one-row group (one records call per card, so its slots come from one source row, with the row check against sibling headings), a card with one field stays loose, and no later grid takes them.
- **Numbered slot grid:** in one unit, `replace_token` fields whose named placeholder ends in a number (`[Date 1] [Item 1] … [Date 3] [Item 3]`; `^(.*?\S)[\s_#-]*(\d{1,3})$`, stem normalized like `share_key`). Stems that each occur once per number and share the number set `{1..N}`, with N >= 2 and >= 2 stems, form N rows (columns in IR order). A gap, a repeated token or a 4-digit number is not a grid.
- **Numbered list** (Phase 17): a single stem numbered `{1..N}` (N >= 2) that shares its number set with no other stem (`[Key message 1] [Key message 2] [Key message 3]`), when the stem has a letter (a symbol-only stem such as `[# 1]..[# 6]` names no fact: those slots are keyed by their own labels and stay loose) and the slots' contexts that have a letter are all equal (none, or letterless ones such as a "2" marker, are ignored). N rows of one field.
- **Native cell grid:** `fill_cell`/`replace_text` fields on cells (`…:r<R>c<C>`) of one table, body rows only, consecutive rows (>= 2) that each have a field in every column `0..K` (>= 2 columns). A table with a static label column is not a grid: its rows are keyed by the template, not by source order.
- **`fill_table_rows`** (`expected_type` `table`): its own group, columns from `constraints.columns`, at most `constraints.max_items` rows.
- **Charts** (Phase 18, `expected_type` `chart`): its own group, never a tile or shared fact.

Tiles, grids, lists and cell grids use the **records call** (`extractor.extract_records`, `slot_records.md`). The spec is `{"records": [{"n", "context"?, "slots": [{"label", "description", "expected_type", "placeholder"?}]}], "section_path", "context"?}` (fields bounded as in step 1; `placeholder` is a token's inner text). A record's context is the first context with a letter among its fields; when the records' contexts differ (and the group is not a list) each record carries its own (**keyed records**: record `n` is the entity its context names), otherwise one shared context is sent on top and the records are distinct items in source order, except that a record whose own slots name a different item than the others (not only a different number, e.g. one KPI card per measure whose label slot is `[Total spend]`) is that item. The model replies `{"records": [{"n", "value": [{"slot": <label>, "value": <string>}, ...one per slot], "quotes": [{chunk_id, quote}]}], "not_found"}` (every value is bound to its slot label; when each spec label occurs exactly once among the reply's `slot`s, `extract_records` reorders the cells to spec order by normalized label, otherwise the reply order is kept), one entry per record the chunks state; skipped records are left out. Row `i` gets the record with `n = i + 1` (the first of a repeated `n` wins, out-of-range `n` is dropped), so a skipped record never shifts the others into the wrong card. `fill_table_rows` keeps the row call (`table_rows.md`, `{"rows": [{"value": [{"slot": <column label>, "value"}], "quotes"}], "not_found"}`, one row per source record in source order, cells realigned to column order by label as for records). Both specs carry `other_headings` (Phase 19; `sibling_headings` of the group's first field) when there are any, and both prompts exclude rows of the opposite direction or side the table's `context` names and rows that belong under a sibling heading. **Row check** (`extractor.check_rows`, `row_check.md`, strict `LLMRowChecks` `{"checks": [{"about", "belongs": "this"|"other"|"unknown"}]}`; Phase 19, cause: an incoming credit row appeared in an outgoing table): after the row call, when a `fill_table_rows` table has a context with a letter (or a records group has one and sibling headings; a keyed card is one such group), one more call gets the context, `other_headings` and each row's cells and quotes. Only rows judged `other` are dropped; `unknown` keeps the row, a reply without one verdict per row or `LLMInvalidOutput` keeps every row, and when every row is dropped the group is `missing(not_found_by_llm)`. Chart rows are not checked.

Each record's or row's quotes are verified as in step 3; a cell is supported by a quote when its normalized text is in it (typed cells: canonical value in the quote, as `value_in_quotes`; a `paragraphs`/`bullet_list` slot gets a one-item list). A `fill_cell` slot typed `number`, `currency` or `percent` (never `date`: it is stored ISO and an xlsx date cell is a serial) whose cell is a word with no digit (`Preferred` under a Rating column) is checked and kept as verbatim text (`engine._cell_types`; the writer writes such a string as is): a column's type is the IR model's guess (Phase 19 live test, cause: all five supplier ratings were `missing` and that unsupported cell made their 25 correct row mates `partial`). A card slot keeps its type. A record's **spans** are its verified quotes plus each line of a chunk it cites that states 2 or more of its cells (Phase 18 live test, cause: a small model copied record 1's quote into every record, so records 2..N failed verification; the line is verbatim chunk text and every cell is still checked against it). Its **anchor** is the span that states its first non-empty cell (its key), most cells first. A cell is **shifted** when its anchor does not state it and every span that does overlaps (same chunk, one normalized quote inside the other) another record's anchor (Phase 18 live test, cause: KPI tiles got the next tile's value in their second slot); a shifted cell is `missing(evidence_failed_verification)` and does not affect its row. A later cell whose value (after `normalize`) equals a kept cell of the same record is `missing(evidence_failed_verification)` too and does not affect its row (Phase 18 live test 3, cause: a KPI tile's caption slot repeated the tile's own `4.6`). A card's `text` slot whose value is only a kept typed sibling's number with at most one unit or currency word (`AED 182.4M`, `4.6 days`, `91%`, never `Top 5 suppliers`; `engine._restates`) is a caption slip: `missing(evidence_failed_verification)`, its row unaffected; `slot_records.md` asks for the measure's name as the source writes it (Phase 19 live test, cause: the four KPI captions repeated their values). A record is **anchored** when its anchor states every other non-empty cell. Per slot: supported cell of an anchored record -> `populated` with the spans stating its kept cells (anchor first); supported cell of a record that is not anchored (a cell stated only by another span, or an unsupported row mate) -> `partial` (`insufficient_evidence`); empty cell or no record -> `missing(not_found_by_llm)`; unsupported cell -> `missing(evidence_failed_verification)`. A `fill_table_rows` value keeps only rows whose every non-empty cell is supported (unverified rows stay out); it is `populated` when every row was kept and anchored, else `partial`, and `missing` when none is kept. Inside a group, rows are records, so there are no conflicts: two sources that state one record differently give two rows. `not_found`, no hits and invalid output make every field of the group `missing` (`not_found_by_llm`, `no_relevant_chunks`, `llm_error`). A records reply with fewer in-range `n` than rows, or an empty row reply, logs `group_short` with counts only (`kind` records/rows, `rows`, `returned`, `hits`, `not_found`).

**Lists** (Phase 17, `engine._list_results`): duplicate values (after `normalize`) collapse and the distinct items fill slots 1..N in order, the rest `missing(not_found_by_llm)`. If that leaves a slot unpopulated, the records call returned at least one record (absent facts stay missing: never after `not_found`, except for prose slots, below) and no slot is typed, **grounded synthesis** (step 5) runs once on the same hits for a `bullet_list` of at most N items (`max_chars` = the slots' sum when every slot has one; `shortcut: items are not cut to their own slot's max_chars, summarize warns`). Supported, distinct item k fills slot k as `populated`, confidence `0.7`, with that item's verified quotes (`item` = 0, so the chat summary names it as summarised); the synthesis is used only when it populates more slots than the verbatim records, and invalid output keeps the records result. A list whose first slot is sized for prose (`engine._prose`: `fit_chars`, else `max_chars`, at least `MIN_PROSE_CHARS` = 40) also gets this synthesis when the records call found nothing, and keeps a weakly backed item (`engine._backing`, the synthesis-mode rule of step 5), whose slot is then `partial` (`insufficient_evidence`, confidence `0.5`); a weak item never replaces a verbatim `populated` slot (Phase 21, cause: the finance deck's Key Findings 01-04, `extractive` text slots 89 characters wide, stayed `missing` because no chunk states a finding verbatim). A list of N verbatim slots costs 1 call instead of N (2 with synthesis). Group fields skip the per-field candidate check (step 2): keyed cards are not grouped (see Tiles), and a keyed record of a numbered grid is tied to its heading by the records prompt only (`shortcut: no second heading check per record; add one if the live test shows values landing on the wrong row`).

**Charts** (Phase 18, `engine._chart_result`): the row call (`table_rows.md`) with columns `category` plus one `number` column per series (`constraints.columns`, default `[label]`), at most `constraints.max_items` or `MAX_CHART_POINTS = 24` rows. A row is a data point and is kept only when its category is non-empty and holds no template token, every value is a number (`canonical_value(…, "number")`), its category was not kept before (after `normalize`), and one verified quote **states the point** (`engine._states_point`): the category occurs in it as a whole word and every value occurs (typed check as `value_in_quotes`, so `1.8` is never backed by `18`) after it and before the next category the reply returned. So a quote naming several points (`January 82 mm, February 64 mm`, or contiguous table rows `| May | 48 |` `| June | 37 |`) backs each number only for its own category, never a swapped pair; a number written before its category (`82 mm in January`) is not read as a point (the row drops, never mis-pairs). The row prompt asks for a table row too short to quote alone (under the 8-character minimum after normalization, e.g. `| May | 48 |`) to be quoted with the row next to it as one contiguous span. Value = `[[category, "<canonical number>", …], …]` in reply order. 2 or more kept points -> `populated` (confidence `0.9`) when every returned row was kept, else `partial`; fewer than 2 -> `missing(evidence_failed_verification)` (a chart needs two verified points), so a chart never shows an unverified label or number. `shortcut: canonical numbers carry no K/M suffix scaling ("$1.2M" is 1.2), so a series mixing "$900K" and "$1.2M" plots inconsistently; add unit scaling if sources do that.` Chat edits may set a chart value: rows of a non-empty category plus numbers, stored canonical.

The group query is every field's label in group order (a trailing slot number dropped, so `Value 1`..`Value 4` is `Value`; deduplicated, at most 300 characters) plus the section path, prefixed with the context only when every field shares it; when the group's labels, field ids, section paths and contexts state one direction, a short hint (`CR incoming credits deposits receipts` or `DR outgoing debits withdrawals payments`) is appended, so paired LARGEST RECEIPTS / LARGEST PAYMENTS groups on one slide query differently. Progress: `{stage, done, total, field}` before each group or field (a group reports its earliest field once), `done` after it.

A retried job recomputes every field; a run that is no longer `queued`/`retrieving`/`extracting` is returned unchanged. When the job fails for good, the `run_mapping` failure hook marks the run `failed`.

`failed` is reserved for infrastructure errors (DB, Qdrant, storage, writer). A field-level LLM failure (invalid output after the corrective retry) does **not** fail the run: the field becomes `missing(llm_error)`. An unreachable LLM (connection error, timeout, 5xx) is an infrastructure error: the job is retried and the run ends `failed`.

### Per-unit batches (`engine.map_fields`, `engine.map_batch`, `extractor.extract_batch`)

A field is **loose** when it is in no row group (`row_groups`), its `expected_type` is not `table` or `chart` and its `mode` is not `synthesis` (a synthesis slot is mapped alone by `map_field`, also on the opening unit) or `derived` (mapped alone by `map_field`, never in a row group, batch, shared fact or slot-role box; `map_fields` builds the row groups from the non-derived fields). Cause: a live run of 89 fields took 13 min 48 s because every loose slot was mapped alone (its own retrieval, an extract call, often a corrective retry, and a candidate check: 2-3 sequential LLM calls per slot).

- **Batching.** Loose fields are grouped by `_unit` (slide, sheet or docx section) and, for native table cells (`...:rNcM` ids), by their table, so a table's loose cells are a batch of their own apart from the unit's text slots, in IR order, in chunks of at most `MAX_BATCH_SLOTS = 12`. A batch is mapped when the loop reaches its first field. A field whose `shared_facts` key was already mapped is copied (no call) and leaves the batch; one with a new key joins and fills `by_fact` afterwards. A batch left with one field uses the single-field `map_field` (its pipeline below), except on the **opening unit** (the unit of the IR's first field), where it is still a batch so the opening chunks are read. Progress is unchanged: `{stage, done, total, field}` before the unit with its first field, `done` after it.
- **Retrieval** (`engine._retrieve_batch`). One `search` per field (`field_query`, `top_n=2`) and one per distinct context heading with a letter (`top_n=1`), run concurrently; the lists are merged round-robin by rank and deduplicated by `chunk_id`; `with_neighbors(ctx, hits, 2 * budget)` widens them (`budget` is the chunk budget of step 1 below); then `select_context(hits, min(RETRIEVAL_CONTEXT_TOKENS, prompt_tokens(LLM_NUM_CTX) - 3072))` cuts by tokens. On the opening unit each source document's first two chunks (`Retriever.opening`) come first and are kept through the cut. On the opening unit with no search hit, the opening chunks alone are the context. Otherwise no hit from the searches -> every field `missing(no_relevant_chunks)`, no LLM call.
- **Extract.** One call (`extract_batch`, prompt `llm/prompts/slot_values.md`, `think=False`) gets `{"slots": [{n, label, description, expected_type, context?, placeholder?, constraints?, fit_chars?}], "section_path"}` (every template string bounded as in step 1) and the chunks, and replies `{"slots": [{"n", "candidates": [...], "not_found", "incomplete"}]}`. Each slot answers for its own label and context only. Per field the first entry with its `n` wins (none: not found). A candidate is converted to the field's type: a list type takes a list (a string is one item), a scalar type takes a string (a list is dropped, never joined). The result goes through the **same** `resolve` as a single field (steps 3 and 4): every value is verified alone against the batch's chunks, so a quote that does not verify is `missing(evidence_failed_verification)` however the call went.
- **Escalation, only when needed.** (a) A field whose verified candidates all break a slot rule (`_rule_notes`) is mapped again with `map_field` (its own retrieval and one corrective retry). (b) A list field that needs synthesis, or an `extractive` prose slot reported not found (step 5), goes through `_synthesized` (`_filled_by_synthesis`). (c) Among the batch's `populated`/`partial` text results with a context holding a letter, when two fields with **different** contexts got the same value (after `normalize`), `check_candidates` runs for each of those fields only (a field whose check says no becomes `missing(not_found_by_llm)`, invalid output `missing(llm_error)`); no other candidate check runs for a batch. (d) `LLMInvalidOutput` from the batch call sends every field of the batch to `map_field`. A transport `LLMError` propagates (the worker retries). `check_slot_roles` still runs at the end over all results.
- **`fit_chars`** (`IRField.fit_chars`, set by code from the box size, § 4/5) is a soft hint only: it goes into the batch spec, the single-field spec and the records spec, and the prompts say it is about how many characters fit the slot's box, so the shortest verbatim sentence or phrase that states the value is preferred and a quote is never cut, joined or paraphrased to fit. It is never a constraint: not in `constraints`, no `max_chars` check, no warning, no summarising.

### Derived slots (`engine._derived`, `mapping/derived.py`, prompt `llm/prompts/derive_bind.md`, ADR-018)

`map_field` routes a `derived` field here first. (a) `Retriever.table_chunks` reads every table chunk of the run's documents and `derived.tables` rebuilds each table from its structured rows; none: `missing(no_relevant_chunks)`. (b) Binding: with an IR `derivation`, the candidate tables are those holding every referenced header (`column`, `minus`, `group_by`, `pick`, `where` keys): one is used, none is `missing(no_relevant_chunks)`, several `missing(evidence_failed_verification)`. Without one, one `complete_json` call (`LLMDerivation`) is shown each table (`t1`..`t20`: filename, exact headers, first and last 3 rows with row ids) and returns the table label and a nested `derivation` (the exact headers, the op, filters and printed total rows to exclude, `group_sep`, `pick`); never a number; `rank` is left null. The binder also sees the slot's `section_path` and `context`. **Intent** (Phase 26): when `derivation_intent(field)` is not None, `engine._with_intent` replaces the binding's `op` and `tag` with it (it keeps `column`, `minus` for `diff`, `group_by`/`group_sep` for `group_sum`, `pick` for `top`, `where`, `exclude_rows`; `diff` has no tag); a result that is no valid `Derivation` is `missing(evidence_failed_verification)`; the intent tag needs a CR/DR-tagged column, else the binding is kept (tag dropped) only when it has a `where` filter or its column header names the same direction ("Money In"), otherwise it fails closed, so an untagged or signed amount column never sums both directions; a `top` intent on a non-numeric slot with no `pick` (or `pick` = `column`) fails closed; a preset derivation is never overridden;  `not_found` or an unknown label: `missing(not_found_by_llm)`; invalid output `missing(llm_error)`. (c) `derived.compute` returns the value or a failure code (logged as `derived_rejected`, code only): `column_missing` and `no_rows` give `missing(no_relevant_chunks)`, every other code (`op_type_mismatch`, `rows_incomplete`, `cell_unparseable`, `ambiguous_number_format`, `untagged_cell`, `mixed_tags` (CR and DR cells with no `tag` filter; `(1.234,56)` is `cell_unparseable`), `group_label_missing`, `summary_row`, `rank_out_of_range`) `missing(evidence_failed_verification)`. `summary_row` (Phase 24 follow-up): `derived.summary_rows` flags a row equal to the sum of the values since the previous flagged row or of all values before it (at least two values, per aggregated sequence: `diff` sides and CR/DR subsets separately), a printed total, subtotal or carried-forward row; `compute` fails closed on it and never auto-excludes, and for an LLM binding `engine._derived` makes one corrective re-binding that names the flagged row ids (`_bound(..., retry=)`, the `extract` retry shape) before giving up as `missing`; an excluded id must itself be a row `summary_rows` flags with no exclusions, else `compute` returns `exclude_unverified` (missing; trade-off: a non-sum row the binding rightly skips, e.g. an opening balance in an amount column, makes the slot missing); a preset derivation goes straight to `missing`. (d) One `Evidence` per row used: `quote` is the row's verbatim line (`derived.row_line`), re-verified with `verify_quote` and checked against the quote length limits, else `missing(evidence_failed_verification)`; `row_id` is the row's provenance id and `item` the group index for `group_sum`. (e) The value is stored like an extractive one of its type (a number for `number`/`currency`/`percent` from the exact `Decimal` of `Computed.number`, never the rendered text re-parsed; `_convert` for the list forms), `populated`, confidence `0.9` (typed) or `0.8` (list, table). The synthesis path `derived_in_quotes` (step 3 below) is unchanged for IRs stored before Phase 24. **Ranked slots** (Phase 25, ADR-019): `op` `top` is the `rank`-th largest row by magnitude of `column` (ties in source order; `tag` filters `CR`/`DR`; the slot takes the row's `pick` cell, default `column`, as text, date or number; a signed column with no `tag` and both signs is `mixed_tags`); `group_sum` with `rank` returns the k-th group's label (`text` slot) or sum, and `group_sep` takes the group label as the `group_by` cell text before the separator. `engine._slot_rank` sets `rank` in code from the slot number of the field id (`SLOT_NUMBER`) for the ranking slots `ir_builder._ranking_slots` finds only, never from the LLM, and `map_fields` passes it to `map_field`. After mapping, `engine._check_ranks` makes every populated cell of a record (slide, first field id word, number) cite the same row ids, and no two ranks of a grid cite the same rows; a violation makes the cells `missing(evidence_failed_verification)` (log code `rank_mixed`). `shortcut: at most 20 tables are offered to the binding, and a preset derivation matching two tables fails closed; rank tables by the slot label if a source needs more.`

### Per-field pipeline

```
PENDING ─retrieve─▶ RETRIEVED ─extract─▶ EXTRACTED ─verify─▶ RESOLVED(populated|partial|missing|conflict)
   │ 0 hits              │ not_found / llm_error
   ▼                     ▼
 missing(no_relevant_chunks)   missing(not_found_by_llm | llm_error)
```

Think on escalation (`THINK_ON_ESCALATION`, every provider): the first pass of every call stays fast (`think=False`). After `resolve` (and synthesis for lists), `map_field` and `map_batch` give a slot one more `extract(..., think_budget=THINK_ESCALATION_MAX_TOKENS)` when it is a `conflict`, `missing(evidence_failed_verification)`, or `missing(not_found_by_llm)` although a retrieved chunk scored at least `THINK_ESCALATION_SCORE_THRESHOLD`; `populated` and `partial` never escalate, and a `synthesis`-mode slot never escalates on a high-score miss (it expects no verbatim value; Phase 19, cause: narrative finance slots spent a thinking call each and still found nothing to copy). A `missing(evidence_failed_verification)` slot is retried with its first reply echoed (cut to `RETRY_ECHO_CHARS`) and one note per rejected candidate, `<code>: <why>` (`engine._rejection`: `quote_not_in_chunk`, `number_not_in_quote`, `date_not_in_quote`, `slot_rule` with the rule note, `value_unusable`), over only the chunks its candidates cite and their neighbors in the same document (`engine._near_cited`; every chunk when none of the cited ids was retrieved); the codes are logged as `verification_rejected` (`slot_id`, `codes`; codes only, never values or quotes), whether or not escalation is on (Phase 21, cause: verification failures were retried blind, with no reason given and none logged). A conflict or a high-score miss is retried with no echo, over every chunk. **Row provenance checks** (Phase 23, cause: `NET MOVEMENT` took the closing balance and a reconciliation `Closing balance` took a "balance brought forward" row, because a typed value was verified only as a number in a quote). Both use the structured rows (`Hit.rows`) of the chunks the verified evidence cites and do nothing for a hit with `rows = null`. (1) `row_label_mismatch` (`engine._row_label_note`, called from `_rejection`, so the real candidate gate and the logged code agree; escalated like any verification failure): for a `number`/`currency`/`percent`/`date` slot, the value's cells are the cells whose own text states it; a cell's label is its row label (the row's first-column cell, unless it is the value cell) plus its column header; a slot matches a cell when `_names` finds every slot-label word in that label. The candidate is rejected only when no value cell matches the slot and another cell of those rows does; otherwise it is accepted (no rows, no labelled match, a non-matching language, or the value also sits in a matching cell). The retry note is `row_label_mismatch: <value> is in row [<id>] '<label>'; the slot '<slot label>' is row [<id>] '<label>'` (never logged). (2) `row_mixed` (`engine._mixed_rows`): a template row whose non-empty cells are each stated by some source row of the cited chunks but not all by one is mixed; unknown (a cell in no structured row) decides nothing. `fill_table_rows` drops a mixed row (the table is `partial`, `verification_rejected` code `row_mixed` once per row); a record of a row group or numbered slot grid keeps only the cells of the source row that states the most (ties: the row stating the record's key cell, then the first id) and the others go `missing`, the row `partial`. The escalated reply goes through the same heading check and `resolve`, so its values are verified like the first pass's and two distinct verified values are still a `conflict`. A list field's escalated value then gets the first pass's length check (`_needs_synthesis`, then `_synthesized`); a list that still breaks `max_items`/`max_chars` keeps the first result, as does a `missing` reply or an `LLMError` (a verified conflict is never dropped); there is no second escalation. The thinking budget is added on top of the reply cap, so reasoning never starves the JSON. Each escalation logs `think_escalation` with `slot_id`, `reason`, `before_status`, `after_status` and `elapsed_ms`.
Row groups (`map_group`: tables, charts, records) escalate the same way, once per group: the whole structured call (`extract_rows` or `extract_records` with the budget) and its row verification run again when any slot is a `conflict` or `missing(evidence_failed_verification)`, or, with a retrieved chunk at the threshold, nothing was filled (`high_score_miss`, never when every field of the group is `synthesis`) or one column is empty in every filled row (`missing_column`, e.g. a `ratings` column the fast pass dropped). The escalated group replaces the first one whole, and only if it fills more cells, so rows never mix the two passes and a miss or an `LLMError` keeps the first. It logs the same `think_escalation` event, `slot_id` being the group's first field.

Hybrid thinking (`LLM_HYBRID_THINKING`, `openai_compat` only): `complete_json(..., think=True)` is used only where reasoning pays: `extract_rows` and `extract_records` for a group of two or more rows (several entities to tell apart; cause: a live run of 89 fields spent 30-45 s on each of `synthesize`, `check_roles` and one-row records, which hit the thinking cap and then ran a second pass). Every other call (`extract`, `extract_batch`, `extract_records` for one row, `synthesize` (both calls), `check_roles`, `check_candidates`, `check_rows`, router, IR build, Q&A, edits) stays fast. A thinking call streams and counts reasoning events (`reasoning`, `reasoning_content`, or content inside a leading `<think>`); past `LLM_THINKING_BUDGET_TOKENS` it stops reading and makes one non-streaming, non-thinking pass whose last user turn carries the partial reasoning. Each call logs `llm_call` with mode, reasoning token count and whether it was capped. Measured (Phase 19 live test, Qwen3-14B-4bit on `mlx_lm`, budget 160): every hybrid call was capped, so the 160 reasoning tokens were spent and then thrown away; replaying every multi-row group of a 10-slide deck with `LLM_HYBRID_THINKING=false` gave the same value and status on all 70 fields and took 6-9 s less per records call. Keep it off for that model.

1. **Retrieve.**
   - Query = `label + " — " + description + " (" + " > ".join(section_path) + ")"`, prefixed with `context + ": "` when the field has a `context` (§ 4).
   - Budget (`mapping/extractor.py`): `hit_budget = max(1, (prompt_tokens(LLM_NUM_CTX) - 3072) // CHUNK_MAX_TOKENS)` chunks, where `prompt_tokens(n) = max(6144, n // 2)` (`llm/gateway.py`) and 3072 tokens cover the system prompt, the field spec and one truncated retry echo. That is 6 chunks at 8192 (the old fixed value) and 26 at 32768; the rest of the window is the reply (and reasoning when thinking is on).
   - `budget = min(hit_budget, max(1, RETRIEVAL_CONTEXT_TOKENS // CHUNK_MAX_TOKENS))` (6 at the defaults with `LLM_NUM_CTX=8192`), `retriever.search(ctx, query, document_ids=run.document_ids, top_n=min(max(1, budget // 2), MAX_PRIMARY_HITS = 12))`; when the slot has a `context` with a letter, a second `search(ctx, context, ..., top_n=1)` adds its hit if new (a section's lead-in rarely matches a slot label; a group does this when all its fields share one context), then `retriever.with_neighbors(ctx, hits, budget)` (§ 3) fills the rest of the budget with the chunks next to the ranked hits, so a value whose heading sits in the previous chunk is read with it. The budget is sized before `with_neighbors` because it fills by rank while `select_context` trims its reading-order output: trimming a larger list would drop the best hit when it sits late in the document.
   - `checked_documents` = the distinct `document_id`s in `run.document_ids`, i.e. everything that was searched, not just the documents with hits.
   - Only the `hit_budget` chunks shown to the LLM (ranked hits and neighbors) can be cited.
   - The query and the prompt use a bounded copy of the field: the last 4 `section_path` elements (120 chars each), `context` (120 chars), at most 30 `columns`/`allowed_values` (80 chars each).
2. **Extract.** The LLM gets the field spec and the hits (each labeled with its `chunk_id`), with JSON-schema output:
   `{"candidates": [{"value": …, "quotes": [{"chunk_id": "…", "quote": "…"}]}], "not_found": bool, "incomplete": bool}`.
   The prompt says to copy quotes verbatim (the whole sentence or table row that states the value), use only the given chunks, and return `not_found` rather than guess. A field asking for one item of a list (an ordinal slot such as the first upcoming award, or a named item) gets only that item: other items of the same list are not conflicting values. Template placeholder text is never a value. A field's `context` is in its spec: the value must belong to that heading, and values under sibling headings are not this field. A value must also be the kind of quantity the field asks for (a count of people is not an amount of money).
   **Candidate check** (`extractor.check_candidates`, prompt `llm/prompts/candidate_check.md`, cause: in the Phase 14 eval a card whose heading had no value in the source got its sibling cards' values; prompt rules alone did not stop it). Only for a field with a `context`, at least one candidate and at least one sibling heading (`map_field` skips it otherwise; Phase 18 live test 3, cause: a contact line under the generic slide title "Questions & Discussion" was rejected because the source never states that title, and a prompt rule for generic titles was ignored by the MLX model): one more call gets `{context, label, description}`, `other_headings` (`engine.sibling_headings`: the other distinct contexts of fields in the same unit, in order, at most 20, 120 chars each) and, for at most 10 candidates, the value (200 chars), its first 3 quotes (1,000 chars each), the chunk's `section_heading` and the text of the chunk just before it in the hits (500 chars). It replies `{"checks": [{"about", "match"}, ...]}`: `about` is the heading, item or entity the value is stated for, copied from the quote, section or preceding text (a reasoning aid only: never stored, logged or used by code), and `match` says whether it is the slot's `context`, in any language or wording (a value stated for one of `other_headings` never matches). **All or nothing:** if any entry matches, the extraction is kept unchanged (every candidate); if none matches, the field is `not_found` (`missing(not_found_by_llm)`). The check never drops a single candidate, so it can never choose between conflicting values (a per-candidate drop was tried and rejected one of two real conflicting values in a prototype; running the check on fields without `context`, judged by label alone, rejected many correct values, so they are not checked). Invalid output twice -> `missing(llm_error)`. Cost: one extra LLM call per context field with candidates.
   The LLM `value` is a string for text/number/currency/percent/date, a list of strings for paragraphs/bullet_list, and a list of rows (lists of cell strings, body rows only, in `constraints.columns` order) for table; `table` fields are extracted with the row call instead (see Row groups above), so each row is verified against one record. `not_found = true` wins over any candidates.
3. **Verify** (`verifier.py`, deterministic).
   - `normalize(s)` = NFKC → casefold → map curly quotes and dashes to ASCII → replace `|` with a space (so a quoted Markdown table row matches its chunk) → collapse whitespace → strip.
   - A quote is verified iff `chunk_id ∈ hits` **and** `normalize(quote) in normalize(hit.text)` **and** `len(normalize(quote)) >= 8`.
   - Quotes shorter than 8 or longer than 1,000 raw characters are dropped. The stored quote is the LLM quote verbatim.
   - **Quote recovery** (`engine._located`, mapping only; Phase 18 live test 3, cause: `mlx-community/Qwen3-14B-4bit` quoted the right risk-table row but cited the heading chunk just before it, and quoted table rows 2..N with the header glued on, so all four risk levels and three of four next-step rows failed). When a quote is not in its cited chunk but that chunk was retrieved, the quote, else each of its lines (a line needs 8+ letters or digits, so `| --- |` separators are skipped), is looked for in the cited chunk and in a retrieved chunk of the same document whose `chunk_index` is one before or after it. Each part found is evidence of the chunk that holds it, so every stored quote is verbatim in the chunk it names. An unretrieved chunk id, or a chunk further away, finds nothing, and a line the source does not hold is dropped. `verify_quote` itself is unchanged (chat citations use it as is).
   - Typed values (`number`, `currency`, `percent`, `date`): the value is normalized to a canonical form (`12,000` → `12000`; dates → ISO) and must appear, canonicalized, in at least one verified quote of that candidate. A `synthesis`-mode `number`/`currency`/`percent` value may instead be **derived** (`verifier.derived_in_quotes`): the sum or difference of two different numbers of its verified quotes (net movement = deposits - withdrawals; the extraction prompt allows this only for `mode` `synthesis`). `shortcut: two operands only, add longer sums when a template asks for totals of 3+ figures.`
   - Typed canonical form: the value must state exactly one number (sign kept, thousand/million/billion applied, currency symbols and `%` ignored, digit groups of 3 after a comma) or one date (ISO). It must appear among the numbers/dates of a verified quote.
   - Number formats: US/UK (`1,829,914.84`) and European (`1.829.914,84`) both parse. A European reading is used only when it is unambiguous: dot groups before a decimal comma (`444.132,10`), two or more dot groups (`1.829.914`), or a bare 1-2 digit decimal comma (`10,50`, `752,76`). A value with a single group (`1,234`, `12.000`) keeps the US reading; a **quote's** single-group number counts with both readings (`12.000` states 12 and 12000), and a quote's number tagged `DR`/`CR` (before or after, any case, `1.234,56cr`) or with a trailing minus (`500.00-`, not a range `10-12`) counts with either sign (Phase 19, cause: MXN bank statements). `source_text` widens the span over the tag (`1,234.56 DR`).
   - Date formats: ISO, `D Month YYYY` (Spanish `de`/`del` connectors allowed: `1 de junio de 2022`, any case: `31 DE MARZO DE 2025`), `Month D[,] YYYY`, `D/Mon/YYYY`, `D-mon-YY` or `D mon YY` (`/`, `-`, `.` or a space; a two-digit year is 20YY), with English or Spanish month names, full or abbreviated (`jun.`, `sept`), and numeric `D/M/YYYY` when one part is over 12 (`31/03/2025`, `03/31/2025`). A numeric date with both parts 12 or less (`03/04/2025`) counts both ways in a quote but is no value (`canonical_value` is `None`: day/month order is ambiguous).
   - Stored values: text → string; paragraphs/bullet_list → list of strings (bullet markers stripped); number/currency/percent → JSON number; date → ISO string; table → list of rows; chart → list of `[category, number, …]` rows (numbers as canonical strings, one per series in `constraints.columns` order).
   - Evidence `filename`, `page_number`, and `section_heading` are copied from the **hit payload**, never from LLM output.
4. **Resolve status.**
   1. Drop unverified quotes, then drop candidates with no verified quote.
   2. Merge candidates whose canonical values are equal (union of evidence). Text merges on `normalize()`; lists on their normalized items.
   3. 0 candidates → `missing`. The reason is `evidence_failed_verification` if the LLM proposed something, otherwise `not_found_by_llm`.
      **Slot rules** (`engine._rule_note`; Phase 18 live test 3) drop a verified candidate that breaks its slot: a `#` count slot (token `[# 1]`, § 5) takes a whole number with no unit, currency or magnitude in the source (`7`, never `64.8M`), stated by one chunk that also names the slot's own label, the last ` / ` part of its context, each word of 5+ letters by its first 5: the verified quotes are grouped by `chunk_id` and the rule passes only if some one chunk's quotes hold the plain-number span and name the label, so a number from one chunk never borrows a label stated in another; when a quote copies a body row of a Markdown table in its chunk, that table's header row counts toward naming the label (`engine._header_rows`; Phase 19 live test, cause: a sideways table's row `| No. of events | 7 | 6 | ... |` sent all six counts of a slide to `map_field` alone, 6 retrievals and 12 extra calls that ended with the same header-plus-row quote) (cause: `[# 1]` "Requirement" got the headline total `25`, `[# 6]` "Contract" the `AED 64.8M` value; Phase 19, the total `25` was accepted because a column header of the next chunk named the label); a `text` slot never takes a value holding a Markdown cell break ` | ` (several table cells: cause, a risk description came back as its whole row). When every candidate verifies but breaks a rule, `extract` runs once more with its reply and one note per value (values and labels only, never logged); a second break is `missing(evidence_failed_verification)`. A card slot's (`IRField.card` set) text-like value (`text`; every item of a list) whose words, after `normalize`, are all words of the slot's own `context` only repeats a heading (`Risk: [Risk]` under `Price inflation` answered `Price inflation`): it gets a note asking for the description stated under the heading, and still echoing after the retry it is `missing(evidence_failed_verification)` (Phase 19). A card slot that really is the heading's own name cannot be filled; known limit. Table cells are not checked: their context also holds the slide heading, which a correct cell value may repeat.
   4. ≥ 2 candidates → `conflict` (`value = null`, `conflict_details.candidates`). **Never auto-resolve.**
   5. 1 candidate → `populated`. It becomes `partial` (`missing_reason = "insufficient_evidence"`) when:
      - the LLM flagged `incomplete`, or
      - `min_items` is not met, or
      - a text, list or table value has an item (cell) that appears in no verified quote of that candidate (paraphrased text is never `populated` by extraction; see step 5 for grounded synthesis of lists), or
      - a `table` row has fewer than `len(columns)` non-empty cells, or
      - a typed value is derived (no quote states it), so a computed figure is always flagged for review.
   6. A value longer than `max_chars` is still written (after step 5 had its chance to fit it); a warning (`{"field_id", "code": "max_chars_exceeded"}`) goes into `mapping_runs.summary.warnings`. Values are never silently truncated.

5. **Grounded synthesis** (`engine._synthesized`, `extractor.synthesize`, prompt `llm/prompts/field_synthesis.md`, `verifier.supported`; cause: summary and finding fields ended `partial`, a fake `conflict` of one candidate per paragraph, or a verbatim list far over the slot). A `synthesis`-mode field that is not typed (`text`, `paragraphs`, `bullet_list`) skips extraction: `map_field` calls `_synthesized` right after retrieval with `missing(not_found_by_llm)` as the first result and no candidate check (a written summary never sits under the slot's heading in the source; Phase 19 diagnostic, cause: it rejected the only two finance summaries that passed `supported`). Such a field also keeps an item whose content-word share is under 60% when the item states at least one number and every number is in its verified quotes (`supported(text, quotes, 0.0)`; a summary of table rows shares few of their words), or states no number and has a content-word share of at least 50% (`MIN_PROSE_SUPPORT`), and the field is then `partial` (`insufficient_evidence`), never `populated`; a number-free item under 50%, or an item with a number no quote states, is dropped. A `text` field gets its kept items joined into one string, every quote with `item` 0. Otherwise only for `paragraphs` and `bullet_list` fields, never for row-group fields (numbered lists have their own fallback, see Row groups), and the trigger is structural (no label words): the result is `partial` or `missing` with reason `evidence_failed_verification` / `insufficient_evidence` (the source has the content, not as verbatim items), or `populated` but over `max_items` or `max_chars`. Never after `no_relevant_chunks`, `llm_error`, or a `conflict`, and after `not_found_by_llm` only for an `extractive` `text`/list field sized for prose (`engine._prose`: `fit_chars`, else `max_chars`, at least `MIN_PROSE_CHARS` = 40; Phase 21, cause: free-text slots the IR model marked `extractive` had only a verbatim lookup): it gets one synthesis attempt under the synthesis-mode rules above, so a weakly backed value is at most `partial` (`engine._filled_by_synthesis`, in `map_field` and `map_batch`). Typed slots, slots under 40 characters and slots of unknown size stay `missing`: absent facts stay missing and conflicts are never resolved. One call on the same hits (plus one corrective retry that states the measured length when the reply exceeds `max_items`/`max_chars`) returns `{"items": [{"value": "<one sentence or bullet>", "quotes": [{chunk_id, quote}]}], "not_found"}`. An item is kept only if at least one of its quotes verifies (step 3) and `supported(item, its verified quotes)`: every number in the item equals (canonically, as typed values: `1.8` is never `18`) a number in its quotes, at least 60% of its content words (a sanity filter: the guarantees are verified quotes and exact numbers) (`\w{4,}` after `normalize`) occur in them (a word matches a quote word sharing its first `min(5, len)` characters: light, language-agnostic inflection), and the item is at most 400 characters (`shortcut: scripts written without spaces (CJK, Thai) form one long word per run, add a tokenizer when such sources matter`). Kept items get their evidence with `item` = their index in `value`, so every sentence keeps its own citations. Result: every item kept, fits `max_items`/`max_chars` and meets `min_items` → `populated`, confidence `0.7`; otherwise `partial`. A `populated` verbatim value is replaced only by a complete, fitting synthesis; anything else (no item kept, `not_found`, invalid output) returns the first-pass result unchanged. The chat summary names fields summarised from quotes. Extraction itself returns one candidate listing every item for `paragraphs`/`bullet_list` (items from different chunks are not different values; several candidates only when chunks contradict each other about the same item).

**Box roles** (`engine.map_fields`, `extractor.check_roles`, prompt `llm/prompts/slot_roles.md`, Phase 19 live test, cause: a box `Account holder: [Account Holder] / Bank / institution: [Bank Name]` gave both slots the context `ACCOUNT SUMMARY`, so the candidate check accepted the holder's name, quoted from the addressee block, as the bank; rules added to `candidate_check.md` changed nothing on the MLX model, the comparative question below did). After every field is mapped, `replace_token` fields that came back `populated` or `partial` from RAG with the same value (after `normalize`) and at least two distinct role keys (`engine._role_key`: the normalized label without trailing filler words value, amount, valor, importe, monto (numbers kept; `shortcut: a Spanish "Fecha valor" is read as "Fecha"`), so "Total Deposits" and "Total deposits value" are one role and not checked) are checked together: `text` fields on one anchor, and `number`, `currency`, `percent` fields with the same canonical number anywhere in the deck (Phase 25: `"$ 402.031,45"`, `"402.031,45 $"` and `402031.45` are one group; the Phase 21 grouping was per slide, so a net movement and a closing balance on different slides were never compared) and `date` fields in one slide, sheet or docx section (Phase 21, cause: a finance slide's net movement got the closing balance, cited from the closing-balance line, and both were `populated`, because a typed value is verified only as a number its quote states). The group check is: one call gets the value (200 chars), the numbered slots (`label`, `description`) and up to 10 distinct evidence quotes of the group with their `section_heading`, and replies `{"about", "slots": [n, ...]}` (`about` is a reasoning aid only: never stored, logged or used by code). Each slot not listed becomes `missing(evidence_failed_verification)`, so a role the source never states is not filled from another role's value; invalid output twice makes every slot of the group `missing(llm_error)`. The fields sharing a demoted field's fact (`shared_facts`) are demoted with it. Each demoted slot that is not in a row group then gets one `map_field` retry (Phase 21, cause: a finance slide's NET MOVEMENT was dropped with no second try for its own value): its first extraction echoes the rejected value with its quotes and the note `slot_role: <value> is the value of <kept labels>, not of <label>`, logged as `verification_rejected` with code `slot_role`. A retry that is `missing` or repeats the rejected value keeps the slot `missing(evidence_failed_verification)`; another value replaces it and its shared copies, and the groups holding a retried slot are role-checked once more, never retried again. An unusable check (`llm_error`) is not retried. Cost: one extra call per such collision, plus one retry per demoted slot. `shortcut: dates stay per slide (a date repeats across slides), widen if a date role is misfiled.`

**Amount in an identifier column** (Phase 25, ADR-019, cause: a Reference column filled with the neighbouring amounts): a `text` slot, or a text column of a table or grid, whose header (`context` or label, accents folded) holds an identifier, reference, description or date word (`engine._ID_HEADERS`, EN/ES; ambiguous words such as account, card, details are left out) rejects a value that `verifier.is_amount` finds to be one monetary amount (a number with a decimal part or thousands grouping, or any number with a currency symbol or DR/CR tag; "REF 1234" is not one). The cell is not populated, logged as `verification_rejected` with code `amount_in_text_column`, and a single slot gets the note on its one retry; for a table or grid, `extract_rows`/`extract_records` take the collected notes (`notes`) on the escalated group pass.

Units run sequentially (`JOB_CONCURRENCY=1`): one bounded LLM call per row group, per batch of loose fields and per lone loose field, plus the escalations above (at most 2 attempts per call, each reply capped by `num_predict`). `jobs.progress` is set before each unit (group, batch or field) to `{"stage": "mapping", "done": i, "total": n, "field": "<field_id>"}` and after it to `{"stage": "mapping", "done": <results so far>, "total": n}`. Stored results are a historical snapshot: deleting a source later does not change them.

## 8. Surgical writers (Stage 4)

Common rules:
- Copy the template `original.{ext}` into memory, mutate it, and write it as a new `artifacts/{artifact_id}.{ext}`. The upload is never changed.
- `artifact_id = uuid5(fixed namespace, run_id)`, so a retry targets the same `artifacts/{artifact_id}.{ext}`. The file is written with `put_bytes(overwrite=False)` (atomic); `FileExistsError` means an earlier attempt crashed after writing, and that file is reused. The row, `completed` and `finished_at` commit in one transaction. If the project was deleted meanwhile, the file is removed and the job finishes. Writer errors (unresolvable anchor, bad content) are not retried: they are logged as `render_failed` with `error_type` and, for the writer's own codes, `code` (`anchor_unresolved`, `token_not_found`), never the message.
- download `filename` = `{template stem}_v{version}.{ext}`; stem characters outside `[A-Za-z0-9._-]` become `_`, cut to 100, fallback `artifact` (`shortcut: non-ASCII names become "_"; add RFC 5987 filename* when users need them.`).
- every target is resolved before the first write, so cloned paragraphs never shift later anchors.
- markers: `{label}` is replaced literally (never `str.format`); XML-illegal characters are stripped from every value.
- Only anchors named in the IR are touched. All other package parts must be **C14N-equal** to the template, and are byte-equal for XLSX.
- What gets written per status:

| Status | Written |
|---|---|
| `populated`, `partial` | `value` |
| `missing` | `MISSING_MARKER` (default `[MISSING: {label}]`) |
| `conflict` | `CONFLICT_MARKER` (default `[CONFLICT: See Chat Summary]`) |

- Value rendering by `expected_type`:
  - `paragraphs` / `bullet_list`: one paragraph per item;
  - `number` / `currency` / `percent` / `date`: formatted with `constraints.unit` / `date_format` for text targets, typed for XLSX.
  - `text` targets: a typed value is written as the source wrote it: `source_text` (verifier) returns the first span of a verified quote whose canonical value equals the field value, with the same number/date rules as verification, plus a suffix glued to the number (`%`, or 1-2 letters ending the word: `182.4M`, `5bn`, `3.2K`) and an adjacent currency marker (any Unicode currency symbol; for `currency` also a 3-letter uppercase code). `number`/`currency` spans that show no currency marker and not the unit itself get `" {unit}"` when `unit` is set; `percent` spans without `%` get `%`. With no such span (a user-set value, no evidence) the fallback is `"{v} {unit}"` (or `"{v}"`), `"{v}%"`, ISO `YYYY-MM-DD` (`shortcut: a sample number in the template ("$0.00") is not read; upgrade when templates rely on it.`);
  - `replace_token` with a typed value: a non-numeric prefix or suffix of the value that the template already shows next to the token is dropped (a suffix only when it is the whole next word: `[Spend] Managed` keeps `182.4M`; `$[Amount]` + `$1,250` → `$1,250`, `[Rate]%` + `12.5%` → `12.5%`, `AED [Spend]M` + `AED 182.4M` → `AED 182.4M`);
  - lists into a single run/cell (`replace_token`, xlsx) are joined with `"; "`;
  - a `table` value into a non-`fill_table_rows` anchor is one paragraph per row, cells joined by `" | "`;
  - XLSX: `percent` is written as `v / 100`; a `date` is an Excel serial only when the target cell's style has a date number format (built-in 14-22, 45-47, or a custom format with d/m/y), otherwise ISO text as `inlineStr` (`shortcut: 1900 date system only`);
  - `max_items` is not enforced (values are never truncated).
- Write modes:
  - `fill_table_rows` writes value rows into the existing body rows from row 1, one value per unique cell (merged docx cells and spanned pptx cells are written once); every body cell it did not write (all but `(1, 0)` for a marker, cells past a short row, rows past the content) has its placeholder tokens removed so no `[...]` stays, static text kept (Phase 19);
  - rows or cells that do not fit are not written and the field gets one `table_overflow` warning (status unchanged);
  - a missing/conflict marker goes into cell r1c0.
  - Warnings go to `mapping_runs.summary.warnings` as `{"field_id", "code"}`.

### PPTX (`python-pptx` + `lxml`)

- `replace_text`:
  - keep the first paragraph's `a:pPr` and its first run's `a:rPr`;
  - put the value in that run and delete the other runs;
  - extra items get a cloned paragraph (same `pPr`/`rPr`), and leftover original paragraphs are removed.
- `replace_token`: first merge runs that the token spans (keeping the first run's `rPr`), then replace the token text in place. Several `replace_token` fields on one anchor are applied one after another, longest token first (`render()` in `writers/__init__.py`), so a token that contains another (`____` inside `______`) is never broken by the shorter one.
- Table cells work the same way inside `a:tc/a:txBody`.
- `fill_chart` (Phase 18): `chart.replace_data` with the rows' categories and one series per template series (names kept, a missing column plots no point), so the chart type, colours and formatting stay. Bracketed tokens in the chart title are unwrapped to their inner text (`[Spend by category ($)]` -> `Spend by category ($)`; `shortcut: only the template's own title words are kept, a bare blank stays, upgrade by naming the title as its own field`). `missing`/`conflict`: the data becomes one empty category with no values and the title becomes the marker, so the template's sample numbers never reach the output.
- `insert_chart` (Phase 18): `slide.shapes.add_chart` at the placeholder's left/top/width/height, then the placeholder shape is removed. The type comes from the data's shape only (`pptx_writer.chart_type_for`): `PIE` for one series of 2-6 positive values summing to 100 ± 1 (shares of a whole); `LINE` for 3+ categories that are all time-like (a parseable date, a year 1900-2099, a quarter such as `Q3 2026` or `2026 Q3`, `YYYY-MM`, or a month name the verifier knows, optionally with a year); `BAR_CLUSTERED` for more than 8 categories or a label over 20 characters; else `COLUMN_CLUSTERED`. Legend only for 2+ series or a pie. `missing`/`conflict`: the marker is written into the placeholder text. `shortcut: a placeholder inside a group shape gets the chart at its group child coordinates.`
- Shapes, layouts, masters, notes, and media are never touched (a filled chart's own chart part and embedded workbook change). Text overflow is reported through `max_chars_exceeded`.
- **Shrink on overflow** (`pptx_writer._fit`, Phase 19 live test, cause: `[MISSING: Credit Transaction Count]` in a 17pt KPI tile of 2.0 x 0.34 in wrapped to three lines over the label below). After all writes, each non-table text shape that got a text write is estimated (no font metrics: 0.55 em per character, 1.2 em per line, the largest explicit run size per paragraph, else 18pt; the frame's margins subtracted). Text that is one line in total always fits (templates use one-line boxes tighter than a line plus insets). Otherwise, when it needs more height than the box has, the largest scale from 100% down in 5% steps (floor 25%) that fits is written as `a:normAutofit fontScale`, replacing the frame's `spAutoFit`/`noAutofit`. Table cells, charts and frames that fit are unchanged. Typed values keep the source's own spelling (§ 8 `source_text`); only the size changes.

### DOCX (`python-docx` + `lxml`)

- Same run/paragraph rules on `w:p`/`w:r`/`w:rPr`.
- Header and footer anchors are resolved through `section.header` / `section.footer`.
- `content_control` writes into the `w:sdtContent` of `w:sdt` with a matching `w:tag`, and removes `w:showingPlcHdr`.
- Numbering (`w:numPr`) is kept on cloned paragraphs, so bullet lists stay bullets.
- a paragraph `replace_text` keeps an inline content control that is itself a target of another field;
- filling a placeholder content control also drops `w:rStyle="PlaceholderText"`;
- cloned paragraphs drop `w:sectPr`.

### XLSX (zip-level XML patch, `lxml`)

`openpyxl` is used only to read anchors (ADR-006). The writer:
- Opens the zip and locates each worksheet XML via `workbook.xml` + `workbook.xml.rels`.
- For each target cell `<c r="B4">`, keeps the `s` (style) attribute and writes:
  - strings: `t="inlineStr"` + `<is><t>…</t></is>`; `sharedStrings.xml` is untouched;
  - numbers and currency: no `t` attribute, `<v>` with the canonical number; percent: `<v>` is `v / 100`;
  - dates: an Excel serial in `<v>` only for date-formatted cells, otherwise ISO text as `inlineStr` (see Value rendering).
- Creates the `<c>` element in column order if the cell doesn't exist.
- Never writes into a cell with an `<f>` formula; the field gets a `formula_cell` warning.
- Sets `<calcPr fullCalcOnLoad="1"/>` in `workbook.xml` so dependent formulas recalculate.
- Copies every other zip entry byte-for-byte.

### Known limits

`shortcut: no slide/shape/table-row/sheet-row insertion — fill_table_rows writes only into existing empty rows and adds a table_overflow warning when rows run out; upgrade when templates need dynamic row counts.`

`shortcut: touched runs keep only their text (tabs, breaks, fields inside a rewritten run are dropped); upgrade when templates carry them in anchored text.`

`shortcut: a repeated content-control tag writes the first control only; upgrade when templates reuse tags.`

`shortcut: xlsx rows/cells without an r attribute are not supported; upgrade when a template needs them.`

`shortcut: a custom number format counts as a date if it has d/m/y outside quotes and brackets; upgrade when a template misclassifies one.`

`shortcut: a final transaction that fails on every retry leaves the written artifact file unreferenced (run failed); upgrade with a sweep of unreferenced artifact files.`

## 9. Chat orchestration (`chat/`)

`POST /v1/pipe/chat`:
1. Resolve or create the project for `(user_id, openwebui_chat_id)`.
2. Map each `files[].openwebui_file_id` to a document via `openwebui_file_ids`. The Pipe uploads before chatting, so an unknown id means that upload failed: a `delta` names the skipped file and the turn continues.
3. Wait on every project document still `uploaded` or `parsing` (the 200 newest, referenced or not), on its `ingest_document` job, streaming progress as `status` events. A failed ingest adds a `delta` ("I could not read X, so I skipped it.") and the turn continues. The wait covers the whole project because a template pick purges Qdrant points. Each poll is 1 s (`POLL_INTERVAL_S`); a `status` is sent only when its text changes, otherwise a `: keep-alive` comment.
   `shortcut: no server-side wait cap; a stuck or disabled worker (JOB_CONCURRENCY=0) holds the stream until the client disconnects or TIMEOUT_S, add a cap if that happens.`
   `shortcut: disconnect is only noticed between polls; route/answer/start_template run to completion, run the flow as a cancellable task if Ollama time is wasted.`
4. **Intent routing** (`router.py`, prompt `llm/prompts/router.md`): one LLM call with JSON output `{"intent": "qa"|"map_template"|"edit"|"chitchat", "template_document_id": uuid|null, "candidates": [uuid], "format": "pptx"|"docx"|"xlsx"|null}`. `format` is the output format the user asked for ("powerpoint" → `pptx`, "word" → `docx`, "excel" → `xlsx`); a value outside those three is dropped. Intents are `qa`, `map_template`, `edit` (Phase 11) and `chitchat`. The handler receives `{intent, template_document_id}`: ids from both fields are checked against the project's pptx/docx/xlsx documents, de-duplicated. Only when exactly one remains is it considered for mapping. It sees the last 10 messages, each cut to its **last 1,500 characters**, so long run summaries can't push the prompt past `LLM_NUM_CTX` (Ollama silently drops the start of an over-long prompt, which is where the instructions are). It also sees the project's document list (ids, filenames, roles, formats), minus library copies (`role = 'template'` documents whose `sha256` is a library template's). Those are reached only through the library. No document text goes into this call.
   - `qa` → `qa.py`.
   - `map_template` resolves the template in this order (Phase 9):
     1. **Library choice** (`chat/library.py`). Either the last user message names a library template (case-insensitive, word boundaries; if several match, the unique longest name wins), or it holds a number and the last assistant message has the line `N. <md(name)> (<format>)` from a library list. Resolving by the rendered line means list order and filtering can't shift the choice. → `adopt` the template (copy-on-use, [data-model.md § library_templates](data-model.md#library_templates)), add the delta "Using the library template **name**.", and go straight to the mapping run: no `extract_template`, no LLM call for the IR.
     2. **A confirmed uploaded template** (`template_document_id`) → the flow below.
     3. **Uploaded template-capable documents of the requested format** exist (any format when `format` is null), or the library is empty → `render_candidates`, as before.
     4. **Otherwise the library.** If `format` is set and exactly one library template has it, run that one (as in 1). Otherwise reply with `render_library`: a numbered list `N. name (format): description` (description clipped to 200 chars; name and description escaped with `md()`) of the library templates of that format, or all of them when none match, asking for a number or a name.
   - `map_template` with exactly one candidate that passes the confirmation guard:
     1. set `role = 'template'` and delete that document's points;
     2. run `extract_template` (it reuses the existing `templates` row and latest IR if this template was mapped before), then create a `mapping_runs` row (`trigger = 'initial'`, `document_ids` = all `indexed` sources) and run `run_mapping`;
     3. stream progress and finish with `summary.py`.
     `shortcut: concurrent turns in one chat (regenerate, double send) can start duplicate extraction/mapping runs, add a per-project lock if that happens.`
   - The confirmation guard: candidates are first narrowed to the requested `format`, so a source DOCX is never auto-confirmed for "make a powerpoint out of this". A template is mapped only when it is already `role = 'template'`, or it is the only pptx/docx/xlsx document of the requested format in the project, or its filename appears in the last user message (case-insensitive), or the last user message contains its number in the numbered list. Otherwise the document keeps its role and points.
   - `map_template` with zero or several candidates, or one that is not confirmed → reply with a numbered list of the template-capable documents (`render_candidates`) and ask the user to choose. With no template-capable document, the reply asks for a PPTX, DOCX or XLSX upload.
     `shortcut: stateless confirmation via chat history, add projects.pending_action if routing proves unreliable.`
     `shortcut: the only pptx/docx/xlsx in a chat is mapped without asking; a source memo of that format then becomes the template (re-adding it needs DELETE), drop this rule if users hit it.`
     `shortcut: the list number refers to the current document order; an upload between the list and the reply can shift it, upgrade with pending_action.`
   - `chitchat` → the fixed help text (`HELP_TEXT`). No answer call is made; the routing call still runs.
   - `edit` (Phase 11) → the project's latest `completed` mapping run is the parent (none → "There is no generated document to edit yet."). `parse_edits` turns the last user message into `EditOp`s, then the same path as `POST …/edits` (`start_edit_run`) creates the new run and the Pipe waits on its `render_artifact` job. The reply is "Applied N edit(s). This is version V.", one line per applied op by field label (`render_edits`: "Set **Owner** to Ann", "Used value 2 for **Customers**", "Cleared **Date**"; labels and values escaped), then `render_summary` for the new run. Naming every op makes an edit the user did not ask for (prompt injection through a quoted value) visible. `shortcut: choose_candidate and clear have no deterministic check against the user's words (set_value does, ADR-013), add a label match if injected edits show up.` A rejected edit replies "I could not apply that edit: <reason>" and creates nothing.
5. **Q&A** (`qa.py`):
   1. Retrieve. No hits → the "not found" answer below, without an LLM call. A search failure → `503 upstream_unavailable`.
   2. Make one LLM call (prompt `llm/prompts/qa.md`) with JSON output `{"answer_markdown": "… [1] …", "citations": [{"marker": 1, "chunk_id": "…", "quote": "…"}]}`. Each hit is shown as `[chunk_id | filename | p.N | section]` followed by its text.
   3. Verify the citations (`mapping/verifier.py`; metadata copied from the hit). Remove unverified markers. If no verified citations remain, the answer is "I could not find this in the uploaded documents."
      `shortcut: a sentence whose only marker failed verification stays uncited, drop such sentences if the eval shows unsupported claims surviving.`
   4. Send the **verified** answer as a `delta`, followed by a source list (`[n] filename p.N: "quote"`, `render_answer`).
   `shortcut: no token budget, the context is bounded by TOP_N hits x CHUNK_MAX_TOKENS within LLM_NUM_CTX; add one if either grows.`
   Generation isn't token-streamed, because the answer has to be verified before the user sees it.
6. **Summary** (`summary.py`):
   - counts by status;
   - every `missing` field (label + reason);
   - every `conflict` (each candidate value + citations, numbered from 1 so the user can say "use the second value");
   - `partial` fields and warnings;
   - the signed download link.
   - Escaping: document-derived text (filenames, quotes, values, labels, and the filenames in skip and upload-failure messages, in the backend and in the Pipe) is markdown-escaped with `md()`, so an image or link inside a document cannot make Open WebUI fetch anything. `md()` also folds the text onto one line, escapes `~` and backticks (no code fence, which Open WebUI may open as a live HTML artifact), and writes `://` as `:\/\/` (no bare-URL autolink) (Phase 12). The LLM answer keeps emphasis, lists and tables, but every backslash, bracket and angle bracket in it is escaped and only plain `[n]` markers are restored (never one followed by `(`, `[` or `:`), so no inline or reference-style link, image, definition or raw HTML can form (`_answer_md`). Backticks and `~` are escaped too (no fence), and `://` becomes `:\/\/` (Phase 12).

## 10. Conversational edits (`chat/edits.py`)

`EditOp` (schema in [api-specification.md](api-specification.md#post-v1projectsproject_idmapping-runsrun_idedits)):

| `op` | Effect on the copied `FieldMappingResult` |
|---|---|
| `set_value` | `status=populated`, `value=<user value>` (coerced, below), `evidence=[]`, `provenance=user`, `confidence=1.0` |
| `choose_candidate` | only valid on `conflict`: `status=populated`, `value`/`evidence` from `conflict_details.candidates[i]`, `provenance=rag`, `conflict_details=null`, `confidence` 0.9 for typed fields and 0.8 otherwise (the candidates are verified) |
| `clear` | `status=missing`, `value=null`, `evidence=[]`, `missing_reason=cleared_by_user`, `provenance=user`, `confidence=0.0` |

`set_value` coerces the value by the field's `expected_type`: `text` needs a non-empty string; `paragraphs`/`bullet_list` take a string (one item) or a list of strings; `number`/`currency`/`percent`/`date` must canonicalize to exactly one value (`canonical_value`, so `1.829.914,84` and `30 junio 2022` work) and are stored canonical (number, ISO date), which the xlsx writer needs; `table` needs a list of rows.

Validation (`apply_edits`, pure): every op is checked before any is applied. An unknown `field_id`, the same field twice in one request, `choose_candidate` on a field that is not `conflict` or with an index out of range, or a value that does not coerce → `ValidationFailed` (422), and nothing is applied. Messages name the op index and the rule, never the value or field id.

Shared facts: fields that `map_run` maps once (same `share_key`, § 7) get the same edited result, so one edit changes the value on every slide. Naming two such fields in one request counts as a duplicate.

Applying an edit (`start_edit_run` in `api/v1/mapping_runs.py`, shared by the endpoint and the Pipe):
1. Load the parent run (tenant-scoped, `404` otherwise); it must be `completed` (`409`).
2. Copy its `results` and apply the ops.
3. Recompute `summary` with `summarize` (the parent's writer warnings are not carried over; rendering appends the new ones).
4. Insert a new `mapping_runs` row: `trigger='edit'`, `parent_run_id`, the parent's `document_ids`, `version = max(version for this template_ir_id) + 1` (retried once on a `UNIQUE` violation), `edits`, `status='rendering'`.
5. Enqueue `render_artifact`. The artifact is `{template stem}_v{version}.{ext}`; earlier artifacts stay downloadable.

The parent run is never modified.

**Chat parsing (`parse_edits`).** One JSON LLM call (prompt `llm/prompts/edits.md`) sees the run's field list (`field_id | label | status | current value`, clipped), each conflict's candidates (value and filename) numbered from 1, the same numbers the chat summary shows (the model's number minus 1 is the API's `candidate_index`), and the last user message. It returns flat ops `{op, field_id, value, candidate_index}`. An op missing its argument is rejected. A `set_value` value must appear in the last user message as whole words (`normalize`d, so a cut-down `1200` does not match `12000`), so the model can't invent a value the user never wrote ([ADR-013](DECISIONS.md#adr-013--chat-edits-take-values-only-from-the-users-message)). `shortcut: an edit always targets the latest completed run in the chat; add an explicit version reference ("edit v1") if users ask for it.`
