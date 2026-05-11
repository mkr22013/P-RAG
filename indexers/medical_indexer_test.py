"""
Test suite for medical_indexer.parse_benefit_cell.

Run with:  python test_medical_indexer.py
All tests must pass before deploying a changed medical_indexer.py.

Each test documents WHY the case exists so future developers understand
what behaviour is being protected.
"""

import sys, re, types
from unittest.mock import MagicMock

# Stub out dependencies so we can import medical_indexer without a full env
sys.modules["ollama"] = MagicMock()
sys.modules["dotenv"] = MagicMock()
sys.modules["pdfplumber"] = MagicMock()

utils_mock = MagicMock()
utils_mock.get_smart_keywords = lambda t: []
sys.modules["utils"] = utils_mock

sys.path.insert(0, ".")
import medical_indexer as mi

# ─── helpers ──────────────────────────────────────────────────────────────────


def run(cell):
    benefit, services, notes = mi.parse_benefit_cell(cell)
    return benefit, services, notes


def svc_names(services):
    return [s[0] for s in services]


def svc_notes(services):
    return [s[1] for s in services]


def svc_contexts(services):
    return [s[2] for s in services]


# ─── tests ────────────────────────────────────────────────────────────────────


def test_acupuncture_flat():
    """
    Flat structure: no sub-groups, one continuation note that belongs to
    'Office and Clinic Visits' (calendar year limit) — should be a leaf service,
    NOT a sub-group. Both bullets are leaf services.
    """
    cell = (
        "Acupuncture\n"
        "• Office and Clinic Visits\n"
        "calendar year visit limit: 24 visits\n"
        "• Visits outside an office setting"
    )
    benefit, services, _ = run(cell)
    assert benefit == "Acupuncture"
    assert svc_names(services) == [
        "Office and Clinic Visits",
        "Visits outside an office setting",
    ]
    assert not any(svc_contexts(services)), "No sub-group context expected"


def test_emergency_room_per_service_notes():
    """
    'Facility charges' has 'You may have...' and 'The copay is waived...'
    continuation notes — these are limitations for THAT service only.
    'Professional services' has no notes → limitations will be 'Data Not Found'.
    """
    cell = (
        "Emergency Room\n"
        "• Facility charges\n"
        "You may have additional costs for other services.\n"
        "The copay is waived if you are admitted as an inpatient.\n"
        "• Professional services"
    )
    benefit, services, _ = run(cell)
    assert benefit == "Emergency Room"
    assert svc_names(services) == ["Facility charges", "Professional services"]
    assert "You may" in svc_notes(services)[0]  # Facility charges HAS note
    assert svc_notes(services)[1] == ""  # Professional services has NONE


def test_medical_transportation_dollar_services_and_subgroup():
    """
    Non-bullet bold lines with '$' are services (For transplants, For cellular).
    '• For total hip...' is a sub-group label — skipped as a service but its
    text becomes the sub-group CONTEXT for the two To/from bullets under it.
    This context goes into the TOPIC (not limitations) so searches for
    'hip replacement travel' find the To/from entries.
    '• For total hip' is skipped via the 'starts with For' rule.
    """
    cell = (
        "Medical Transportation\n"
        "Travel (for Washington and Alaska\n"
        "members)\n"
        "For transplants: limit per\n"
        "transplant: $7,500\n"
        "For surgeries covered under the\n"
        "Premera-Designated Centers of\n"
        "Excellence benefit.\n"
        "• For total hip and knee joint\n"
        "replacements and certain spinal\n"
        "surgeries.\n"
        "• To/from a Premera Designated\n"
        "Center of Excellence\n"
        "• To/from other providers\n"
        "For cellular immunotherapy and\n"
        "gene therapy: $7,500 per episode\n"
        "of care"
    )
    benefit, services, _ = run(cell)
    assert benefit == "Medical Transportation"
    names = svc_names(services)
    assert len(names) == 4
    assert "For transplants: limit per transplant: $7,500" in names[0]
    assert "To/from a Premera Designated Center of Excellence" in names[1]
    assert "To/from other providers" in names[2]
    assert "For cellular immunotherapy" in names[3]
    # Sub-group context attached to both To/from services
    assert "total hip" in svc_contexts(services)[1].lower()
    assert "total hip" in svc_contexts(services)[2].lower()
    # Dollar-line services have no sub-group context
    assert svc_contexts(services)[0] == ""
    assert svc_contexts(services)[3] == ""


