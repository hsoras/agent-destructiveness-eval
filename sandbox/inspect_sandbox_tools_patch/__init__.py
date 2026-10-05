"""Pinned source CLI bootstrap for the vendored Inspect 1.2.1 package."""

# The generated standalone Inspect bundle normally supplies dist-info metadata.
# This source invocation uses the exact same pinned package outside a wheel, so
# keep its version available without querying installed package metadata.
__version__ = "1.2.1"
__all__ = ["__version__"]
