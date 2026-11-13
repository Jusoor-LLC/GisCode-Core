"""First-run onboarding for GisCode Core.

The product used to assume the user had already installed a model runtime,
pulled a model and picked it by hand. This package turns that assumption into a
guided, resumable sequence that ends only when **a real local coding task has
succeeded** on this machine.

Design rules, all of them enforced here rather than in documentation:

* **Nothing is installed or downloaded without an explicit, recorded consent.**
  Ollama is the user's own installation; GisCode opens its official page and
  waits, it never runs an installer silently.
* **No model is substituted.** A recommendation names one tag, and that tag is
  what gets pulled, validated and configured.
* **Progress is honest.** A pull that was interrupted stays interrupted until
  the user retries; an inference test that failed is never shown as ready.
* **Ollama owns its blobs.** Downloads are driven by ``ollama pull``, so the
  transfer resumes on Ollama's blob store rather than being faked by GisCode.

The public surface is :class:`OnboardingService` for logic and
:func:`open_wizard_if_needed` for the GUI entry point.
"""
from __future__ import annotations

from giscode.onboarding.service import OnboardingService
from giscode.onboarding.state import OnboardingState, OnboardingStore, STAGES

__all__ = ["OnboardingService", "OnboardingState", "OnboardingStore", "STAGES", "open_wizard_if_needed"]


def open_wizard_if_needed(root, *, settings=None, model_manager=None):
    """Show the wizard for an unfinished first run, and do nothing otherwise.

    Kept import-light: the Tk wizard module is only imported when it is actually
    going to be shown, so a headless or already-onboarded launch pays nothing.
    """
    from giscode.onboarding.state import OnboardingStore

    state = OnboardingStore().load()
    if state.completed or state.legacy:
        return None
    from giscode.gui.onboarding_wizard import OnboardingWizard

    return OnboardingWizard(root, settings=settings, model_manager=model_manager)
