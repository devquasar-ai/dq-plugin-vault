---
name: tts
description: 'Control the local voice that reads Claude''s replies aloud (macOS voices or Kokoro). Use when the user asks to turn the voice/speech/TTS on or off, stop or be quiet, change the voice or speed, switch backend, or check its status. Arguments: on, off, stop, status, voice [NAME], speed N, backend macos|kokoro|kokoro-server, server up|down|status, restart, log.'
argument-hint: '[on|off|stop|status|voice NAME|speed N|backend macos|kokoro|kokoro-server|server up|down|build|status|restart|log]'
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/scripts/tts-ctl.sh:*)
---

Run `${CLAUDE_PLUGIN_ROOT}/scripts/tts-ctl.sh $ARGUMENTS` (use `status` when no
arguments were given). If the user asked in their own words ("turn the voice off", "talk slower"),
pick the matching argument yourself, e.g. `off`, `on`, `stop`, `speed 0.9`. Then:

Reply with the script's output exactly as it printed it, inside a code block: same
lines, same words, nothing summarized or rephrased, and nothing added before or after.

`server up` may print a first-time-setup notice and take several minutes: it
pulls a multi-GB image, and falls back to building from the bundled source if
the pull fails. Both are expected, so report progress rather than treating it
as an error.

Do not explain the plugin, do not suggest follow-up commands, and do not run
anything else.
