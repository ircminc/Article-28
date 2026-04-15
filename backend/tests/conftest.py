"""Shared pytest fixtures / configuration."""
import pytest

# Configure pytest-asyncio to treat all async tests as asyncio without requiring
# the @pytest.mark.asyncio decorator (we still include the marker for clarity).
def pytest_collection_modifyitems(config, items):
    pass


# Make asyncio mode strict so missing decorators fail loudly.
# (Set in pyproject / pytest.ini; left here as a comment for clarity.)
