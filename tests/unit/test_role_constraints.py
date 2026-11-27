"""Phase 28: a verified number or date is rejected when the label bound to it in its quote states
the opposite direction (`direction_mismatch`) or an opening/prior role the slot does not
(`temporal_role_mismatch`); an opening slot drops running-balance cells once a labelled opening
value exists (`running_balance_cell`)."""

import uuid

from doc_intel.embeddings.schemas import Hit
from doc_intel.ingestion.schemas import TableRow
from doc_intel.mapping.engine import resolve
from doc_intel.mapping.verifier import bound_labels
from tests.unit.test_mapping_engine import DOCS, _cand, _ext, _field, _hit


def _slot(label, etype, context=None):
    return _field(etype).model_copy(update={"label": label, "context": context})


def _resolve(slot, hit, *cands):
    return resolve(slot, [hit], _ext(*cands), DOCS)


def _status(slot, hit, value, quote):
    r = _resolve(slot, hit, _cand(value, (hit, quote)))
    return r.status, r.value


COUNTS = "Credits: 4 | Debits: 9"
ES_COUNTS = "Abonos: 4 | Cargos: 9"
AMOUNTS = "Total deposits $1,500.00 | Total withdrawals $900.00"
DATES = "Opening date: 01 Jan 2025 | Closing date: 31 Jan 2025"


def test_bound_labels_are_the_text_up_to_the_neighbouring_number():
    assert [(b.strip(), a.strip()) for b, a in bound_labels(9, "number", [COUNTS])] == [
        ("Debits:", "")
    ]
    assert [(b.strip(), a.strip()) for b, a in bound_labels(4, "number", [COUNTS])] == [
        ("Credits:", "Debits:")
    ]


def test_an_inflow_count_slot_rejects_the_outflow_number():
    hit, slot = _hit(COUNTS), _slot("Number of credits", "number")
    assert _status(slot, hit, 9, COUNTS)[0] == "missing"
    assert _status(slot, hit, 4, COUNTS) == ("populated", 4)


def test_the_same_in_spanish_and_by_context():
    hit = _hit(ES_COUNTS)
    assert _status(_slot("Cantidad de abonos", "number"), hit, 9, ES_COUNTS)[0] == "missing"
    assert _status(_slot("Cantidad de abonos", "number"), hit, 4, ES_COUNTS) == ("populated", 4)
    ctx = _slot("Value", "number", "Deposits / Value")
    assert _status(ctx, _hit(COUNTS), 9, COUNTS)[0] == "missing"


def test_a_summary_currency_slot_rejects_the_opposite_amount():
    hit, slot = _hit(AMOUNTS), _slot("Total deposits", "currency")
    assert _status(slot, hit, 900, AMOUNTS)[0] == "missing"
    assert _status(slot, hit, 1500, AMOUNTS) == ("populated", 1500)


def test_a_slot_without_a_direction_is_unaffected():
    hit, slot = _hit(COUNTS), _slot("Customers", "number")
    assert _status(slot, hit, 9, COUNTS) == ("populated", 9)


def test_a_cited_number_with_no_direction_label_is_not_rejected():
    q = "Net 12 | Other 9 items"
    assert _status(_slot("Number of credits", "number"), _hit(q), 12, q) == ("populated", 12)


def test_an_after_label_is_used_when_nothing_precedes_the_number():
    q = "4 credits\n9 debits"
    hit, slot = _hit(q), _slot("Number of credits", "number")
    assert _status(slot, hit, 9, q)[0] == "missing"
    assert _status(slot, hit, 4, q) == ("populated", 4)


def test_a_period_date_slot_rejects_a_date_labelled_opening():
    hit, slot = _hit(DATES), _slot("Statement date", "date")
    assert _status(slot, hit, "2025-01-01", DATES)[0] == "missing"
    assert _status(slot, hit, "2025-01-31", DATES)[1] is not None
    assert _status(slot, hit, "2025-01-31", DATES)[0] == "populated"


def test_an_opening_date_slot_is_unaffected():
    hit, slot = _hit(DATES), _slot("Opening date", "date")
    assert _status(slot, hit, "2025-01-01", DATES)[0] == "populated"


OPENING = "Balance brought forward 4,200.00"
RUNNING = "| 03 Jan | Payment | 3,100.00 |"


def _ledger(*texts, roles=("carry_forward", "data")):
    rows = [
        TableRow(id=f"P1-T1-R{i:02d}", cells={"Text": t}, row_role=r)
        for i, (t, r) in enumerate(zip(texts, roles, strict=True), 1)
    ]
    return Hit(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        filename="ledger.pdf",
        page_number=1,
        section_heading=None,
        element_type="table",
        text="\n".join(texts),
        score=0.5,
        rows=rows,
    )


def test_an_opening_slot_drops_running_balance_cells_when_a_labelled_opening_exists():
    hit, slot = _ledger(OPENING, RUNNING), _slot("Opening balance", "currency")
    r = _resolve(slot, hit, _cand(3100, (hit, RUNNING)), _cand(4200, (hit, OPENING)))
    assert (r.status, r.value) == ("populated", 4200)


def test_a_carry_forward_row_counts_as_labelled_without_the_words():
    cf = "| Saldo | 4,200.00 |"
    hit = _ledger(cf, RUNNING)
    r = _resolve(
        _slot("Opening balance", "currency"),
        hit,
        _cand(3100, (hit, RUNNING)),
        _cand(4200, (hit, cf)),
    )
    assert (r.status, r.value) == ("populated", 4200)


def test_without_a_labelled_opening_nothing_is_dropped():
    hit = _ledger(RUNNING, "| 05 Jan | Fee | 2,000.00 |", roles=("data", "data"))
    r = _resolve(
        _slot("Opening balance", "currency"),
        hit,
        _cand(3100, (hit, RUNNING)),
        _cand(2000, (hit, "| 05 Jan | Fee | 2,000.00 |")),
    )
    assert r.status == "conflict"


def test_two_labelled_openings_stay_a_conflict():
    other = "Balance brought forward 4,300.00"
    hit = _ledger(OPENING, other, roles=("carry_forward", "carry_forward"))
    r = _resolve(
        _slot("Opening balance", "currency"),
        hit,
        _cand(4200, (hit, OPENING)),
        _cand(4300, (hit, other)),
    )
    assert r.status == "conflict"


def test_context_direction_counts_only_for_a_bare_label():
    text = "Closing balance after debits: 4,100.00"
    hit, slot = _hit(text), _slot("Closing balance", "currency", "Credits this month")
    assert _status(slot, hit, 4100, text) == ("populated", 4100)


def test_a_header_named_opening_cell_and_a_prose_opening_stay_a_conflict():
    row = "| 03 Jan | 3,100.00 |"
    ledger = Hit(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        filename="l.pdf",
        page_number=1,
        section_heading=None,
        element_type="table",
        text=row,
        score=0.5,
        rows=[TableRow(id="P1-T1-R01", cells={"Date": "03 Jan", "Previous balance": "3,100.00"})],
    )
    prose = _hit("Previous balance 4,200.00")
    slot = _slot("Opening balance", "currency")
    r = resolve(
        slot,
        [ledger, prose],
        _ext(_cand(3100, (ledger, row)), _cand(4200, (prose, "Previous balance 4,200.00"))),
        DOCS,
    )
    assert r.status == "conflict"
