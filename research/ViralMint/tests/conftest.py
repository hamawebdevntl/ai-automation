# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Shared test configuration — runs before any test module imports."""
import os
from cryptography.fernet import Fernet

# Generate a valid Fernet key for all tests
_TEST_KEY = Fernet.generate_key().decode()
os.environ.setdefault("ENCRYPTION_KEY", _TEST_KEY)
