"""Refusals the caller can read, rather than failures it will paper over."""

from __future__ import annotations


class ResearchRefused(ValueError):
    """A write the workspace declines, with a machine-readable ``code``.

    Every refusal in this package carries a code so an MCP tool can hand the
    agent the reason verbatim instead of a stack trace, and so a test can
    assert *which* rule fired rather than that something went wrong.
    """

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class ResearchWorkspaceDisabled(ResearchRefused):
    """``RESEARCH_WORKSPACE_ENABLED`` is false. False means false."""

    def __init__(self, detail: str = ""):
        super().__init__(
            "research_workspace_disabled",
            detail
            or "RESEARCH_WORKSPACE_ENABLED is false; the research workspace is "
            "not serving reads or writes.",
        )
