"""Installed, versioned system prompts for the profile proxy."""
from hashlib import sha256
from pathlib import Path

_ROOT = Path(__file__).with_name("prompts")
PROPOSER_PROMPT_VERSION = "profile-proposer.v1"
REVIEWER_PROMPT_VERSION = "profile-reviewer.v1"
PROPOSER_SYSTEM_PROMPT = (_ROOT / (PROPOSER_PROMPT_VERSION + ".txt")).read_text()
REVIEWER_SYSTEM_PROMPT = (_ROOT / (REVIEWER_PROMPT_VERSION + ".txt")).read_text()
PROPOSER_PROMPT_SHA256 = sha256(PROPOSER_SYSTEM_PROMPT.encode()).hexdigest()
REVIEWER_PROMPT_SHA256 = sha256(REVIEWER_SYSTEM_PROMPT.encode()).hexdigest()

def prompt_receipt():
    return {
        "proposer": {"version": PROPOSER_PROMPT_VERSION, "sha256": PROPOSER_PROMPT_SHA256},
        "reviewer": {"version": REVIEWER_PROMPT_VERSION, "sha256": REVIEWER_PROMPT_SHA256},
    }
