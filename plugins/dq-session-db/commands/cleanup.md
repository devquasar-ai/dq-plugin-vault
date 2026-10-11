---
description: Tidy the session archive - list, hide or remove quick lookup/test sessions (list/hide/remove/unhide/auto)
argument-hint: "[list | hide [SESSION...] | remove [SESSION...] | unhide SESSION... | auto on|off]"
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/scripts/cleanup-ctl.sh:*)
---

Run `${CLAUDE_PLUGIN_ROOT}/scripts/cleanup-ctl.sh $ARGUMENTS` (use `list` when no arguments
were given), then:

Reply with the script's output exactly as it printed it, inside a code block: same
lines, same words, nothing summarized or rephrased, and nothing added before or after.

`remove` deletes sessions. Without `--yes` it only shows what would be removed. Show that list
to the user and ask them to confirm. Run it again with `--yes` added only after they say yes
in this conversation. Never add `--yes` on your own.

`list` and `hide` change nothing permanent: hidden sessions stay in the archive and come back
with `unhide`.

Do not explain the plugin and do not run anything else.