def test_dental_injury_cross_ref_skipped():
    """
    '• Dental Anesthesia (See Dental Injury...)' has a cross-reference →
    skipped as a sub-section header. All remaining bullets are leaf services.
    'Dental Injury' (bold, no cross-ref) appears as a service because we removed
    bold detection (it caused Premera COE to go missing entirely).
    5 leaf services expected.
    """
    cell = (
        "Dental Injury and Facility\n"
        "Anesthesia\n"
        "• Dental Anesthesia (See Dental\n"
        "Injury benefit for details.)\n"
        "• Inpatient facility care\n"
        "• Outpatient surgery center\n"
        "• Anesthesiologist\n"
        "• Dental Injury\n"
        "• Exams to determine treatment\n"
        "needed"
    )
    benefit, services, _ = run(cell)
    assert benefit == "Dental Injury and Facility Anesthesia"
    assert len(services) == 5
    assert svc_names(services)[0] == "Inpatient facility care"
    assert svc_names(services)[4] == "Exams to determine treatment needed"


def test_neurodevelopmental_outpatient_care_subgroup():
    """
    '• Outpatient care' has a limit continuation AND the next line is a bullet
    AND its name is a GENERIC CATEGORY ('outpatient care') → treated as sub-group,
    skipped. Office/Other outpatient/Inpatient care are the leaf services.
    'Inpatient care' has a limit continuation but NO next bullet → leaf service.
    """
    cell = (
        "Neurodevelopmental\n"
        "(Habilitation) Therapy\n"
        "See the Mental Health Care benefit\n"
        "• Outpatient care\n"
        "calendar year visit limit: 45 visits\n"
        "• Office and clinic visits\n"
        "• Other outpatient services\n"
        "• Inpatient care\n"
        "calendar year day limit: 30 days"
    )
    benefit, services, _ = run(cell)
    assert benefit == "Neurodevelopmental (Habilitation) Therapy"
    assert svc_names(services) == [
        "Office and clinic visits",
        "Other outpatient services",
        "Inpatient care",
    ]


def test_rehabilitation_outpatient_subgroup_inpatient_leaf():
    """
    Same pattern as Neurodevelopmental:
    'Outpatient Care' → sub-group (limit + next bullet + generic name) → skipped.
    'Inpatient Care' → leaf (limit + NO next bullet) → kept.
    """
    cell = (
        "Rehabilitation Therapy\n"
        "• Outpatient Care\n"
        "calendar year visit limit: 45 visits\n"
        "No limit for cardiac or pulmonary.\n"
        "• Office and clinic visits\n"
        "• Other outpatient services\n"
        "• Inpatient Care\n"
        "calendar year day limit: 30 days"
    )
    benefit, services, _ = run(cell)
    assert benefit == "Rehabilitation Therapy"
    assert svc_names(services) == [
        "Office and clinic visits",
        "Other outpatient services",
        "Inpatient Care",
    ]


def test_premera_coe_bariatric_not_missing():
    """
    'Certain Spine Surgeries...' is a BOLD bullet but bold detection was
    intentionally removed because it caused this entire section to vanish.
    Both bullets must appear as services.
    The description after 'Certain Spine Surgeries' is not a limit note so
    it gets appended to the service name (acceptable — LLM handles context).
    """
    cell = (
        "Premera-Designated Centers Of\n"
        "Excellence Program\n"
        "Includes travel as needed.\n"
        "• Certain Spine Surgeries And\n"
        "Total Knee And Hip Joint\n"
        "Replacements\n"
        "For hip, knee, and spinal surgery by\n"
        "providers other than designated\n"
        "centers of excellence, see Hospital.\n"
        "• Bariatric Surgery\n"
        "Special criteria are required for\n"
        "coverage under this benefit."
    )
    benefit, services, _ = run(cell)
    assert benefit == "Premera-Designated Centers Of Excellence Program"
    assert len(services) == 2
    assert "Certain Spine Surgeries" in svc_names(services)[0]
    assert svc_names(services)[1] == "Bariatric Surgery"


