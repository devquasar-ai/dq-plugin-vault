---
description: Control local text-to-speech (Kokoro or macOS say) (on/off/voice/speed/backend/server/status)
argument-hint: "[on|off|stop|status|voice NAME|speed N|backend embedded|http|say|server up|down|build|status|restart|log]"
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/scripts/tts-ctl.sh:*)
---

Run `${CLAUDE_PLUGIN_ROOT}/scripts/tts-ctl.sh $ARGUMENTS` (use `status` when no
arguments were given) and report its output verbatim in a couple of lines.

`server up` may print a first-time-setup notice and take several minutes: it
pulls a multi-GB image, and falls back to building from the bundled source if
the pull fails. Both are expected, so report progress rather than treating it
as an error.

Do not explain the plugin, do not suggest follow-up commands, and do not run
anything else.
