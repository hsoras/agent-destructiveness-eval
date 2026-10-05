# Vendored Inspect sandbox tools

This source is from the `src/inspect_sandbox_tools/src/inspect_sandbox_tools`
directory in the Inspect AI 0.3.263 source distribution. The pinned archive is
`inspect_ai-0.3.263.tar.gz`, SHA-256
`54553ca8bfe711853414b49d962e492a60fd4cac8df935be47287a4b719f92b1`.
The upstream MIT license is included in `LICENSE`.

The task image overlays the pinned files in `sandbox/inspect_sandbox_tools_patch`
to bootstrap the source checkout without wheel metadata, record service/job
lifecycle events, and keep terminal job results available for retrying a lost
terminal poll response. The rest of the package is unchanged.
