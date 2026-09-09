"""xyne-cli running on its NATIVE (Cordis plugin-kernel) engine.

Separate from `xyne_harbor_agent` on purpose: the two engines write
incompatible session-log formats, so they cannot share a token reader.
"""
