"""Tests for apb action."""

# Standard Python Libraries
import os

# Third-Party Libraries
import pytest

# cisagov Libraries
import apb
import apb.entrypoint

# define sources of version strings
PROJECT_VERSION = apb.__version__
RELEASE_TAG = os.getenv("RELEASE_TAG")


def test_unset_ca_variables():
    """Test that CA variables are unset."""
    os.environ["REQUESTS_CA_BUNDLE"] = "test"
    apb.entrypoint.clear_ca_variables_in_gha()
    assert "REQUESTS_CA_BUNDLE" not in os.environ


@pytest.mark.skipif(
    RELEASE_TAG in [None, ""], reason="this is not a release (RELEASE_TAG not set)"
)
def test_release_version():
    """Verify that release tag version agrees with the module version."""
    assert (
        RELEASE_TAG == f"v{PROJECT_VERSION}"
    ), "RELEASE_TAG does not match the project version"
