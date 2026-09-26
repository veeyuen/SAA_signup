from pathlib import Path

import pandas as pd

from signup.hytek_export import (
    E_FIELD_COUNT,
    I_FIELD_COUNT,
    build_hytek_text,
    build_legacy_output_hytek_export,
    build_transactional_hytek_exports,
    export_event_code,
)


FIXTURES = Path(__file__).parent / "fixtures"


def _split(line: str):
    # Canonical files use '; ' but split on ';' so blank trailing fields remain.
    return [part.strip() for part in line.rstrip("\n").split(";")]


def _golden_transaction_rows():
    i_lines = (FIXTURES / "current_entries_I_semicolon_delimited.txt").read_text().splitlines()
    e_lines = (FIXTURES / "current_entries_E_semicolon_delimited.txt").read_text().splitlines()
    assert len(i_lines) == len(e_lines)

    entries = []
    registrations = []
    for idx, (i_line, e_line) in enumerate(zip(i_lines, e_lines), start=1):
        i = _split(i_line)
        e = _split(e_line)
        reg_id = f"REG-GOLD-{idx}"
        entry_id = f"ENT-GOLD-{idx}"
        registration = {
            "REGISTRATION_ID": reg_id,
            "ORDER_ID": f"ORD-GOLD-{idx}",
            "COMPETITION_ID": "COMP_GOLDEN",
            "ATHLETE_ID": i[21],
            "ORGANIZATION_ID": "ORG_TEST",
            "DIVISION": e[14],
            "STATUS": "CONFIRMED",
            "IS_DELETED": "FALSE",
            "ATHLETE_NAME": f"{i[2]} {i[1]}".strip(),
            "DOB": i[5],
            "GENDER": i[4],
            "NATIONALITY": i[16],
            "TEAM_CODE": i[6],
            "TEAM_NAME": i[7],
            "FIRST_NAME": i[2],
            "OTHER_NAME": "",
            "LAST_NAME": i[1],
        }
        entry = {
            "ENTRY_ID": entry_id,
            "REGISTRATION_ID": reg_id,
            "ORDER_ID": f"ORD-GOLD-{idx}",
            "COMPETITION_ID": "COMP_GOLDEN",
            "ATHLETE_ID": i[21],
            "ORGANIZATION_ID": "ORG_TEST",
            "EVENT_NAME": e[10] + ("m" if e[10].isdigit() else ""),
            "EVENT_CODE": e[10],
            "DIVISION": e[14],
            "SEASON_BEST": e[11],
            "STATUS": "CONFIRMED",
            "IS_DELETED": "FALSE",
            "ATHLETE_NAME": f"{i[2]} {i[1]}".strip(),
            "DOB": e[5],
            "GENDER": e[4],
            "NATIONALITY": i[16],
            "TEAM_CODE": e[6],
            "TEAM_NAME": e[7],
            "FIRST_NAME": i[2],
            "OTHER_NAME": "",
            "LAST_NAME": i[1],
        }
        registrations.append(registration)
        entries.append(entry)
    return entries, registrations


def _entry(**overrides):
    row = {
        "ENTRY_ID": "ENT-1",
        "REGISTRATION_ID": "REG-1",
        "ORDER_ID": "ORD-1",
        "COMPETITION_ID": "COMP-1",
        "ATHLETE_ID": "A123",
        "ORGANIZATION_ID": "ORG-1",
        "EVENT_NAME": "100m",
        "EVENT_CODE": "100",
        "DIVISION": "U18",
        "SEASON_BEST": "11.23",
        "STATUS": "CONFIRMED",
        "IS_DELETED": "FALSE",
        "ATHLETE_NAME": "John Tan",
        "DOB": "2010-10-10",
        "GENDER": "Male",
        "NATIONALITY": "Singapore",
        "TEAM_CODE": "TAC",
        "TEAM_NAME": "Test Athletics Club",
        "FIRST_NAME": "John",
        "OTHER_NAME": "",
        "LAST_NAME": "Tan",
    }
    row.update(overrides)
    return row