def test_hearing_care_limit_dollar_not_service():
    """
    'Limit $3,000 per ear...' has a dollar sign but starts with 'Limit $' →
    matched by LIMIT_DOLLAR pattern → NOT collected as a service.
    Only 'Hearing Exams' and 'Hearing Hardware' are services.
    """
    cell = (
        "Hearing Care\n"
        "For hearing loss, often due to age\n"
        "or noise exposure.\n"
        "• Hearing Exams\n"
        "Limit each calendar year: 1\n"
        "exam/test\n"
        "(Copay does not apply.)\n"
        "• Hearing Hardware\n"
        "Limit $3,000 per ear with hearing\n"
        "loss every 36 months"
    )
    benefit, services, _ = run(cell)
    assert benefit == "Hearing Care"
    assert svc_names(services) == ["Hearing Exams", "Hearing Hardware"]


def test_diagnostic_xray_no_charge_note_not_service():
    """
    'No charge on certain laboratory services...' follows the last bullet as a
    trailing note. The continuation loop breaks on 'no charge on' pattern so
    it is NOT appended to 'Diagnostic and supplemental breast exams'.
    3 clean service names expected.
    """
    cell = (
        "Diagnostic X-Ray, Lab, And\n"
        "Imaging for medical conditions\n"
        "• Basic diagnostic images and scans\n"
        "• Major diagnostic images and\n"
        "scans\n"
        "• Diagnostic and supplemental\n"
        "breast exams\n"
        "No charge on certain laboratory\n"
        "services that are provided by a\n"
        "Kinwell clinic."
    )
    benefit, services, _ = run(cell)
    assert benefit == "Diagnostic X-Ray, Lab, And Imaging for medical conditions"
    assert svc_names(services) == [
        "Basic diagnostic images and scans",
        "Major diagnostic images and scans",
        "Diagnostic and supplemental breast exams",
    ]


def test_clinical_trials_benefit_level_note():
    """
    No bullet services — 'Clinical Trials' itself is the single service.
    'Covers routine patient care...' and 'You may have additional costs...'
    are benefit-level notes captured in the notes list (for limitations field).
    """
    cell = (
        "Clinical Trials\n"
        "Covers routine patient care during\n"
        "the trial\n"
        "You may have additional costs for\n"
        "other services such as x-rays."
    )
    benefit, services, notes = run(cell)
    assert benefit == "Clinical Trials"
    assert (
        len(services) == 0
    )  # no bullets → single-service handled in generate_sub_index
    assert len(notes) == 1
    assert "you may" in notes[0].lower()
    assert "covers routine" in notes[0].lower()


# ─── runner ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        test_acupuncture_flat,
        test_emergency_room_per_service_notes,
        test_medical_transportation_dollar_services_and_subgroup,
        test_dental_injury_cross_ref_skipped,
        test_neurodevelopmental_outpatient_care_subgroup,
        test_rehabilitation_outpatient_subgroup_inpatient_leaf,
        test_premera_coe_bariatric_not_missing,
        test_hearing_care_limit_dollar_not_service,
        test_diagnostic_xray_no_charge_note_not_service,
        test_clinical_trials_benefit_level_note,
    ]

    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✓ {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  ✗ {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ✗ {t.__name__}: UNEXPECTED ERROR: {e}")
            failed += 1

    print(f"\n{passed}/{passed+failed} passed", "✓" if failed == 0 else "✗")
    sys.exit(0 if failed == 0 else 1)
