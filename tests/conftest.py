import pytest
from router import POLICY_PATH, Policy, load_policy


@pytest.fixture
def policy() -> Policy:
    return load_policy(POLICY_PATH)