def _registration(**overrides):
    row = {
        "REGISTRATION_ID": "REG-1",
        "ORDER_ID": "ORD-1",
        "COMPETITION_ID": "COMP-1",
        "ATHLETE_ID": "A123",
        "ORGANIZATION_ID": "ORG-1",
        "DIVISION": "U18",
        "STATUS": "CONFIRMED",
        "IS_DELETED": "FALSE",
        "ATHLETE_NAME": "John Tan",
        "DOB": "2010-10-10",
        "GENDER": "Male",
        "NATIONALITY": "Singapore",
        "TEAM_CODE": "TAC",
        "TEAM_NAME": "Test Athletics Club",
        "FIRST_NAME": "John",
        "OTHER_NAME": "",
        "LAST_NAME": "Tan",
    }
    row.update(overrides)
    return row


def test_golden_i_export_matches_attached_canonical_file_exactly():
    entries, registrations = _golden_transaction_rows()
    bundle = build_transactional_hytek_exports(
        entries, registrations, competition_id="COMP_GOLDEN"
    )
    expected = (FIXTURES / "current_entries_I_semicolon_delimited.txt").read_text()
    assert bundle.i_text == expected


def test_golden_e_export_matches_attached_canonical_file_exactly():
    entries, registrations = _golden_transaction_rows()
    bundle = build_transactional_hytek_exports(
        entries, registrations, competition_id="COMP_GOLDEN"
    )
    expected = (FIXTURES / "current_entries_E_semicolon_delimited.txt").read_text()
    assert bundle.e_text == expected


def test_canonical_field_counts_are_fixed():
    entries, registrations = _golden_transaction_rows()
    bundle = build_transactional_hytek_exports(entries, registrations)
    assert all(len(line.split(";")) == I_FIELD_COUNT for line in bundle.i_text.splitlines())
    assert all(len(line.split(";")) == E_FIELD_COUNT for line in bundle.e_text.splitlines())


def test_confirmed_active_entry_is_exported():
    bundle = build_transactional_hytek_exports([_entry()], [_registration()])
    assert bundle.exported_count == 1
    assert bundle.excluded_count == 0
    assert bundle.i_text.startswith("I; Tan; John;")
    assert bundle.e_text.startswith("E; Tan; John;")


def test_pending_payment_entry_is_excluded_even_with_valid_parent():
    bundle = build_transactional_hytek_exports(
        [_entry(STATUS="PENDING_PAYMENT")], [_registration()]
    )
    assert bundle.exported_count == 0
    assert bundle.excluded_entry_ids == ("ENT-1",)


def test_withdrawn_or_deleted_entry_is_excluded():
    withdrawn = build_transactional_hytek_exports(
        [_entry(STATUS="WITHDRAWN")], [_registration()]
    )
    deleted = build_transactional_hytek_exports(
        [_entry(IS_DELETED="TRUE")], [_registration()]
    )
    assert withdrawn.exported_count == 0
    assert deleted.exported_count == 0


def test_parent_registration_must_be_confirmed_and_active():
    bundle = build_transactional_hytek_exports(
        [_entry()], [_registration(STATUS="WITHDRAWN", IS_DELETED="TRUE")]
    )
    assert bundle.exported_count == 0
    assert any(d.code == "PARENT_NOT_EXPORTABLE" for d in bundle.diagnostics)


def test_orphan_event_entry_is_excluded_and_reported():
    bundle = build_transactional_hytek_exports([_entry()], [])
    assert bundle.exported_count == 0
    assert any(d.code == "ORPHAN_REGISTRATION" and d.level == "ERROR" for d in bundle.diagnostics)


def test_competition_filter_only_exports_selected_competition():
    entries = [
        _entry(ENTRY_ID="ENT-1", REGISTRATION_ID="REG-1", COMPETITION_ID="COMP-1"),
        _entry(ENTRY_ID="ENT-2", REGISTRATION_ID="REG-2", COMPETITION_ID="COMP-2"),
    ]
    regs = [
        _registration(REGISTRATION_ID="REG-1", COMPETITION_ID="COMP-1"),
        _registration(REGISTRATION_ID="REG-2", COMPETITION_ID="COMP-2"),
    ]
    bundle = build_transactional_hytek_exports(entries, regs, competition_id="COMP-2")
    assert bundle.exported_entry_ids == ("ENT-2",)


