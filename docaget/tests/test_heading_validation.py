
from docagent.rules import _check_heading_guards, Proposal, Validation
from docagent.chunk import ChunkRules

def test_non_contiguous_headings_fail_validation():
    # This simulates the proposal and lines that produce the bad sequence [2, 5, 6, 7]
    proposal = Proposal(heading_l1_max=60, heading_l2_max=70)
    lines = [
        "2. PAPADO, MONAQUISMO E IMPERIO MUSULMÁN Y",
        "Some other text",
        "5. EL PAPADO",
        "Some other text",
        "6. EL DESARROLLO DEL MONACATO.",
        "Some other text",
        "7. EL SURGIMIENTO DEL PODERÍO MUSULMÁN"
    ]
    
    validation = Validation()
    _check_heading_guards(proposal, validation, lines)

    # The finding should be about non-contiguous numbers
    finding = validation.findings[0]
    assert not finding.ok
    assert "not contiguous" in finding.detail

def test_contiguous_headings_starting_late_pass_validation():
    # A book's chapters might correctly start at a number > 1
    proposal = Proposal(heading_l1_max=60, heading_l2_max=70)
    lines = [
        "5. EL PAPADO",
        "Some other text",
        "6. EL DESARROLLO DEL MONACATO.",
        "Some other text",
        "7. EL SURGIMIENTO DEL PODERÍO MUSULMÁN"
    ]
    
    validation = Validation()
    _check_heading_guards(proposal, validation, lines)

    finding = validation.findings[0]
    assert finding.ok, "Validation should pass for a contiguous sequence like [5, 6, 7]"

