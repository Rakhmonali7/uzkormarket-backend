"""HTTP API surface — versioned subpackages live here.

Currently only ``api.v1`` exists. When a v2 lands its own subpackage will
sit alongside v1, with its own router aggregator, and ``main.create_app``
will mount both under their respective prefixes.
"""