def test_transaction_sheet_order_is_preserved():
    entries = [
        _entry(ENTRY_ID="ENT-B", REGISTRATION_ID="REG-B", FIRST_NAME="Second"),
        _entry(ENTRY_ID="ENT-A", REGISTRATION_ID="REG-A", FIRST_NAME="First"),
    ]
    regs = [
        _registration(REGISTRATION_ID="REG-B", FIRST_NAME="Second"),
        _registration(REGISTRATION_ID="REG-A", FIRST_NAME="First"),
    ]
    bundle = build_transactional_hytek_exports(entries, regs)
    assert bundle.exported_entry_ids == ("ENT-B", "ENT-A")
    assert "Second" in bundle.i_text.splitlines()[0]
    assert "First" in bundle.i_text.splitlines()[1]


def test_legacy_output_enriches_historical_structured_names_by_entry_id():
    entry = _entry(FIRST_NAME="", LAST_NAME="", ATHLETE_NAME="DisplayOnly")
    reg = _registration(FIRST_NAME="", LAST_NAME="", ATHLETE_NAME="DisplayOnly")
    output = pd.DataFrame(
        [{
            "entry_id": "ENT-1",
            "registration_id": "REG-1",
            "first_name": "Given",
            "last_name": "Surname",
        }]
    )
    bundle = build_transactional_hytek_exports(
        [entry], [reg], legacy_output_rows=output
    )
    assert bundle.i_text.startswith("I; Surname; Given;")
    assert not any(d.code == "NAME_FALLBACK" for d in bundle.diagnostics)


def test_name_fallback_is_visible_as_diagnostic():
    entry = _entry(FIRST_NAME="", LAST_NAME="", ATHLETE_NAME="John Tan")
    reg = _registration(FIRST_NAME="", LAST_NAME="", ATHLETE_NAME="John Tan")
    bundle = build_transactional_hytek_exports([entry], [reg])
    assert bundle.i_text.startswith("I; Tan; John;")
    assert any(d.code == "NAME_FALLBACK" for d in bundle.diagnostics)


def test_event_code_derivation_matches_established_codes():
    assert export_event_code("100m", "74") == "100"
    assert export_event_code("100m Hurdles", "") == "100H"
    assert export_event_code("Long Jump", "") == "LJ"
    assert export_event_code("Javelin Throw", "") == "JT"
    assert export_event_code("4 x 100m Relay", "") == "100"
    # Explicit current transaction code takes precedence when non-numeric.
    assert export_event_code("Long Jump", "LJ") == "LJ"


def test_dob_is_rendered_dd_mm_yyyy():
    text = build_hytek_text([_entry()], "I")
    assert "; 10/10/2010;" in text


def test_semicolon_and_newline_in_source_values_do_not_break_layout():
    row = _entry(FIRST_NAME="John;Danger\nName")
    text = build_hytek_text([row], "I")
    line = text.splitlines()[0]
    assert len(line.split(";")) == I_FIELD_COUNT
    assert "John Danger Name" in line


def test_legacy_output_compatibility_renderer_keeps_established_layout():
    output = pd.DataFrame(
        [{
            "first_name": "John",
            "last_name": "Tan",
            "gender": "Male",
            "birth_date": "2010-10-10",
            "team_code": "TAC",
            "team_name": "Test Athletics Club",
            "nationality": "Singapore",
            "unique_id": "A123",
            "event": "100m",
            "event_code": "100",
            "season_best": "11.23",
            "event_division": "U18",
        }]
    )
    i_text = build_legacy_output_hytek_export(output, "I")
    e_text = build_legacy_output_hytek_export(output, "E")
    assert len(i_text.splitlines()[0].split(";")) == I_FIELD_COUNT
    assert len(e_text.splitlines()[0].split(";")) == E_FIELD_COUNT
    assert i_text.startswith("I; Tan; John;")
    assert "; 100; 11.23; ; M; U18; ; " in e_text


def test_legacy_compatibility_excludes_withdrawn_and_deleted_rows():
    output = pd.DataFrame(
        [
            {"first_name": "Keep", "last_name": "One", "entry_status": "CONFIRMED"},
            {"first_name": "Drop", "last_name": "Two", "entry_status": "WITHDRAWN"},
            {"first_name": "Drop", "last_name": "Three", "is_deleted": "TRUE"},
        ]
    )
    text = build_legacy_output_hytek_export(output, "I")
    assert "Keep" in text
    assert "Two" not in text
    assert "Three" not in text


def test_invalid_record_type_is_rejected():
    try:
        build_hytek_text([_entry()], "X")
    except ValueError as exc:
        assert "record_type" in str(exc)
    else:
        raise AssertionError("Expected ValueError")
