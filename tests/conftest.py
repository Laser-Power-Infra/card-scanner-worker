"""Test-wide defaults.

The temporary address cache must never leak state between tests. Production
runs default to ADDRESS_CACHE=1; the suite explicitly turns it off before any
app module reads the environment.
"""
import os

os.environ["ADDRESS_CACHE"] = "0"
