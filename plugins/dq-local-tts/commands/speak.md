---
description: Say something out loud with the local TTS voice
argument-hint: "<text to speak>"
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/scripts/tts-ctl.sh:*)
---

Speak this aloud: $ARGUMENTS

Run `${CLAUDE_PLUGIN_ROOT}/scripts/tts-ctl.sh say "<the text above>"`. If no text
was given, speak the last substantive thing you told the user instead.

Reply with one short line confirming what was queued. Nothing else.
