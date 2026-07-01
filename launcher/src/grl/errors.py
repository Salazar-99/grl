"""Launcher-specific exceptions."""


class GrlError(Exception):
    """Base error for GRL launcher failures."""


class PreflightError(GrlError):
    """Preflight validation failed."""


class ToolError(GrlError):
    """Managed tool installation or execution failed."""


class TerraformError(GrlError):
    """Terraform operation failed."""


class KubernetesError(GrlError):
    """Kubernetes or Helm operation failed."""
