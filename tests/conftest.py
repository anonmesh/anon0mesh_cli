"""
conftest.py — pytest configuration
====================================
Adds the project root to sys.path so all project modules are importable
without installation. Also resets shared mutable state between tests.
"""

import sys
import os
import pytest

# Project root is one level up from this file
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import state
import shared


@pytest.fixture(autouse=True)
def reset_state():
    """Restore shared mutable state after every test."""
    original_wallet = state.active_wallet
    original_pool   = state.pool
    yield
    state.active_wallet = original_wallet
    state.pool          = original_pool
    shared.set_quiet(False)
