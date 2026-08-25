
import json
import pathlib
from docagent.graph import n_load_profile, Deps
from docagent.profiles import PROFILE_DIR, Profile

# Ensure the test directory for profiles exists
PROFILE_DIR.mkdir(exist_ok=True)

DUMMY_FINGERPRINT = "test_fingerprint_force_tune"
DUMMY_SLUG = f"dummy-profile-{DUMMY_FINGERPRINT}"
dummy_profile_path = PROFILE_DIR / f"{DUMMY_SLUG}.json"

def test_force_tune_skips_profile_loading():
    # 1. Setup: Create a dummy profile that would normally be loaded.
    dummy_profile = Profile(
        fingerprint=DUMMY_FINGERPRINT,
        slug=DUMMY_SLUG,
        learned_from="some/dummy/file.pdf",
        extractor="pdf_text",
    )
    dummy_profile.save()
    assert dummy_profile_path.exists()

    # 2. State: The input to the node, with force_tune=True
    state = {
        "fingerprint": DUMMY_FINGERPRINT,
        "force_tune": True,
        "path": "a/new/path.pdf"
    }

    try:
        # 3. Execution: Call the node function
        result = n_load_profile(state, Deps())

        # 4. Assertion: Check that no profile was loaded.
        assert result.get("profile") is None, "Profile should not be loaded when force_tune is True"
        assert result.get("profile_source") == "default", "Profile source should be default"
        assert "force-tune" in result["log"][0], "Log should mention force-tune"

    finally:
        # Cleanup
        if dummy_profile_path.exists():
            dummy_profile_path.unlink()
